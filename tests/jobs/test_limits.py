from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import pricepulse.api.alerts as alerts_module
from pricepulse.api.alerts import AlertService
from pricepulse.data.normalize import Availability, NormalizedProduct, PriceField
from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import Source, SourcePolicy
from pricepulse.jobs.manager import JobLimitExceeded, JobManager, utc_isoformat


POLICY = {
    "source_id": "limited",
    "base_url": "https://fixture.test",
    "allowed_hosts": ["fixture.test"],
    "allowed_path_prefixes": ["/"],
    "robots_policy": "operator_authorized_fixture",
    "max_pages": 1,
    "requests_per_minute": 30,
    "max_concurrency": 1,
    "timeout_seconds": 10,
    "allowed_currencies": ["RUB"],
    "live_enabled": False,
}


def seeded(path: str, *, policy: dict | None = None) -> None:
    with Database(path) as database:
        database.initialize()
        repository = DataRepository(database)
        repository.upsert_source(Source(source_id="limited", name="Limited", base_url="https://fixture.test"))
        repository.upsert_policy(
            SourcePolicy(
                policy_id="policy_1",
                source_id="limited",
                policy=policy or POLICY,
                policy_digest="0" * 64,
                live_enabled=False,
            )
        )
        for index, price in enumerate(("10.00", "12.00", "15.00"), start=1):
            repository.persist_normalized(
                NormalizedProduct(
                    source_id="limited",
                    identity_key="same",
                    title="Product",
                    canonical_url="https://fixture.test/p/1",
                    variant_key="variant",
                    sku="SKU",
                    attributes={},
                    packaging={"unit_name": "piece", "quantity": "1"},
                    unit_name="piece",
                    unit_quantity=Decimal("1"),
                    sale_price=PriceField("present", Decimal(price), "RUB", 2),
                    list_price=PriceField("present", Decimal(price), "RUB", 2),
                    unit_price=PriceField("present", Decimal(price), "RUB", 2),
                    availability=Availability.AVAILABLE,
                    observed_at=f"2026-01-0{index}T00:00:00.000+00:00",
                    evidence={},
                    raw_hash=f"hash-{index}",
                )
            )


def test_concurrency_limit(tmp_path):
    path = str(tmp_path / "limits.db")
    seeded(path)
    manager = JobManager(path)
    job = manager.start_job(source_id="limited", idempotency_key="running")
    claimed = manager.claim(job_id=job.job_id, owner="worker")
    assert claimed is not None and claimed["status"] == "running"
    with pytest.raises(JobLimitExceeded):
        manager.start_job(source_id="limited", idempotency_key="blocked")


def test_rate_limit(tmp_path):
    path = str(tmp_path / "rate.db")
    seeded(path)
    manager = JobManager(path)
    now = utc_isoformat()
    with Database(path) as database:
        for index in range(30):
            database.connection.execute(
                """
                INSERT INTO collection_jobs(
                    job_id, source_id, idempotency_key, status, attempts, retry_budget,
                    backoff_seconds, created_at, updated_at
                )
                VALUES (?, 'limited', ?, 'queued', 0, 1, 0, ?, ?)
                """,
                (f"rate_{index}", f"rate-{index}", now, now),
            )
    with pytest.raises(JobLimitExceeded):
        manager.start_job(source_id="limited", idempotency_key="too-soon")


def test_concurrent_callers_cannot_exceed_concurrency_limit(tmp_path):
    path = str(tmp_path / "concurrent-concurrency.db")
    seeded(path)
    manager = JobManager(path)
    barrier = threading.Barrier(2)

    def start(index: int) -> object:
        barrier.wait()
        try:
            return manager.start_job(
                source_id="limited",
                idempotency_key=f"concurrent-{index}",
            )
        except JobLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, range(2)))
    assert sum(result is not None for result in results) == 1
    with pytest.raises(JobLimitExceeded):
        manager.start_job(source_id="limited", idempotency_key="after-race")
    with Database(path) as database:
        assert database.connection.execute(
            "SELECT COUNT(*) FROM collection_jobs WHERE source_id = 'limited'"
        ).fetchone()[0] == 1


def test_concurrent_callers_cannot_exceed_rate_limit(tmp_path):
    path = str(tmp_path / "concurrent-rate.db")
    seeded(path, policy=POLICY | {"requests_per_minute": 1, "max_concurrency": 2})
    manager = JobManager(path)
    barrier = threading.Barrier(2)

    def start(index: int) -> object:
        barrier.wait()
        try:
            return manager.start_job(
                source_id="limited",
                idempotency_key=f"rate-race-{index}",
            )
        except JobLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, range(2)))
    assert sum(result is not None for result in results) == 1
    with Database(path) as database:
        assert database.connection.execute(
            "SELECT COUNT(*) FROM collection_jobs WHERE source_id = 'limited'"
        ).fetchone()[0] == 1


