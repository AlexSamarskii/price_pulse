from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pricepulse.api.dashboard import dashboard_snapshot
from pricepulse.data.normalize import Availability, NormalizedProduct, PriceField
from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import Source, SourcePolicy, Schedule


@pytest.fixture
def snapshot_db(tmp_path, request):
    path = str(tmp_path / "snapshot.db")
    scenario = getattr(request, "param", "fresh")
    now = datetime(2020, 1, 1, tzinfo=timezone.utc) if scenario == "stale" else datetime.now(timezone.utc)
    with Database(path) as database:
        database.initialize()
        repository = DataRepository(database)
        for source in ("first", "second"):
            repository.upsert_source(Source(source, source.title(), "https://fixture.test"))
            repository.upsert_policy(SourcePolicy("policy_" + source, source, {}, "0" * 64, True))
            repository.upsert_schedule(Schedule("catalog_" + source, source, json.dumps({"type": "interval", "seconds": 600}), max_concurrency=1, requests_per_minute=6, enabled=True))
            database.connection.execute(
                "INSERT INTO collection_sources(source_id,config_json,version_id,updated_at) VALUES(?,?,?,?)",
                (source, json.dumps({"enabled": True, "interval_seconds": 600}), "fixture", now.isoformat()),
            )
            item = NormalizedProduct(source_id=source, identity_key="p", title="Fixture product", canonical_url="https://fixture.test/p",
                variant_key="s", sku="SKU-S", attributes={"size": "S"}, packaging={}, unit_name="piece", unit_quantity=Decimal("1.000") if scenario == "equivalent" else Decimal("1"),
                sale_price=PriceField("unknown") if scenario == "unknown" else PriceField("present", Decimal("100.10"), "RUB", 2),
                list_price=PriceField("absent"), unit_price=PriceField("absent"), availability=Availability.AVAILABLE,
                observed_at=(now-timedelta(hours=3) if scenario == "old_previous" else now-timedelta(minutes=5)).isoformat(), evidence={}, raw_hash="a")
            repository.persist_normalized(item)
            repository.persist_normalized(replace(item, unit_quantity=Decimal("1"), sale_price=PriceField("unknown") if scenario == "unknown" else PriceField("present", Decimal("105.30"), "RUB", 2), observed_at=now.isoformat(), raw_hash="b"))
            repository.persist_normalized(replace(item, variant_key="m", sku="SKU-M", attributes={"size": "M"}, sale_price=PriceField("present", Decimal("100.10"), "USD", 2) if scenario == "mixed" else item.sale_price, observed_at=now.isoformat(), raw_hash="c"))
    return path


def test_snapshot_groups_variants_and_scopes_counts_read_only(snapshot_db):
    with Database(snapshot_db) as db:
        before = db.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    state = dashboard_snapshot(snapshot_db, source_id="first")
    assert state["totals"] == {"products": 1, "variants": 2, "observations": 3}
    assert len(state["products"]) == 1
    product = state["products"][0]
    assert product["price_min"] == "100.10" and product["price_max"] == "105.30"
    variants = {v["attributes"]["size"]: v for v in product["variants"]}
    assert variants["S"]["delta"] == "5.20"
    assert variants["S"]["latest"]["stale"] is False
    assert variants["M"]["delta"] is None
    assert [s["source_id"] for s in state["sources"]] == ["first"]
    assert state["sources"][0]["enabled"] is True
    assert state["sources"][0]["interval_seconds"] == 600
    with Database(snapshot_db) as db:
        assert db.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == before
    assert dashboard_snapshot(snapshot_db)["totals"]["products"] == 2


def test_snapshot_uses_current_policy_and_saved_schedule(snapshot_db):
    with Database(snapshot_db) as db:
        repository = DataRepository(db)
        repository.upsert_policy(SourcePolicy("disabled", "first", {}, "1" * 64, False))
        db.connection.execute("UPDATE sources SET current_policy_id='policy_first' WHERE source_id='first'")
        db.connection.execute("UPDATE schedules SET enabled=0 WHERE source_id='first'")
    source = dashboard_snapshot(snapshot_db, source_id="first")["sources"][0]
    assert source["live_enabled"] is True
    assert source["enabled"] is False
    assert source["next_run_at"] is None


@pytest.mark.parametrize("snapshot_db", ["stale"], indirect=True)
def test_snapshot_never_invents_stale_changes(snapshot_db):
    stale = dashboard_snapshot(snapshot_db, source_id="first")
    assert all(v["latest"]["stale"] and v["delta"] is None for v in stale["products"][0]["variants"])


@pytest.mark.parametrize("snapshot_db", ["unknown"], indirect=True)
def test_snapshot_never_invents_unknown_prices(snapshot_db):
    unknown = dashboard_snapshot(snapshot_db, source_id="first")["products"][0]
    assert unknown["price_min"] is None
    assert all(v["latest"]["price_amount"] is None for v in unknown["variants"])


