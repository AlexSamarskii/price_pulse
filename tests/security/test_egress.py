from __future__ import annotations

import ipaddress
from collections.abc import Iterator

import httpx
import pytest

from pricepulse.egress import EgressBroker, EgressPolicy, EgressViolation
from pricepulse.egress.broker import _address_is_safe, resolve_allowed_host


POLICY = EgressPolicy(
    allowed_hosts=("example.invalid",),
    allowed_path_prefixes=("/products/",),
    timeout_seconds=1,
    max_redirects=3,
    requests_per_minute=60,
    max_concurrency=1,
    max_response_bytes=16,
    robots_policy="operator_authorized_fixture",
)


class RecordingTransport(httpx.MockTransport):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__(self.handle_request)
        self.response = response
        self.requested_urls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requested_urls.append(str(request.url))
        return self.response


def test_rate_limit_counts_concurrent_requests_and_redirect_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    clock = [100.0]
    monkeypatch.setattr("pricepulse.egress.broker.time.monotonic", lambda: clock[0])
    policy = replace(POLICY, requests_per_minute=1, max_concurrency=4)
    requested_urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, content=b"ok")

    broker = EgressBroker(policy, transport=httpx.MockTransport(respond), resolver=lambda _: "8.8.8.8")

    def request_once(_: object) -> int | str:
        try:
            return broker.request("GET", "https://example.invalid/products/1").status_code
        except EgressViolation as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(request_once, range(4)))
    assert outcomes.count(200) == 1
    assert outcomes.count("rate limit exceeded") == 3
    assert len(requested_urls) == 1

    clock[0] = 159.99
    assert request_once(None) == "rate limit exceeded"
    clock[0] = 160.0
    assert request_once(None) == 200
    assert request_once(None) == "rate limit exceeded"
    assert len(requested_urls) == 2

    redirected_urls: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        redirected_urls.append(str(request.url))
        return httpx.Response(302, headers={"location": "/products/next"})

    redirect_broker = EgressBroker(policy, transport=httpx.MockTransport(redirect), resolver=lambda _: "8.8.8.8")
    with pytest.raises(EgressViolation, match="rate limit exceeded"):
        redirect_broker.request("GET", "https://example.invalid/products/1")
    assert len(redirected_urls) == 1


def test_streaming_response_stops_at_size_limit() -> None:
    class CountingStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.read_chunks = 0
            self.closed = False

        def __iter__(self) -> Iterator[bytes]:
            for _ in range(1000):
                self.read_chunks += 1
                yield b"x" * 8

        def close(self) -> None:
            self.closed = True

    stream = CountingStream()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    broker = EgressBroker(POLICY, transport=transport, resolver=lambda _: "8.8.8.8")
    with pytest.raises(EgressViolation, match="size limit"):
        broker.request("GET", "https://example.invalid/products/1")
    assert stream.read_chunks == 3
    assert stream.closed

    broker = EgressBroker(
        POLICY,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"ok")),
        resolver=lambda _: "8.8.8.8",
    )
    response = broker.request("GET", "https://example.invalid/products/1")
    assert response.content == b"ok"
    assert response.headers["content-length"] == "2"