def test_circuit_breaker(tmp_path):
    path = str(tmp_path / "breaker.db")
    seeded(path)
    manager = JobManager(path)
    now = utc_isoformat()
    with Database(path) as database:
        for index in range(5):
            database.connection.execute(
                """
                INSERT INTO collection_jobs(
                    job_id, source_id, idempotency_key, status, attempts, retry_budget,
                    backoff_seconds, created_at, updated_at
                )
                VALUES (?, 'limited', ?, 'failed', 3, 3, 0, ?, ?)
                """,
                (f"failed_{index}", f"failed-{index}", now, now),
            )
    with pytest.raises(JobLimitExceeded):
        manager.start_job(source_id="limited", idempotency_key="after-failures")


def test_retry_backoff_and_exhaustion(tmp_path):
    path = str(tmp_path / "retry.db")
    seeded(path)
    manager = JobManager(path)
    job = manager.start_job(source_id="limited", idempotency_key="retry", retry_budget=2, backoff_seconds=1)
    claim = manager.claim(job_id=job.job_id, owner="worker-1")
    assert claim is not None and claim["attempts"] == 1
    manager.complete(job_id=job.job_id, owner="worker-1", result={"status": "failed", "reason": "temporary"})
    with Database(path) as database:
        database.connection.execute(
            "UPDATE collection_jobs SET updated_at = ? WHERE job_id = ?",
            (utc_isoformat(), job.job_id),
        )
    assert manager.claim(job_id=job.job_id, owner="worker-2") is None
    with Database(path) as database:
        updated = database.connection.execute(
            "SELECT updated_at FROM collection_jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
        ready_at = datetime.fromisoformat(updated) - timedelta(seconds=2)
        database.connection.execute(
            "UPDATE collection_jobs SET updated_at = ? WHERE job_id = ?",
            (ready_at.astimezone(timezone.utc).isoformat(), job.job_id),
        )
    retry_claim = manager.claim(job_id=job.job_id, owner="worker-2")
    assert retry_claim is not None and retry_claim["attempts"] == 2
    manager.complete(job_id=job.job_id, owner="worker-2", result={"status": "failed", "reason": "permanent"})
    assert manager.claim(job_id=job.job_id, owner="worker-3") is None


def test_cancel_queued_job(tmp_path):
    path = str(tmp_path / "cancel.db")
    seeded(path)
    manager = JobManager(path)
    job = manager.start_job(source_id="limited", idempotency_key="cancel")
    assert manager.cancel(job.job_id)
    assert manager.get(job.job_id)["status"] == "cancelled"


def test_alert_cooldown_and_idempotency(tmp_path):
    path = str(tmp_path / "alerts.db")
    seeded(path)
    with Database(path) as database:
        variant = database.connection.execute("SELECT variant_id FROM variants LIMIT 1").fetchone()
    variant_id = variant[0]
    service = AlertService(path)
    service.upsert_rule(
        {
            "rule_id": "price-drop",
            "metric": "price_change",
            "threshold": "2.9",
            "currency": "RUB",
            "variant_id": variant_id,
            "cooldown_seconds": 1,
        }
    )
    first = service.evaluate()
    second = service.evaluate()
    time.sleep(1.05)
    third = service.evaluate()
    assert first["evaluated"] == 2 and first["created"] == 1
    assert second["created"] == 0
    assert third["created"] == 1
    with Database(path) as database:
        count = database.connection.execute(
            "SELECT COUNT(*) FROM alert_events WHERE rule_id = 'price-drop' AND variant_id = ?",
            (variant_id,),
        ).fetchone()[0]
    assert count == 2


def test_alert_source_scope_ignores_other_sources(tmp_path):
    path = str(tmp_path / "alert-scope.db")
    seeded(path)
    with Database(path) as database:
        repository = DataRepository(database)
        repository.upsert_source(Source(source_id="other", name="Other", base_url="https://fixture.test"))
        repository.persist_normalized(
            NormalizedProduct(
                source_id="other",
                identity_key="other-1",
                title="Other",
                canonical_url="https://fixture.test/other",
                variant_key="other-variant",
                sku="OTHER-1",
                attributes={},
                packaging={},
                unit_name="piece",
                unit_quantity=Decimal("1"),
                sale_price=PriceField("present", Decimal("100"), "RUB", 2),
                list_price=PriceField("present", Decimal("100"), "RUB", 2),
                unit_price=PriceField("present", Decimal("100"), "RUB", 2),
                availability=Availability.AVAILABLE,
                observed_at="2026-01-01T00:01:00.000+00:00",
                evidence={},
                raw_hash="other-1",
            )
        )
        variant = database.connection.execute(
            "SELECT variant_id FROM variants LIMIT 1"
        ).fetchone()[0]
    service = AlertService(path)
    service.upsert_rule(
        {
            "rule_id": "source-only",
            "metric": "price_change",
            "threshold": "1",
            "currency": "RUB",
            "source_id": "limited",
            "cooldown_seconds": 60,
        }
    )
    result = service.evaluate()
    with Database(path) as database:
        variants = database.connection.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
        events = database.connection.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0]
    assert variants == 2
    assert result["created"] == 1
    assert events == 1


