from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from pricepulse.data.normalize import NormalizedProduct, normalize_canonical_url


@dataclass(frozen=True, slots=True)
class URLNormalizationRules:
    drop_query_parameters: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DedupeResult:
    product_key: str
    variant_key: str
    action: Literal["create", "update", "review"]
    reason: str


__all__ = ["DedupeResult", "URLNormalizationRules", "dedupe_products", "normalize_canonical_url"]


def dedupe_products(products: Iterable[NormalizedProduct]) -> list[DedupeResult]:
    seen: dict[tuple[str, str], str] = {}
    results: list[DedupeResult] = []
    for product in products:
        product_key = f"{product.source_id}:{product.identity_key}"
        variant_key = product.variant_key
        group = (product_key, variant_key)
        previous_title = seen.get(group)
        if previous_title is not None and previous_title != product.title:
            results.append(DedupeResult(product_key, variant_key, "review", "same identity has conflicting titles"))
            continue
        if previous_title is None:
            seen[group] = product.title
            action = "create"
            reason = "unique deterministic identity"
        else:
            action = "update"
            reason = "deterministic product and variant match"
        results.append(DedupeResult(product_key, variant_key, action, reason))
    return results
