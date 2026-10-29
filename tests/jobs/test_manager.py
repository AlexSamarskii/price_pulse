from __future__ import annotations

from decimal import Decimal

import pytest

from pricepulse.data.normalize import (
    Availability,
    NormalizedProduct,
    PriceField,
)
from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import Source
from pricepulse.jobs.manager import JobManager, utc_isoformat


def seeded_products(path: str) -> None:
    with Database(path) as database:
        database.initialize()
        repository = DataRepository(database)
        repository.upsert_source(Source(source_id="jobs", name="Jobs", base_url="https://fixture.test"))
        for index, price in enumerate(("10.00", "12.00", "15.00"), start=1):
            repository.persist_normalized(
                NormalizedProduct(
                    source_id="jobs",
                    identity_key=f"product-{index}",
                    title=f"Product {index}",
                    canonical_url=f"https://fixture.test/p/{index}",
                    variant_key=f"variant-{index}",
                    sku=f"SKU-{index}",
                    attributes={},
                    packaging={},
                    unit_name="piece",
                    unit_quantity=Decimal("1"),
                    sale_price=PriceField("present", Decimal(price), "RUB", 2),
                    list_price=PriceField("present", Decimal(price), "RUB", 2),
                    unit_price=PriceField("present", Decimal(price), "RUB", 2),
                    availability=Availability.AVAILABLE,
                    observed_at=f"2026-01-0{index}T00:00:00.000+00:00",
                    evidence={},
                    raw_hash=f"hash-{index}",
                ),
                job_id=None,
            )


def test_job_idempotency_and_status(tmp_path):
    path = str(tmp_path / "jobs.db")
    seeded_products(path)
    manager = JobManager(path)
    first = manager.start_job(source_id="jobs", idempotency_key="same")
    second = manager.start_job(source_id="jobs", idempotency_key="same")
    assert first.created and not second.created
    assert first.job_id == second.job_id
    assert manager.get(first.job_id)["status"] == "queued"


def test_competing_claims_and_lease_guard(tmp_path):
    path = str(tmp_path / "jobs.db")
    seeded_products(path)
    manager = JobManager(path)
    job = manager.start_job(source_id="jobs", idempotency_key="lease")
    claimed = manager.claim(job_id=job.job_id, owner="worker-a")
    assert claimed is not None and claimed["attempts"] == 1
    assert manager.claim(job_id=job.job_id, owner="worker-b") is None
    with pytest.raises(PermissionError):
        manager.complete(job_id=job.job_id, owner="worker-b", result={"status": "succeeded"})
    digest = manager.complete(job_id=job.job_id, owner="worker-a", result={"status": "succeeded"})
    stored = manager.get(job.job_id)
    assert stored["status"] == "succeeded"
    assert stored["result"]["status"] == "succeeded"
    assert digest == stored["result_digest"]


def test_expired_lease_recovery_and_exhaustion(tmp_path):
    path = str(tmp_path / "jobs.db")
    seeded_products(path)
    manager = JobManager(path)
    job = manager.start_job(source_id="jobs", idempotency_key="expired", retry_budget=1)
    claimed = manager.claim(job_id=job.job_id, owner="worker-a", lease_seconds=30)
    assert claimed is not None
    assert manager.recover_expired(now=utc_isoformat(120)) == 1
    assert manager.get(job.job_id)["status"] == "failed"


def test_retry_backoff_and_rate_fields_persist(tmp_path):
    path = str(tmp_path / "jobs.db")
    seeded_products(path)
    manager = JobManager(path)
    job = manager.start_job(
        source_id="jobs",
        idempotency_key="retry",
        retry_budget=2,
        backoff_seconds=45,
        lease_seconds=120,
    )
    stored = manager.get(job.job_id)
    assert stored["retry_budget"] == 2
    assert stored["backoff_seconds"] == 45
    assert stored["status"] == "queued"