def test_availability_alerts_use_confirmed_transitions_and_durable_cooldown(tmp_path, monkeypatch):
    path = str(tmp_path / "availability.db")
    seeded(path)
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(alerts_module, "datetime", FixedDatetime)
    service = AlertService(path)
    rule = {
        "rule_id": "availability", "metric": "availability", "threshold": "0",
        "currency": "RUB", "source_id": "limited", "cooldown_seconds": 60,
    }
    for metric in ("availability", "price_change"):
        with pytest.raises(ValueError, match="nonnegative"):
            service.upsert_rule({**rule, "metric": metric, "threshold": "-1"})
    service.upsert_rule(rule)
    service.upsert_rule({**rule, "rule_id": "price", "metric": "price_change", "threshold": "1000"})
    assert service.evaluate()["created"] == 0

    product = NormalizedProduct(
        source_id="limited", identity_key="same", title="Product",
        canonical_url="https://fixture.test/p/1", variant_key="variant", sku="SKU",
        attributes={}, packaging={"unit_name": "piece", "quantity": "1"},
        unit_name="piece", unit_quantity=Decimal("1"),
        sale_price=PriceField("present", Decimal("15"), "RUB", 2),
        list_price=PriceField.missing(), unit_price=PriceField.missing(),
        availability=Availability.UNAVAILABLE,
        observed_at="2026-01-04T00:00:00.000+00:00", evidence={}, raw_hash="unavailable",
    )
    with Database(path) as database:
        repository = DataRepository(database)
        repository.upsert_source(Source("other", "Other", "https://fixture.test"))
        repository.persist_normalized(replace(product, source_id="other", availability=Availability.AVAILABLE))
        repository.persist_normalized(replace(
            product, source_id="other", observed_at="2026-01-05T00:00:00.000+00:00", raw_hash="other-change",
        ))
        repository.persist_normalized(replace(
            product, sale_price=PriceField.unknown(), raw_hash="unconfirmed",
            observed_at="2026-01-03T12:00:00.000+00:00",
        ))
    assert service.evaluate()["created"] == 0
    with Database(path) as database:
        result = DataRepository(database).persist_normalized(product)
    assert service.evaluate()["created"] == 1
    assert service.evaluate()["created"] == 0
    with Database(path) as database:
        DataRepository(database).persist_normalized(replace(
            product, availability=Availability.AVAILABLE,
            observed_at="2026-01-05T00:00:00.000+00:00", raw_hash="available-again",
        ))
    assert service.evaluate()["created"] == 0
    now += timedelta(seconds=61)
    assert service.evaluate()["created"] == 1
    assert service.evaluate()["created"] == 0
    now += timedelta(seconds=61)
    assert service.evaluate()["created"] == 0

    with Database(path) as database:
        repository = DataRepository(database)
        for day, availability in ((6, Availability.UNKNOWN), (7, Availability.UNAVAILABLE), (8, Availability.AVAILABLE)):
            repository.persist_normalized(replace(
                product, sale_price=PriceField.missing(), availability=availability,
                observed_at=f"2026-01-0{day}T00:00:00.000+00:00", raw_hash=f"availability-{day}",
            ))
    assert service.evaluate()["created"] == 1
    now += timedelta(seconds=61)
    assert service.evaluate()["created"] == 0
    with Database(path) as database:
        events = database.connection.execute(
            "SELECT rule_id, variant_id, delta_amount, delta_currency FROM alert_events ORDER BY event_id"
        ).fetchall()
    assert [tuple(event) for event in events] == [
        ("availability", result.variant_id, "available -> unavailable", ""),
        ("availability", result.variant_id, "unavailable -> available", ""),
        ("availability", result.variant_id, "unavailable -> available", ""),
    ]
