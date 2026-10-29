from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pricepulse.collectors.source_onboarding import validate_source_policy

_SPACE_RE = re.compile(r"[\s\u00a0\u1680\u180e\u2000-\u200b\u202f\u205f\u3000]+")
_PRICE_RE = re.compile(r"(?<![0-9])-?\d+(?:[.,\s]\d+)*(?:[.,]\d+)?(?![0-9])")
_SENSITIVE_KEYS = {
    "token",
    "access_token",
    "auth",
    "authorization",
    "apikey",
    "api_key",
    "session",
}


class PriceState(str, Enum):
    MISSING = "missing"
    UNKNOWN = "unknown"


class Availability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PriceField:
    state: str
    amount: Decimal | None = None
    currency: str | None = None
    precision: int | None = None
    unit_name: str | None = None
    unit_quantity: Decimal = Decimal("1")

    @classmethod
    def missing(cls) -> "PriceField":
        return cls("missing")

    @classmethod
    def unknown(cls) -> "PriceField":
        return cls("unknown")


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return _SPACE_RE.sub(" ", str(value)).strip()


def parse_decimal(value: object) -> Decimal:
    text = clean_text(value)
    match = _PRICE_RE.search(text)
    if not match:
        raise InvalidOperation("value does not contain a decimal number")
    compact = re.sub(r"[\s\u00a0]", "", match.group(0))
    comma_count = compact.count(",")
    dot_count = compact.count(".")
    if comma_count and dot_count:
        decimal_separator = "," if compact.rfind(",") > compact.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        compact = compact.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif comma_count > 1:
        compact = compact.replace(",", "")
    elif dot_count > 1:
        compact = compact.replace(".", "")
    elif comma_count == 1:
        compact = compact.replace(",", ".")
    amount = Decimal(compact)
    if amount <= 0:
        raise InvalidOperation("money amount must be positive")
    return amount


def decimal_precision(amount: Decimal) -> int:
    return max(-amount.as_tuple().exponent, 0)


def price_field(raw: object, currency: str, *, kind: str) -> PriceField:
    del kind
    if raw is None:
        return PriceField.missing()
    text = clean_text(raw).lower()
    if text in {"", "-", "n/a", "na", "none", "null"}:
        return PriceField.missing()
    if text in {"unknown", "not available", "unavailable"}:
        return PriceField.unknown()
    try:
        amount = parse_decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        return PriceField.unknown()
    return PriceField("present", amount, currency, decimal_precision(amount))


def normalize_availability(value: object) -> Availability:
    text = clean_text(value).lower().replace(" ", "_")
    if text in {"", "-", "n/a", "na", "none", "null", "unknown", "stale"}:
        return Availability.UNKNOWN
    if text in {"available", "in_stock", "instock", "true", "yes"}:
        return Availability.AVAILABLE
    if text in {"unavailable", "out_of_stock", "outofstock", "false", "no"}:
        return Availability.UNAVAILABLE
    return Availability.UNKNOWN


def normalize_canonical_url(
    raw: object,
    *,
    drop_parameters: Iterable[str] = (),
) -> str:
    text = str(raw or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.path:
        raise ValueError("canonical URL must be absolute HTTP(S) with a path")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("canonical URL must not contain credentials or a fragment")
    if any(segment == ".." for segment in parsed.path.split("/")):
        raise ValueError("canonical URL path must not traverse")
    dropped = {item.strip().lower() for item in drop_parameters}
    parameters = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in dropped and key.lower() not in _SENSITIVE_KEYS
    ]
    query = urlencode(parameters)
    path = parsed.path.rstrip("/") or "/"
    host = parsed.netloc.lower()
    return urlunsplit((parsed.scheme.lower(), host, path, query, ""))


