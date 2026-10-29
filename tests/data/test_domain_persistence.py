from dataclasses import replace
from datetime import datetime, timezone

import pytest

from pricepulse.data.repository import DataRepository
from pricepulse.data.normalize import normalize_product
from pricepulse.data.store import Database
from pricepulse.collectors.source_onboarding import SourcePolicyError, onboard_source
from pricepulse.domain.models import Schedule, ScraperVersion, Source, SourcePolicy


@pytest.fixture()
def repository(tmp_path):
    with Database(tmp_path / "pricepulse.db") as database:
        database.initialize()
        repository = DataRepository(database)
        repository.upsert_source(Source("source", "Fixture", "https://fixture.test"))
        yield repository, database


def test_source_policy_and_schedule_round_trip(repository):
    repository, database = repository
    now = datetime.now(timezone.utc).isoformat()
    repository.upsert_policy(
        SourcePolicy("policy", "source", {"domains": ["fixture.test"]}, "digest", False, now, now)
    )
    repository.upsert_schedule(Schedule("schedule", "source", "*/5 * * * *", 1, 30, True, now))
    policy = database.connection.execute("SELECT policy_json FROM source_policies WHERE policy_id = 'policy'").fetchone()
    schedule = database.connection.execute("SELECT * FROM schedules WHERE schedule_id = 'schedule'").fetchone()
    assert policy is not None and "fixture.test" in policy["policy_json"]
    assert schedule is not None and schedule["requests_per_minute"] == 30


def test_activation_requires_passed_fixture_verification(repository):
    repository, database = repository
    version = ScraperVersion(
        "version",
        "source",
        "candidate",
        1,
        "validated",
        "extract product",
        "code-hash",
        "dependency-hash",
        "policy-digest",
        "html",
        fixture_verification="passed",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    repository.upsert_scraper_version(replace(version, fixture_verification="pending_offline_gate"))
    assert repository.activate_scraper_version("version") is False
    repository.upsert_scraper_version(version)
    assert repository.activate_scraper_version("version") is True
    source = database.connection.execute("SELECT active_version_id FROM sources WHERE source_id = 'source'").fetchone()
    status = database.connection.execute("SELECT status FROM scraper_versions WHERE version_id = 'version'").fetchone()
    assert source is not None and status is not None
    assert source["active_version_id"] == "version"
    assert status["status"] == "active"


def test_policy_controls_identity_and_version_metadata_is_immutable(repository):
    repository, database = repository
    policy = {
        "source_id": "source",
        "base_url": "https://fixture.test/item",
        "allowed_hosts": ["fixture.test"],
        "allowed_path_prefixes": ["/"],
        "robots_policy": "operator_authorized_fixture",
        "max_pages": 1,
        "requests_per_minute": 10,
        "max_concurrency": 1,
        "timeout_seconds": 10,
        "allowed_currencies": ["RUB"],
        "live_enabled": False,
    }
    onboarding = onboard_source({**policy, "drop_query_parameters": ["tracking"]}, "extract prices")
    assert onboarding.policy_digest != onboard_source(policy, "extract prices").policy_digest
    raw = {
        "title": "Tea",
        "url": "https://fixture.test/item?variant=red&tracking=x",
        "currency": "RUB",
        "price": "100",
        "observed_at": "2026-01-01T00:00:00Z",
        "drop_query_parameters": ["variant"],
    }
    untouched = normalize_product(raw, source_id="source")
    assert untouched.canonical_url.endswith("?variant=red&tracking=x")
    first = normalize_product(raw, source_id="source", source_policy=onboarding.policy)
    second = normalize_product(
        {**raw, "url": "https://fixture.test/item?variant=blue&tracking=x"},
        source_id="source", source_policy=onboarding.policy,
    )
    assert first.canonical_url == "https://fixture.test/item?variant=red"
    assert first.identity_key != second.identity_key
    assert first.variant_key != second.variant_key
    for dropped in ("variant", ["tracking", "TRACKING"], ["*"], ["x" * 65], [str(i) for i in range(33)]):
        with pytest.raises(SourcePolicyError):
            onboard_source({**policy, "drop_query_parameters": dropped}, "extract prices")
    with pytest.raises(ValueError, match="does not belong"):
        normalize_product(raw, source_id="another", source_policy=onboarding.policy)

    draft = ScraperVersion(
        "version", "source", "candidate", 1, "draft", "extract prices",
        "fixture-code-hash", "fixture-dependency-hash", onboarding.policy_digest,
        "html", created_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(ValueError, match="activation gate"):
        repository.upsert_scraper_version(replace(draft, status="active", fixture_verification="passed"))
    repository.upsert_scraper_version(draft)
    repository.upsert_scraper_version(draft)
    assert repository.activate_scraper_version("version") is False
    changes = {
        "source_id": "another", "candidate_id": "another", "revision": 2,
        "goal": "another", "code_sha256": "changed-code", "dependencies_sha256": "changed-deps",
        "policy_digest": "changed-policy", "engine": "another", "provider_id": "another",
        "model": "another", "created_at": "2026-01-02T00:00:00Z",
    }
    for field, value in changes.items():
        with pytest.raises(ValueError, match="immutable"):
            repository.upsert_scraper_version(
                replace(draft, **{field: value}, status="validated", fixture_verification="passed")
            )
    row = database.connection.execute(
        "SELECT code_sha256, status, fixture_verification FROM scraper_versions WHERE version_id = ?",
        (draft.version_id,),
    ).fetchone()
    assert tuple(row) == (draft.code_sha256, "draft", "pending_offline_gate")
    validated = replace(draft, status="validated", fixture_verification="passed")
    repository.upsert_scraper_version(validated)
    with pytest.raises(ValueError, match="immutable"):
        repository.upsert_scraper_version(replace(validated, code_sha256="changed-after-gate"))
    assert repository.activate_scraper_version("version")
    repository.upsert_scraper_version(replace(validated, status="active"))
    with pytest.raises(ValueError, match="cannot be changed"):
        repository.upsert_scraper_version(validated)
    assert database.connection.execute(
        "SELECT active_version_id FROM sources WHERE source_id = ?", ("source",),
    ).fetchone()[0] == "version"
