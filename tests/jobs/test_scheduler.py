from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import Schedule, Source, SourcePolicy, ScraperVersion
from pricepulse.jobs.manager import JobLimitExceeded
from pricepulse.jobs.manager import JobManager
from pricepulse.jobs.scheduler import JobScheduler


def test_scheduler_creates_one_job_per_due_bucket(tmp_path):
    path = str(tmp_path / "scheduler.db")
    with Database(path) as database:
        database.initialize()
        repository = DataRepository(database)
        repository.upsert_source(Source(source_id="scheduled", name="Scheduled", base_url="https://fixture.test"))
        version = ScraperVersion(
            version_id="version_1",
            source_id="scheduled",
            candidate_id="candidate_1",
            revision=1,
            status="validated",
            goal="price",
            code_sha256="0" * 64,
            dependencies_sha256="0" * 64,
            policy_digest="0" * 64,
            engine="httpx",
            fixture_verification="passed",
        )
        repository.upsert_scraper_version(version)
        assert repository.activate_scraper_version(version.version_id)
        repository.upsert_schedule(
            Schedule(
                schedule_id="schedule_1",
                source_id="scheduled",
                schedule='{"type":"interval","seconds":60}',
                max_concurrency=1,
                requests_per_minute=30,
            )
        )
    scheduler = JobScheduler(path)
    now = datetime.now(timezone.utc)
    first = scheduler.due_schedules(now=now)
    second = scheduler.due_schedules(now=now)
    assert len(first) == 1 and first[0]["created"]
    assert second == []


def test_scheduler_tracks_recency_per_schedule(tmp_path):
    path = str(tmp_path / "scheduler-multi.db")
    with Database(path) as database:
        database.initialize()
        repository = DataRepository(database)
        repository.upsert_source(Source(source_id="scheduled", name="Scheduled", base_url="https://fixture.test"))
        repository.upsert_schedule(
            Schedule(
                schedule_id="schedule_1",
                source_id="scheduled",
                schedule='{"type":"interval","seconds":60}',
                max_concurrency=1,
                requests_per_minute=30,
            )
        )
        repository.upsert_schedule(
            Schedule(
                schedule_id="schedule_2",
                source_id="scheduled",
                schedule='{"type":"interval","seconds":120}',
                max_concurrency=1,
                requests_per_minute=30,
            )
        )
    scheduler = JobScheduler(path)
    now = datetime.now(timezone.utc)
    scheduler.due_schedules(now=now)
    due = scheduler.due_schedules(now=now + timedelta(seconds=61))
    assert [item["schedule_id"] for item in due] == ["schedule_1"]


def test_scheduler_uses_job_policy_limits(tmp_path):
    path = str(tmp_path / "scheduler-limits.db")
    with Database(path) as database:
        database.initialize()
        repository = DataRepository(database)
        repository.upsert_source(
            Source(source_id="scheduled", name="Scheduled", base_url="https://fixture.test")
        )
        repository.upsert_policy(
            SourcePolicy(
                policy_id="policy_1",
                source_id="scheduled",
                policy={
                    "base_url": "https://fixture.test",
                    "allowed_hosts": ["fixture.test"],
                    "allowed_path_prefixes": ["/"],
                    "requests_per_minute": 30,
                    "max_concurrency": 1,
                    "timeout_seconds": 10,
                    "allowed_currencies": ["RUB"],
                    "live_enabled": False,
                },
                policy_digest="0" * 64,
                live_enabled=False,
            )
        )
        repository.upsert_schedule(
            Schedule(
                schedule_id="schedule_1",
                source_id="scheduled",
                schedule='{"type":"interval","seconds":60}',
                max_concurrency=1,
                requests_per_minute=30,
            )
        )
    running_job = JobManager(path).start_job(
        source_id="scheduled", idempotency_key="running", retry_budget=1
    )
    with Database(path) as database:
        DataRepository(database).claim_job(
            running_job.job_id,
            "test-owner",
            "9999-01-01T00:00:00.000+00:00",
            "2026-01-01T00:00:00.000+00:00",
        )
    with pytest.raises(JobLimitExceeded):
        JobScheduler(path).due_schedules(
            now=datetime.now(timezone.utc).replace(microsecond=0)
        )