def normalize_timestamp(value: object) -> str:
    text = clean_text(value)
    if not text:
        raise ValueError("observation timestamp is required")
    timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class NormalizedProduct:
    source_id: str
    identity_key: str
    title: str
    canonical_url: str
    variant_key: str
    sku: str | None
    attributes: dict[str, Any]
    packaging: dict[str, Any]
    unit_name: str | None
    unit_quantity: Decimal
    sale_price: PriceField
    list_price: PriceField
    unit_price: PriceField
    availability: Availability
    observed_at: str
    evidence: dict[str, Any]
    raw_hash: str


def _stable_hash(*parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def normalize_product(
    raw: Mapping[str, Any],
    *,
    source_id: str,
    currency: str | None = None,
    source_policy: Mapping[str, Any] | None = None,
) -> NormalizedProduct:
    policy = validate_source_policy(source_policy) if source_policy is not None else None
    if policy is not None and policy["source_id"] != source_id:
        raise ValueError("source policy does not belong to the normalized product source")
    title = clean_text(raw.get("title"))
    if not title:
        raise ValueError("normalized product requires a title")
    resolved_currency = clean_text(currency or raw.get("currency") or raw.get("price_currency")).upper()
    if len(resolved_currency) != 3 or not resolved_currency.isascii():
        raise ValueError("normalized product requires a three-letter currency")
    if policy is not None and resolved_currency not in policy["allowed_currencies"]:
        raise ValueError("product currency is outside source policy")

    canonical_url = normalize_canonical_url(
        raw.get("canonical_url") or raw.get("url"),
        drop_parameters=policy.get("drop_query_parameters", ()) if policy is not None else (),
    )
    observed_at = normalize_timestamp(raw.get("observed_at"))
    product_id = clean_text(raw.get("product_id"))
    identity_key = product_id or _stable_hash("url", canonical_url)

    attributes_value = raw.get("attributes") or {}
    if not isinstance(attributes_value, Mapping):
        raise ValueError("product attributes must be a mapping")
    attributes = {str(key): clean_text(value) for key, value in attributes_value.items() if value is not None}
    packaging_value = raw.get("packaging") or {}
    if not isinstance(packaging_value, Mapping):
        raise ValueError("packaging must be a mapping")
    packaging = dict(packaging_value)
    unit_name = clean_text(packaging_value.get("unit_name", raw.get("unit_name"))) or None
    try:
        unit_quantity = parse_decimal(packaging_value.get("quantity", raw.get("unit_quantity", "1")))
    except (InvalidOperation, TypeError, ValueError):
        unit_quantity = Decimal("1")
    if unit_quantity <= 0:
        raise ValueError("unit quantity must be positive")
    sku_value = clean_text(raw.get("sku"))
    sku = sku_value or None
    variant_material = {
        "sku": sku,
        "attributes": attributes,
        "unit_name": unit_name,
        "unit_quantity": str(unit_quantity),
    }
    variant_key = _stable_hash("variant", identity_key, variant_material)

    sale = price_field(raw.get("price"), resolved_currency, kind="sale")
    listed = price_field(raw.get("list_price"), resolved_currency, kind="list")
    unit = price_field(raw.get("unit_price"), resolved_currency, kind="unit")
    if unit.state == "present":
        unit = PriceField(
            unit.state,
            unit.amount,
            unit.currency,
            unit.precision,
            unit_name,
            unit_quantity,
        )
    evidence_value = raw.get("evidence")
    evidence = evidence_value if isinstance(evidence_value, dict) else {}
    raw_hash = _stable_hash("raw", raw)
    return NormalizedProduct(
        source_id=source_id,
        identity_key=identity_key,
        title=title,
        canonical_url=canonical_url,
        variant_key=variant_key,
        sku=sku,
        attributes=attributes,
        packaging=packaging,
        unit_name=unit_name,
        unit_quantity=unit_quantity,
        sale_price=sale,
        list_price=listed,
        unit_price=unit,
        availability=normalize_availability(raw.get("availability")),
        observed_at=observed_at,
        evidence=evidence,
        raw_hash=raw_hash,
    )
