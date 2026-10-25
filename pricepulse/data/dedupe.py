from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pricepulse.data.normalize import NormalizedProduct


@dataclass(frozen=True, slots=True)
class URLNormalizationRules:
    drop_query_parameters: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DedupeResult:
    product_key: str
    variant_key: str
    action: Literal["create", "update", "review"]
    reason: str


def normalize_canonical_url(
    raw: object,
    *,
    drop_parameters: Iterable[str] = (),
) -> str:
    text = str(raw or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.path:
        raise ValueError("canonical URL must be absolute HTTP(S) with a path")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("canonical URL must not contain credentials or a fragment")
    if any(segment == ".." for segment in parsed.path.split("/")):
        raise ValueError("canonical URL path must not traverse")
    dropped = {item.strip().lower() for item in drop_parameters}
    parameters = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in dropped:
            continue
        if key.lower() in {"token", "access_token", "auth", "authorization", "apikey", "api_key", "session"}:
            value = "[REDACTED]"
        parameters.append((key, value))
    query = urlencode(parameters)
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


def dedupe_products(products: Iterable[NormalizedProduct]) -> list[DedupeResult]:
    seen: dict[tuple[str, str], set[str]] = {}
    results: list[DedupeResult] = []
    for product in products:
        product_key = f"{product.source_id}:{product.identity_key}"
        variant_key = product.variant_key
        current = seen.setdefault((product_key, variant_key), set())
        current.add(product.title)
        if len(current) > 1:
            results.append(DedupeResult(product_key, variant_key, "review", "same identity has conflicting titles"))
        elif len(results) >= 0:
            results.append(DedupeResult(product_key, variant_key, "update", "deterministic product and variant match"))
    if results and len({item.product_key for item in results}) == len(results):
        results = [DedupeResult(item.product_key, item.variant_key, "create", "unique deterministic identity") for item in results]
    return results