def test_live_egress_honors_robots_before_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    clock = [100.0]
    monkeypatch.setattr("pricepulse.egress.broker.time.monotonic", lambda: clock[0])
    source_policy = {
        "allowed_hosts": ["example.invalid"],
        "allowed_path_prefixes": ["/products/", "/robots.txt"],
        "live_enabled": True,
        "robots_policy": "honor",
        "timeout_seconds": 1,
        "requests_per_minute": 60,
        "max_concurrency": 1,
    }
    policy = EgressPolicy.from_source_policy(source_policy)
    assert policy.robots_policy == "honor"
    calls: list[str] = []
    robots = [
        "User-agent: *\nDisallow: /products/private\nDisallow: /products/*secret$\n"
        "Allow: /\nUser-agent: PricePulse\nCrawl-delay: 1\n"
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["host"] == "example.invalid"
        assert request.headers["user-agent"] == "PricePulse/0.1"
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots[0], headers={"content-type": "text/plain"})
        if request.url.path == "/products/redirect":
            return httpx.Response(302, headers={"location": "/products/private"})
        return httpx.Response(200, content=b"ok")

    broker = EgressBroker(policy, transport=httpx.MockTransport(respond), resolver=lambda _: "8.8.8.8")
    assert broker.request("GET", "https://example.invalid/products/public", headers={
        "Host": "metadata.internal", "User-Agent": "OtherBot",
    }).content == b"ok"
    for path in ("private", "anything-secret"):
        with pytest.raises(EgressViolation, match="robots.txt disallows"):
            broker.request("GET", f"https://example.invalid/products/{path}")
    with pytest.raises(EgressViolation, match="crawl delay"):
        broker.request("GET", "https://example.invalid/products/public")
    clock[0] = 101.0
    with pytest.raises(EgressViolation, match="robots.txt disallows"):
        broker.request("GET", "https://example.invalid/products/redirect")
    assert calls == ["/robots.txt", "/products/public", "/products/redirect"]

    clock[0] = 401.0
    robots[0] = "User-agent: *\nDisallow: /\n"
    with pytest.raises(EgressViolation, match="robots.txt disallows"):
        broker.request("GET", "https://example.invalid/products/public")
    assert calls[-1] == "/robots.txt" and len(calls) == 4

    missing_path = EgressBroker(
        replace(policy, allowed_path_prefixes=("/products/",)),
        transport=httpx.MockTransport(respond), resolver=lambda _: "8.8.8.8",
    )
    with pytest.raises(EgressViolation, match="explicitly allowed"):
        missing_path.request("GET", "https://example.invalid/products/public")
    assert len(calls) == 4

    failed_calls: list[str] = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        failed_calls.append(request.url.path)
        return httpx.Response(403)

    failed = EgressBroker(policy, transport=httpx.MockTransport(unavailable), resolver=lambda _: "8.8.8.8")
    with pytest.raises(EgressViolation, match="robots.txt is unavailable"):
        failed.request("GET", "https://example.invalid/products/public")
    assert failed_calls == ["/robots.txt"]
    with pytest.raises(ValueError, match="robots_policy=honor"):
        EgressPolicy.from_source_policy({**source_policy, "robots_policy": "operator_authorized_fixture"})
    with pytest.raises(EgressViolation, match="offline MockTransport"):
        EgressBroker(POLICY)


def test_source_policy_rejects_wildcards_and_disabled_live() -> None:
    with pytest.raises(ValueError):
        EgressPolicy.from_source_policy(
            {
                "allowed_hosts": ["*.example.invalid"],
                "allowed_path_prefixes": ["/"],
                "live_enabled": True,
                "timeout_seconds": 10,
                "requests_per_minute": 6,
                "max_concurrency": 1,
            }
        )
    with pytest.raises(ValueError):
        EgressPolicy.from_source_policy(
            {
                "allowed_hosts": ["example.invalid"],
                "allowed_path_prefixes": ["/"],
                "live_enabled": False,
                "timeout_seconds": 10,
                "requests_per_minute": 6,
                "max_concurrency": 1,
            }
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://metadata.internal:8443/latest/meta-data",
        "https://example.invalid/admin/",
        "https://example.invalid:22/products/",
        "https://example.invalid/products/..;/admin/",
        "https://example.invalid/products/%2e%2e;/admin/",
        "https://example.invalid/products/%2e%2e/admin",
        "https://example.invalid/products/%2fadmin/",
        "https://example.invalid/products/%252e%252e/admin",
        "https://example.invalid/products/%00",
    ],
)
def test_disallowed_requests_never_connect(url: str) -> None:
    transport = RecordingTransport(httpx.Response(200, content=b"ok"))
    broker = EgressBroker(POLICY, transport=transport)
    with pytest.raises(EgressViolation):
        broker.request("GET", url)
    assert transport.requested_urls == []