@pytest.mark.parametrize("snapshot_db", ["mixed"], indirect=True)
def test_snapshot_separates_currencies_and_units(snapshot_db):
    product = dashboard_snapshot(snapshot_db, source_id="first")["products"][0]
    assert product["mixed_dimensions"] is True
    assert product["price_min"] is None and product["currency"] is None


def test_snapshot_is_bounded_and_marks_truncation(snapshot_db, monkeypatch):
    import pricepulse.api.dashboard as module
    monkeypatch.setattr(module, "MAX_PRODUCTS", 1)
    monkeypatch.setattr(module, "MAX_VARIANTS", 1)
    state = dashboard_snapshot(snapshot_db)
    assert state["totals"]["products"] == 2
    assert state["truncated"]["products"] and state["truncated"]["variants"]
    assert len(state["products"]) == 1 and len(state["products"][0]["variants"]) == 1
    monkeypatch.setattr(module, "MAX_SNAPSHOT_BYTES", 600)
    state = dashboard_snapshot(snapshot_db)
    assert len(json.dumps(state, ensure_ascii=False).encode()) <= 600


def test_snapshot_handles_malformed_metadata(snapshot_db):
    with Database(snapshot_db) as db:
        db.connection.execute("UPDATE variants SET attributes_json='[]'")
        db.connection.execute("UPDATE collection_sources SET config_json='[]'")
    state = dashboard_snapshot(snapshot_db)
    assert not any(s["enabled"] for s in state["sources"])
    assert all(v["attributes"] == {} for p in state["products"] for v in p["variants"])
    with pytest.raises(LookupError):
        dashboard_snapshot(snapshot_db, source_id="missing")


@pytest.mark.parametrize("snapshot_db", ["equivalent"], indirect=True)
def test_snapshot_normalizes_equivalent_decimal_quantities(snapshot_db):
    product = dashboard_snapshot(snapshot_db, source_id="first")["products"][0]
    assert product["mixed_dimensions"] is False
    assert next(v for v in product["variants"] if v["sku"] == "SKU-S")["delta"] == "5.20"


@pytest.mark.parametrize("snapshot_db", ["old_previous"], indirect=True)
def test_snapshot_does_not_compare_to_stale_previous_price(snapshot_db):
    product = dashboard_snapshot(snapshot_db, source_id="first")["products"][0]
    variant = next(v for v in product["variants"] if v["sku"] == "SKU-S")
    assert variant["latest"]["stale"] is False
    assert variant["previous"]["stale"] is True
    assert variant["delta"] is None


def append_test_observation(path, **changes):
    with Database(path) as db:
        variant = db.connection.execute("SELECT variant_id FROM variants WHERE sku='SKU-S' LIMIT 1").fetchone()[0]
        values = dict(variant_id=variant, observed_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat(),
            ingested_at=datetime.now(timezone.utc).isoformat(), status="confirmed", price_state="present", price_amount="120.00",
            price_currency="RUB", price_kind="sale", price_precision=2, unit_quantity="1", unit_name="piece",
            availability="available", raw_hash="regression", evidence_json="{}", provenance_json="{}")
        values.update(changes)
        db.connection.execute(f"INSERT INTO observations({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
        return variant


def test_snapshot_respects_recent_explicit_stale_status(snapshot_db):
    variant_id = append_test_observation(snapshot_db, status="stale")
    variant = next(v for p in dashboard_snapshot(snapshot_db)["products"] for v in p["variants"] if v["variant_id"] == variant_id)
    assert variant["latest"]["stale"] is True
    assert variant["latest"]["price_amount"] is None and variant["delta"] is None


@pytest.mark.parametrize("changes", [{"price_currency": "bad"}, {"unit_quantity": "0"}, {"unit_quantity": "NaN"}, {"price_amount": "1e9999"}, {"unit_name": ""}])
def test_snapshot_invalid_money_dimensions_stay_unknown(snapshot_db, changes):
    variant_id = append_test_observation(snapshot_db, **changes)
    variant = next(v for p in dashboard_snapshot(snapshot_db)["products"] for v in p["variants"] if v["variant_id"] == variant_id)
    assert variant["latest"]["price_amount"] is None and variant["delta"] is None


def test_snapshot_whole_variant_price_does_not_invent_unit(snapshot_db):
    variant_id = append_test_observation(snapshot_db, unit_name=None)
    variant = next(v for p in dashboard_snapshot(snapshot_db)["products"] for v in p["variants"] if v["variant_id"] == variant_id)
    assert variant["latest"]["price_amount"] == "120.00"
    assert variant["latest"]["unit_name"] is None
    assert variant["delta"] is None
