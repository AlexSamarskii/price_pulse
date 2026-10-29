from pricepulse.data.dedupe import dedupe_products
from pricepulse.data.normalize import normalize_product


def raw(title, *, source="source", url="https://fixture.test/item"):
    return {
        "title": title,
        "url": url,
        "currency": "RUB",
        "price": "10",
        "observed_at": "2026-01-01T00:00:00Z",
    }


def test_cross_source_titles_are_never_merged():
    left = normalize_product(raw("Tea"), source_id="source-a")
    right = normalize_product(raw("Tea"), source_id="source-b")
    results = dedupe_products([left, right])
    assert [result.action for result in results] == ["create", "create"]


def test_repeated_identity_is_update_but_conflict_is_review():
    same = [normalize_product(raw("Tea"), source_id="source"), normalize_product(raw("Tea"), source_id="source")]
    assert [result.action for result in dedupe_products(same)] == ["create", "update"]
    conflict = [normalize_product(raw("Tea"), source_id="source"), normalize_product(raw("Coffee"), source_id="source")]
    assert [result.action for result in dedupe_products(conflict)] == ["create", "review"]
