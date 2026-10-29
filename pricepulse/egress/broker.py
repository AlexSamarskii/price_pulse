from __future__ import annotations

import ipaddress
import socket
import threading
import time
import base64
import json
import os
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urljoin

import httpx

from pricepulse.egress.policy import EgressPolicy, effective_path


class EgressViolation(Exception):
    pass


SAFE_SCHEMES = {"http", "https"}
DISALLOWED_PORTS = {22, 23, 25, 587, 465, 3389}
ROBOTS_AGENT = "PricePulse/0.1"


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
    admitted_at: deque[float] = field(default_factory=deque)

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            while self.admitted_at and self.admitted_at[0] <= now - 60:
                self.admitted_at.popleft()
            if len(self.admitted_at) >= self.requests_per_minute:
                raise EgressViolation("rate limit exceeded")
            self.admitted_at.append(now)


@dataclass(frozen=True)
class _RobotsRule:
    pattern: str
    anchored: bool

    def matches(self, target: str) -> bool:
        parts = self.pattern.split("*")
        if len(parts) == 1:
            return target == self.pattern if self.anchored else target.startswith(self.pattern)
        if not target.startswith(parts[0]):
            return False
        position = len(parts[0])
        for part in parts[1:-1]:
            found = target.find(part, position)
            if found < 0:
                return False
            position = found + len(part)
        if self.anchored:
            return target.endswith(parts[-1]) and len(target) - len(parts[-1]) >= position
        return target.find(parts[-1], position) >= 0


@dataclass
class _RobotsDecision:
    disallowed: tuple[_RobotsRule, ...]
    expires_at: float
    interval_seconds: float
    next_request_at: float = 0.0

    def admit(self, target: str, now: float) -> None:
        if any(rule.matches(target) for rule in self.disallowed):
            raise EgressViolation("robots.txt disallows the target")
        if now < self.next_request_at:
            raise EgressViolation("robots.txt crawl delay is active")
        self.next_request_at = now + self.interval_seconds


def _decode_robots_value(value: str) -> str:
    for _ in range(16):
        try:
            decoded = unquote(value, encoding="utf-8", errors="strict")
        except UnicodeError as exc:
            raise EgressViolation("robots path encoding is invalid") from exc
        if decoded == value:
            if "%" in value or "\x00" in value:
                raise EgressViolation("robots path encoding is unsupported")
            return value
        value = decoded
    raise EgressViolation("robots path encoding is too deep")


