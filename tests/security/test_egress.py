from __future__ import annotations

import ipaddress

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
)


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requested_urls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requested_urls.append(str(request.url))
        return self.response


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