@pytest.mark.parametrize(
    "path",
    [
        "/products/..;/admin/",
        "/products/%2e%2e;/admin/",
        "/products/%2fadmin/",
        "/products/%252e%252e/admin",
    ],
)
def test_effective_path_rejects_encoded_and_parameter_ambiguity(path: str) -> None:
    transport = RecordingTransport(httpx.Response(200, content=b"ok"))
    broker = EgressBroker(POLICY, transport=transport)
    with pytest.raises(EgressViolation):
        broker.request("GET", f"https://example.invalid{path}")
    assert transport.requested_urls == []


def test_policy_rejects_ambiguous_path_prefix() -> None:
    source_policy = {
        "allowed_hosts": ["example.invalid"],
        "allowed_path_prefixes": ["/products/%2e%2e/"],
        "live_enabled": True,
        "timeout_seconds": 10,
        "requests_per_minute": 6,
        "max_concurrency": 1,
    }
    with pytest.raises(ValueError):
        EgressPolicy.from_source_policy(source_policy)


@pytest.mark.parametrize(
    ("prefix", "path", "expected"),
    [
        ("/catalog", "/catalog", True),
        ("/catalog", "/catalog/item/", True),
        ("/catalog", "/catalog-admin/", False),
        ("/catalog/", "/catalog/item/", True),
        ("/catalog/", "/catalog", False),
    ],
)
def test_allowed_path_prefix_uses_segment_boundary(
    prefix: str, path: str, expected: bool
) -> None:
    policy = EgressPolicy.from_source_policy(
        {
            "allowed_hosts": ["example.invalid"],
            "allowed_path_prefixes": [prefix],
            "live_enabled": True,
            "timeout_seconds": 10,
            "requests_per_minute": 6,
            "max_concurrency": 1,
        }
    )
    assert policy.allows_path(path) is expected


def test_allowed_request_pins_resolved_public_address() -> None:
    transport = RecordingTransport(httpx.Response(200, content=b"ok"))
    broker = EgressBroker(
        POLICY,
        transport=transport,
        resolver=lambda host: "8.8.8.8",
    )
    response = broker.request("GET", "https://example.invalid/products/item")
    assert response.status_code == 200
    assert response.text == "ok"
    assert transport.requested_urls == ["https://8.8.8.8/products/item"]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.2",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
    ],
)
def test_resolved_private_and_metadata_addresses_are_rejected(address: str) -> None:
    assert not _address_is_safe(address)
    transport = RecordingTransport(httpx.Response(200, content=b"ok"))
    broker = EgressBroker(POLICY, transport=transport, resolver=lambda host: address)
    with pytest.raises(EgressViolation):
        broker.request("GET", "https://example.invalid/products/item")
    assert transport.requested_urls == []


def test_redirect_to_disallowed_path_is_blocked() -> None:
    redirect = httpx.Response(302, headers={"location": "https://example.invalid/admin/"})
    transport = RecordingTransport(redirect)
    broker = EgressBroker(POLICY, transport=transport, resolver=lambda host: "8.8.8.8")
    with pytest.raises(EgressViolation):
        broker.request("GET", "https://example.invalid/products/item")
    assert len(transport.requested_urls) == 1


def test_path_traversal_is_canonicalized_and_denied() -> None:
    transport = RecordingTransport(httpx.Response(200, content=b"ok"))
    broker = EgressBroker(POLICY, transport=transport)
    with pytest.raises(EgressViolation):
        broker.request("GET", "https://example.invalid/products/../admin/")
    assert transport.requested_urls == []


def test_response_size_limit_is_enforced() -> None:
    transport = RecordingTransport(httpx.Response(200, content=b"x" * 17))
    broker = EgressBroker(POLICY, transport=transport, resolver=lambda host: "8.8.8.8")
    with pytest.raises(EgressViolation):
        broker.request("GET", "https://example.invalid/products/item")


def test_dns_resolution_failure_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(host: str) -> str:
        raise EgressViolation("DNS resolution failed")

    monkeypatch.setattr("pricepulse.egress.broker.resolve_allowed_host", fail)
    with pytest.raises(EgressViolation):
        resolve_allowed_host("example.invalid")
