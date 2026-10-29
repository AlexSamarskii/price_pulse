from decimal import Decimal
from hashlib import sha256

import sqlite3

import pytest

from pricepulse.data.normalize import normalize_product
from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import CollectionJob, Source


RAW = {
    "title": "Tea",
    "url": "https://fixture.test/item?variant=red",
    "currency": "RUB",
    "price": "1 299,90",
    "observed_at": "2026-01-01T00:00:00Z",
}


@pytest.fixture()
def repository(tmp_path):
    with Database(tmp_path / "pricepulse.db") as database:
        database.initialize()
        repository = DataRepository(database)
        repository.upsert_source(Source("source", "Fixture", "https://fixture.test"))
        yield repository, database


def test_observation_and_job_writes_are_idempotent(repository):
    repository, _ = repository
    job_id, created = repository.get_or_create_job(CollectionJob("job", "source", "delivery-1", "queued"))
    assert created
    assert repository.get_or_create_job(CollectionJob("job-2", "source", "delivery-1", "queued")) == (job_id, False)
    first = repository.persist_normalized(normalize_product(RAW, source_id="source"), job_id=job_id)
    second = repository.persist_normalized(normalize_product(RAW, source_id="source"), job_id=job_id)
    assert (first.action, first.observation_id) == ("created", 1)
    assert (second.action, second.observation_id) == ("duplicate", 1)


def test_observations_are_append_only(repository):
    repository, database = repository
    job_id, _ = repository.get_or_create_job(CollectionJob("job", "source", "delivery-1", "queued"))
    result = repository.persist_normalized(normalize_product(RAW, source_id="source"), job_id=job_id)
    with pytest.raises(sqlite3.IntegrityError):
        database.connection.execute("UPDATE observations SET price_amount = '10'")
    assert result.observation_id == 1


def test_unknown_price_is_not_a_confirmed_observation(repository):
    repository, database = repository
    job_id, _ = repository.get_or_create_job(CollectionJob("job", "source", "delivery-1", "queued"))
    unknown_raw = {**RAW, "price": "unknown"}
    result = repository.persist_normalized(normalize_product(unknown_raw, source_id="source"), job_id=job_id)
    observation = database.connection.execute(
        """
        SELECT status, price_state, price_amount
        FROM observations
        WHERE observation_id = ?
        """,
        (result.observation_id,),
    ).fetchone()
    assert result.action == "created"
    assert observation is not None
    assert (observation["status"], observation["price_state"], observation["price_amount"]) == ("unknown", "unknown", None)
    assert repository.previous_price(result.variant_id, "2026-01-02T00:00:00Z", "RUB").state == "missing"


def test_job_claims_are_durable_and_restricted(repository):
    repository, _ = repository
    job_id, _ = repository.get_or_create_job(CollectionJob("job", "source", "delivery-1", "queued"))
    first = repository.claim_job(job_id, "worker-1", "2026-01-01T00:10:00Z", "2026-01-01T00:00:00Z")
    second = repository.claim_job(job_id, "worker-2", "2026-01-01T00:10:00Z", "2026-01-01T00:01:00Z")
    assert (first.claimed, first.attempts) == (True, 1)
    assert second.claimed is False
    digest = sha256(b"one").hexdigest()
    first_result = repository.complete_job(job_id, "worker-1", digest, '{"result": 1}', "succeeded", "2026-01-01T00:02:00Z")
    replay = repository.complete_job(job_id, "worker-1", digest, '{"result": 2}', "succeeded", "2026-01-01T00:03:00Z")
    assert first_result == replay
    assert repository.claim_job(job_id, "worker-1", "2026-01-01T00:10:00Z", "2026-01-01T00:03:00Z").claimed is False


