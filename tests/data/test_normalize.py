from decimal import Decimal

import pytest

from pricepulse.data.normalize import (
    normalize_availability,
    normalize_canonical_url,
    normalize_product,
    parse_decimal,
    price_field,
)


def test_parse_decimal_accepts_supported_locale_forms_and_units():
    assert parse_decimal("1 299,90") == Decimal("1299.90")
    assert parse_decimal("1,299.90") == Decimal("1299.90")
    assert parse_decimal("1\u00a0299,90") == Decimal("1299.90")
    assert parse_decimal("1.299,90 EUR") == Decimal("1299.90")


def test_parse_decimal_rejects_negative_zero_and_non_numeric():
    for value in ("-1", "0", "—", "N/A"):
        with pytest.raises(Exception):
            parse_decimal(value)


def test_price_fields_distinguish_missing_and_unknown():
    assert price_field(None, "RUB", kind="sale").state == "missing"
    assert price_field("", "RUB", kind="sale").state == "missing"
    assert price_field("unknown", "RUB", kind="sale").state == "unknown"
    assert price_field("garbage", "RUB", kind="sale").state == "unknown"


def test_availability_does_not_turn_errors_into_out_of_stock():
    assert normalize_availability("In stock") == "available"
    assert normalize_availability("out of stock") == "unavailable"
    assert normalize_availability("error") == "unknown"
    assert normalize_availability("") == "unknown"


def test_canonical_url_keeps_variant_and_removes_only_policy_drop_or_secret():
    assert normalize_canonical_url(
        "HTTPS://Example.test/item?variant=red&tracking=x&token=s",
        drop_parameters=("tracking",),
    ) == "https://example.test/item?variant=red"


def test_canonical_url_rejects_escape_and_credentials():
    for url in ("https://example.test/../secret", "https://user:pass@example.test/item", "ftp://example.test/item"):
        with pytest.raises(ValueError):
            normalize_canonical_url(url)


def test_product_requires_currency_and_timestamp():
    raw = {"title": "Tea", "url": "https://fixture.test/item", "observed_at": "2026-01-01T00:00:00Z"}
    with pytest.raises(ValueError):
        normalize_product(raw, source_id="source")

    raw["currency"] = "RUB"
    normalized = normalize_product(raw, source_id="source")
    assert normalized.sale_price.state == "missing"
    assert normalized.observed_at == "2026-01-01T00:00:00.000+00:00"


def test_variant_changes_do_not_hide_packaging_differences():
    first = normalize_product(
        {
            "title": "Juice",
            "url": "https://fixture.test/juice",
            "currency": "RUB",
            "price": "100",
            "observed_at": "2026-01-01T00:00:00Z",
            "packaging": {"unit_name": "l", "quantity": "1"},
        },
        source_id="source",
    )
    second = normalize_product(
        {
            "title": "Juice",
            "url": "https://fixture.test/juice",
            "currency": "RUB",
            "price": "180",
            "observed_at": "2026-01-01T00:00:00Z",
            "packaging": {"unit_name": "l", "quantity": "2"},
        },
        source_id="source",
    )
    assert first.variant_key != second.variant_key
