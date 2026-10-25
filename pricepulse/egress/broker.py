from __future__ import annotations

import ipaddress
import socket
import threading
import time
import base64
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urljoin

import httpx

from pricepulse.egress.policy import EgressPolicy, effective_path


class EgressViolation(Exception):
    pass


SAFE_SCHEMES = {"http", "https"}
DISALLOWED_PORTS = {22, 23, 25, 587, 465, 3389}


def _address_is_safe(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return not (
        parsed.is_loopback
        or parsed.is_private
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def resolve_allowed_host(host: str) -> str:
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise EgressViolation("DNS resolution failed") from exc
    if not addresses:
        raise EgressViolation("DNS resolution returned no addresses")
    for info in addresses:
        address = str(info[4][0])
        if not _address_is_safe(address):
            raise EgressViolation(f"resolved address is disallowed: {address}")
    return str(addresses[0][4][0])


def _validated_effective_path(path: str) -> str:
    try:
        return effective_path(path)
    except (UnicodeDecodeError, UnicodeError, ValueError) as exc:
        raise EgressViolation(f"path is not allowed: {path}") from exc


@dataclass
class _RateWindow:
    requests_per_minute: int
    lock: threading.Lock
    started_at: float

    def acquire(self) -> None:
        with self.lock:
            elapsed = time.monotonic() - self.started_at
            if elapsed >= 60:
                self.started_at = time.monotonic()
                return
            expected = int(self.requests_per_minute * elapsed / 60) + 1
            if expected > self.requests_per_minute:
                raise EgressViolation("rate limit exceeded")


class _PinnedTransport(httpx.BaseTransport):
    def __init__(
        self,
        inner: httpx.BaseTransport,
        policy: EgressPolicy,
        resolver: Callable[[str], str] = resolve_allowed_host,
    ) -> None:
        self._inner = inner
        self._policy = policy
        self._resolver = resolver

    def _validated_url(self, url: httpx.URL) -> tuple[httpx.URL, str, str]:
        if url.scheme not in SAFE_SCHEMES:
            raise EgressViolation(f"scheme is not allowed: {url.scheme}")
        if url.userinfo:
            raise EgressViolation("userinfo is not allowed")
        if url.port not in {None, 80, 443} or url.port in DISALLOWED_PORTS:
            raise EgressViolation("port is not allowed")
        host = url.host
        if not isinstance(host, str) or not self._policy.allows_host(host):
            raise EgressViolation(f"host is not allowed: {host}")
        path = _validated_effective_path(url.path)
        if not self._policy.allows_path(path):
            raise EgressViolation(f"path is not allowed: {path}")
        address = self._resolver(host)
        if not _address_is_safe(address):
            raise EgressViolation(f"resolved address is disallowed: {address}")
        pinned = url.copy_with(host=address, path=path)
        return pinned, host, address

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        pinned, original_host, _ = self._validated_url(request.url)
        headers = dict(request.headers)
        headers["Host"] = request.headers.get("host", original_host)
        headers.pop("accept-encoding", None)
        pinned_request = httpx.Request(
            request.method,
            pinned,
            headers=headers,
            content=request.read(),
            extensions={**request.extensions, "sni_hostname": original_host},
        )
        return self._inner.handle_request(pinned_request)


class EgressBroker:
    def __init__(
        self,
        policy: EgressPolicy,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str], str] = resolve_allowed_host,
    ) -> None:
        self.policy = policy
        self._resolver = resolver
        inner = transport or httpx.HTTPTransport(retries=0)
        self._transport = _PinnedTransport(inner, policy, resolver)
        self._client = httpx.Client(
            transport=self._transport,
            timeout=policy.timeout_seconds,
            follow_redirects=False,
        )
        self._rate_window = _RateWindow(policy.requests_per_minute, threading.Lock(), time.monotonic())
        self._concurrency = threading.BoundedSemaphore(policy.max_concurrency)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Iterable[tuple[str, str]] | Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        if not self._concurrency.acquire(timeout=1.0):
            raise EgressViolation("concurrency limit exceeded")
        try:
            self._rate_window.acquire()
            current_url = url
            response: httpx.Response | None = None
            for _ in range(self.policy.max_redirects + 1):
                request = self._build_request(method, current_url, headers, content)
                response = self._client.send(request)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    self._read_bounded(response)
                    return response
                location = response.headers.get("location")
                if not location:
                    raise EgressViolation("redirect has no location")
                response.close()
                current_url = str(urljoin(str(request.url), location))
                content = b"" if response.status_code in {301, 302, 303} else content
                method = "GET" if response.status_code in {301, 302, 303} else method
            raise EgressViolation("redirect limit exceeded")
        finally:
            self._concurrency.release()

    def _build_request(
        self,
        method: str,
        url: str,
        headers: Iterable[tuple[str, str]] | Mapping[str, str] | None,
        content: bytes | None,
    ) -> httpx.Request:
        if method.upper() not in {"GET", "HEAD"}:
            raise EgressViolation("only GET and HEAD are allowed")
        return httpx.Request(method.upper(), url, headers=headers, content=content or b"")

    def _read_bounded(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes(chunk_size=8192):
            total += len(chunk)
            if total > self.policy.max_response_bytes:
                response.close()
                raise EgressViolation("response exceeds the size limit")
            chunks.append(chunk)
        return b"".join(chunks)


MAX_BROKER_REQUEST_BYTES = 65_536


class _BrokerHandler(BaseHTTPRequestHandler):
    broker: EgressBroker

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ready"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/fetch":
            self._send_json(404, {"error": "not found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= MAX_BROKER_REQUEST_BYTES:
                raise ValueError("request body size is invalid")
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            response = self.broker.request(
                str(body["method"]),
                str(body["url"]),
                headers=body.get("headers", []),
                content=base64.b64decode(body.get("content_b64", "")),
            )
            self._send_json(
                200,
                {
                    "status_code": response.status_code,
                    "headers": [(key, value) for key, value in response.headers.multi_items()],
                    "body_b64": base64.b64encode(response.read()).decode("ascii"),
                },
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, EgressViolation, httpx.HTTPError) as exc:
            self._send_json(400, {"error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        return


def serve() -> None:
    raw_policy = os.environ.get("PRICEPULSE_EGRESS_POLICY")
    if not raw_policy:
        raise RuntimeError("PRICEPULSE_EGRESS_POLICY is required")
    broker = EgressBroker(EgressPolicy.from_source_policy(json.loads(raw_policy)))
    handler = type("ConfiguredBrokerHandler", (_BrokerHandler,), {"broker": broker})
    ThreadingHTTPServer(("0.0.0.0", 8080), handler).serve_forever()


if __name__ == "__main__":
    serve()