def test_expired_lease_can_be_claimed(repository):
    repository, _ = repository
    job_id, _ = repository.get_or_create_job(CollectionJob("job", "source", "delivery-1", "queued"))
    repository.claim_job(job_id, "worker-1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
    recovered = repository.claim_job(job_id, "worker-2", "2026-01-01T00:10:00Z", "2026-01-01T00:01:00Z")
    assert (recovered.claimed, recovered.attempts) == (True, 2)


def test_unauthorized_worker_cannot_complete_leased_job(repository):
    repository, database = repository
    job_id, _ = repository.get_or_create_job(CollectionJob("job", "source", "delivery-1", "queued"))
    repository.claim_job(job_id, "worker-1", "2026-01-01T00:10:00Z", "2026-01-01T00:00:00Z")
    with pytest.raises(PermissionError):
        repository.complete_job(
            job_id,
            "worker-2",
            sha256(b"unauthorized").hexdigest(),
            '{"result": "unauthorized"}',
            "succeeded",
            "2026-01-01T00:01:00Z",
        )
    job = database.connection.execute(
        "SELECT status, lease_owner, result_digest FROM collection_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    results = database.connection.execute("SELECT result_id FROM job_results WHERE job_id = ?", (job_id,)).fetchall()
    assert job is not None
    assert (job["status"], job["lease_owner"], job["result_digest"]) == ("running", "worker-1", None)
    assert results == []


def test_stale_worker_cannot_complete_reclaimed_job(repository):
    repository, database = repository
    job_id, _ = repository.get_or_create_job(CollectionJob("job", "source", "delivery-1", "queued"))
    repository.claim_job(job_id, "worker-1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
    repository.claim_job(job_id, "worker-2", "2026-01-01T00:10:00Z", "2026-01-01T00:01:00Z")
    with pytest.raises(PermissionError):
        repository.complete_job(
            job_id,
            "worker-1",
            sha256(b"stale").hexdigest(),
            '{"result": "stale"}',
            "succeeded",
            "2026-01-01T00:02:00Z",
        )
    job = database.connection.execute(
        "SELECT status, lease_owner, lease_expires_at, result_digest FROM collection_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    results = database.connection.execute("SELECT result_id FROM job_results WHERE job_id = ?", (job_id,)).fetchall()
    assert job is not None
    assert (job["status"], job["lease_owner"], job["lease_expires_at"], job["result_digest"]) == (
        "running",
        "worker-2",
        "2026-01-01T00:10:00Z",
        None,
    )
    assert results == []
    repository.complete_job(
        job_id, "worker-2", sha256(b"recovered").hexdigest(), '{"result": "recovered"}', "succeeded", "2026-01-01T00:03:00Z"
    )


def test_previous_price_requires_currency_and_stays_within_variant(repository):
    repository, _ = repository
    job_id, _ = repository.get_or_create_job(CollectionJob("job", "source", "delivery-1", "queued"))
    result = repository.persist_normalized(normalize_product(RAW, source_id="source"), job_id=job_id)
    previous = repository.previous_price(result.variant_id, "2026-01-02T00:00:00Z", "RUB")
    assert previous.state == "present"
    assert str(previous.amount) == "1299.90"
    assert repository.previous_price(result.variant_id, "2026-01-02T00:00:00Z", "USD").state == "missing"


def test_alert_cooldown_and_dedupe(repository):
    from pricepulse.data.enrich import compute_alert_delta
    from pricepulse.data.normalize import PriceField
    from pricepulse.domain.models import AlertRule

    repository, _ = repository
    job_id, _ = repository.get_or_create_job(CollectionJob("job", "source", "delivery-1", "queued"))
    first = repository.persist_normalized(normalize_product(RAW, source_id="source"), job_id=job_id)
    second_raw = {**RAW, "price": "1399.90", "observed_at": "2026-01-02T00:00:00Z"}
    second = repository.persist_normalized(normalize_product(second_raw, source_id="source"), job_id=job_id)
    repository.upsert_alert_rule(AlertRule("rule", "price", "10", "RUB", cooldown_seconds=3600))
    delta = compute_alert_delta(
        PriceField("present", Decimal("1399.90"), "RUB", 2),
        PriceField("present", Decimal("1299.90"), "RUB", 2),
        Decimal("10"),
    )
    assert delta is not None
    event = repository.record_alert_event(
        rule_id="rule",
        variant_id=second.variant_id,
        observation_id=int(second.observation_id),
        delta_amount=delta["delta_amount"],
        delta_currency=delta["currency"],
        dedupe_key="delivery-1:rule:" + str(second.observation_id),
        occurred_at="2026-01-02T00:00:00Z",
        cooldown_seconds=3600,
    )
    assert event == 1
    suppressed = repository.record_alert_event(
        rule_id="rule",
        variant_id=second.variant_id,
        observation_id=int(second.observation_id),
        delta_amount=delta["delta_amount"],
        delta_currency=delta["currency"],
        dedupe_key="delivery-1:rule:second",
        occurred_at="2026-01-02T00:30:00Z",
        cooldown_seconds=3600,
    )
    assert suppressed is None