def _parse_robots(text: str) -> _RobotsDecision:
    agents: list[str] = []
    rules_started = False
    denied: list[_RobotsRule] = []
    interval = 0.0
    for raw_line in text.splitlines():
        key, separator, value = raw_line.partition("#")[0].partition(":")
        if not separator:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if rules_started:
                agents = []
                rules_started = False
            agents.append(value.lower())
            continue
        if key not in {"allow", "disallow", "crawl-delay", "request-rate"}:
            continue
        rules_started = True
        if not any(agent and (agent == "*" or agent in ROBOTS_AGENT.lower()) for agent in agents):
            continue
        if key == "disallow" and value:
            value = _decode_robots_value(value)
            if len(denied) >= 512 or len(value) > 4096 or not value.startswith(("/", "*")):
                raise EgressViolation("robots rules exceed supported bounds")
            denied.append(_RobotsRule(value[:-1] if value.endswith("$") else value, value.endswith("$")))
        elif key in {"crawl-delay", "request-rate"}:
            try:
                if key == "request-rate":
                    requests, seconds = value.split("/")
                    if int(requests) <= 0 or float(seconds) <= 0:
                        raise ValueError("invalid pacing")
                    delay = float(seconds) / int(requests)
                else:
                    delay = float(value)
                if not 0 <= delay <= 86400:
                    raise ValueError("invalid pacing")
            except (ValueError, ZeroDivisionError) as exc:
                raise EgressViolation("robots pacing rule is unsupported") from exc
            interval = max(interval, delay)
    return _RobotsDecision(tuple(denied), time.monotonic() + 300, interval)


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

    def validate_path(self, url: httpx.URL) -> str:
        if url.scheme not in SAFE_SCHEMES:
            raise EgressViolation(f"scheme is not allowed: {url.scheme}")
        if self._policy.robots_policy == "honor" and url.scheme != "https":
            raise EgressViolation("live egress requires HTTPS")
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
        return path

    def _validated_url(self, url: httpx.URL) -> tuple[httpx.URL, str, str]:
        path = self.validate_path(url)
        host = url.host
        address = self._resolver(host)
        if not _address_is_safe(address):
            raise EgressViolation(f"resolved address is disallowed: {address}")
        pinned = url.copy_with(host=address, path=path)
        return pinned, host, address

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        pinned, original_host, _ = self._validated_url(request.url)
        headers = httpx.Headers(request.headers)
        headers["Host"] = request.url.netloc.decode("ascii")
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
        if policy.robots_policy not in {"honor", "operator_authorized_fixture"}:
            raise EgressViolation("robots policy is invalid")
        if policy.robots_policy == "operator_authorized_fixture" and not isinstance(transport, httpx.MockTransport):
            raise EgressViolation("fixture egress requires an offline MockTransport")
        inner = transport or httpx.HTTPTransport(retries=0)
        self._transport = _PinnedTransport(inner, policy, resolver)
        self._client = httpx.Client(
            transport=self._transport,
            timeout=policy.timeout_seconds,
            follow_redirects=False,
        )
        self._rate_window = _RateWindow(policy.requests_per_minute, threading.Lock())
        self._concurrency = threading.BoundedSemaphore(policy.max_concurrency)
        self._robots_lock = threading.Lock()
        self._robots: dict[str, _RobotsDecision] = {}

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
            return self._request(method, url, headers, content, enforce_robots=True)
        finally:
            self._concurrency.release()

    def _request(
        self,
        method: str,
        url: str,
        headers: Iterable[tuple[str, str]] | Mapping[str, str] | None,
        content: bytes | None,
        *,
        enforce_robots: bool,
    ) -> httpx.Response:
        for _ in range(self.policy.max_redirects + 1):
            request = self._build_request(method, url, headers, content)
            self._transport.validate_path(request.url)
            if enforce_robots:
                self._check_robots(request.url)
            response = self._send_once(request)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            if not location:
                raise EgressViolation("redirect has no location")
            url = str(urljoin(str(request.url), location))
            if response.status_code in {301, 302, 303}:
                content, method = b"", "GET"
        raise EgressViolation("redirect limit exceeded")

    def _send_once(self, request: httpx.Request) -> httpx.Response:
        self._rate_window.acquire()
        response = self._client.send(request, stream=True)
        try:
            body = self._read_bounded(response) if response.status_code not in {301, 302, 303, 307, 308} else b""
        finally:
            response.close()
        headers = httpx.Headers(response.headers)
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        return httpx.Response(response.status_code, headers=headers, content=body, request=response.request)

    def _check_robots(self, url: httpx.URL) -> None:
        if self.policy.robots_policy == "operator_authorized_fixture":
            return
        robots_url = url.copy_with(path="/robots.txt", query=None, fragment=None)
        if not self.policy.allows_path("/robots.txt"):
            raise EgressViolation("robots.txt must be explicitly allowed by SourcePolicy")
        if not self._robots_lock.acquire(timeout=self.policy.timeout_seconds):
            raise EgressViolation("robots policy check is busy")
        try:
            origin = str(robots_url)
            decision = self._robots.get(origin)
            if decision is None or time.monotonic() >= decision.expires_at:
                response = self._request("GET", str(robots_url), None, None, enforce_robots=False)
                if response.status_code != 200:
                    raise EgressViolation("robots.txt is unavailable; live request is blocked")
                if "text/html" in response.headers.get("content-type", "").lower():
                    raise EgressViolation("robots.txt returned HTML; live request is blocked")
                decision = _parse_robots(response.text)
                self._robots[origin] = decision
            target = _validated_effective_path(url.path)
            if url.query:
                target += "?" + _decode_robots_value(url.query.decode("ascii"))
            decision.admit(target, time.monotonic())
        finally:
            self._robots_lock.release()

    def _build_request(
        self,
        method: str,
        url: str,
        headers: Iterable[tuple[str, str]] | Mapping[str, str] | None,
        content: bytes | None,
    ) -> httpx.Request:
        if method.upper() not in {"GET", "HEAD"}:
            raise EgressViolation("only GET and HEAD are allowed")
        request = httpx.Request(method.upper(), url, headers=headers, content=content or b"")
        if self.policy.robots_policy == "honor":
            request.headers["User-Agent"] = ROBOTS_AGENT
        return request

    def _read_bounded(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes(chunk_size=min(8192, self.policy.max_response_bytes + 1)):
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
