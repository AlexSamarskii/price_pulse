from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from pricepulse.api.alerts import AlertService
from pricepulse.api.routes import list_history, list_products
from pricepulse.collectors.catalog import PacedBroker
from pricepulse.data.normalize import normalize_product
from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.jobs.manager import JobManager
from pricepulse.jobs.scheduler import JobScheduler
from pricepulse.jobs.worker import CollectionWorker
from tests.collectors.test_catalog import card, config, listing, url


def broker_factory(state):
    def factory(policy, before_send):
        def handle(request):
            assert request.headers["host"] == "limestore.com"
            assert not request.url.query
            state.setdefault("requests", []).append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /ru_ru/cart\nDisallow: /*?*\n")
            if request.url.path == "/ru_ru/catalog/women_new":
                return httpx.Response(200, text=listing())
            if request.url.path == "/ru_ru/product/101-model_101_black":
                return httpx.Response(state.get("first_status", 200), text=card(101, state.get("price", 3500)))
            if request.url.path == "/ru_ru/product/102-model_102_black":
                return httpx.Response(state.get("second_status", 200), text=card(102))
            raise AssertionError("unapproved mock request: " + str(request.url))
        return PacedBroker(policy, before_send, transport=httpx.MockTransport(handle), resolver=lambda _: "93.184.216.34")
    return factory


def test_collection_two_scheduled_cycles_survive_restart_without_duplicate_alerts(tmp_path, runtime_runner, monkeypatch):
    path = str(tmp_path / "catalog.db")
    state = {}
    worker = CollectionWorker(path, [config()], runner=runtime_runner, broker_factory=broker_factory(state))
    first = worker.tick()
    assert len(first) == 1 and first[0]["status"] == "succeeded"
    assert first[0]["collected_products"] == 2 and first[0]["variants"] == 4
    assert len(first[0]["requests"]) == 4
    alerts = AlertService(path)
    alerts.upsert_rule({"rule_id": "change", "source_id": "limestore_ru", "metric": "price_change",
                        "threshold": "1", "currency": "RUB", "cooldown_seconds": 60})
    assert alerts.evaluate()["created"] == 0
    state["price"] = 3000
    second_worker = CollectionWorker(path, [config()], runner=runtime_runner, broker_factory=broker_factory(state))
    assert second_worker.sources["limestore_ru"][1]["version_id"] == worker.sources["limestore_ru"][1]["version_id"]
    assert len(JobScheduler(path).due_schedules(now=datetime.now(timezone.utc) + timedelta(seconds=61))) == 1
    second = second_worker.tick()
    assert len(second) == 1 and second[0]["status"] == "succeeded"
    assert second[0]["observations_created"] == 4 and second[0]["alerts"]["created"] == 2
    rows = list_products(path, source_id="limestore_ru", limit=100, offset=0)
    assert len({p["product_id"] for p in rows}) == 2
    assert len(rows) == 4
    history = list_history(path, variant_id=rows[0]["variant_id"], limit=10, offset=0)
    assert len(history) == 2
    assert {Decimal(p["price_amount"]) for p in history} == {Decimal(3500), Decimal(3000)}
    with Database(path) as db:
        assert db.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 8
        assert db.connection.execute("SELECT COUNT(*) FROM scraper_versions").fetchone()[0] == 1
        assert db.connection.execute("SELECT COUNT(*) FROM collection_jobs WHERE status = 'succeeded'").fetchone()[0] == 2
        db.connection.execute("UPDATE alert_events SET dedupe_key = dedupe_key || ':legacy-bucket'")
    import pricepulse.api.alerts as alert_module
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(alert_module, "datetime", Later)
    assert AlertService(path).evaluate()["created"] == 0
    before = len(state["requests"])
    assert second_worker.tick() == []
    assert len(state["requests"]) == before


def test_partial_block_preserves_data_and_durable_cooldown(tmp_path, runtime_runner):
    path = str(tmp_path / "blocked.db")
    state = {"second_status": 429}
    worker = CollectionWorker(path, [config()], runner=runtime_runner, broker_factory=broker_factory(state))
    result = worker.tick()[0]
    assert result["status"] == "blocked" and result["collected_products"] == 1
    assert result["observations_created"] == 2
    with Database(path) as db:
        assert db.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        assert db.connection.execute("SELECT active_version_id FROM sources").fetchone()[0] is None
        assert db.connection.execute("SELECT status FROM scraper_versions").fetchone()[0] == "draft"
        assert db.connection.execute("SELECT last_error FROM collection_sources").fetchone()[0]
    before = len(state["requests"])
    restarted = CollectionWorker(path, [config()], runner=runtime_runner, broker_factory=broker_factory(state))
    assert restarted.tick() == [] and len(state["requests"]) == before


def test_expired_owner_cannot_send_persist_or_activate(tmp_path):
    from tests.collectors.test_catalog import payload
    path = str(tmp_path / "fence.db")
    worker = CollectionWorker(path, [config()])
    job = JobManager(path).start_job(source_id="limestore_ru", idempotency_key="fence")
    assert JobManager(path).claim(job_id=job.job_id, owner=worker.owner)
    version = worker.sources["limestore_ru"][1]
    with Database(path) as db:
        db.connection.execute("UPDATE collection_jobs SET lease_expires_at = '2000-01-01T00:00:00.000+00:00'")
        with pytest.raises(PermissionError):
            DataRepository(db).activate_scraper_version(version["version_id"], job_id=job.job_id, lease_owner=worker.owner)
    with pytest.raises(PermissionError):
        worker._pace(job.job_id, "limestore_ru", version["version_id"], float("inf"))
    raw = {"source": "limestore_ru", "canonical_url": url(101), "sku": "101", "title": "Product 101",
           "price": "3500", "currency": "RUB", "availability": "available", "attributes": {},
           "evidence": {}, "observed_at": "2026-10-29T20:00:00+00:00"}
    normalized = normalize_product(raw, source_id="limestore_ru", source_policy=config()["policy"])
    with Database(path) as db:
        with pytest.raises(PermissionError):
            DataRepository(db).persist_normalized(normalized, job_id=job.job_id, lease_owner=worker.owner)
        assert db.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_retry_and_scheduled_jobs_cannot_claim_same_source_concurrently(tmp_path):
    path = str(tmp_path / "source-lease.db")
    CollectionWorker(path, [config()])
    manager = JobManager(path)
    retry = manager.start_job(source_id="limestore_ru", idempotency_key="retry", backoff_seconds=0)
    assert manager.claim(job_id=retry.job_id, owner="old-owner")
    manager.complete(job_id=retry.job_id, owner="old-owner", result={"status": "failed"})
    due = manager.start_job(source_id="limestore_ru", idempotency_key="new-schedule")
    assert manager.claim(job_id=retry.job_id, owner="retry-owner")
    assert manager.claim(job_id=due.job_id, owner="other-worker") is None
    manager.complete(job_id=retry.job_id, owner="retry-owner", result={"status": "succeeded"})
    assert manager.claim(job_id=due.job_id, owner="other-worker")


def test_policy_restore_and_config_changes_fence_old_workers(tmp_path):
    from copy import deepcopy
    from pricepulse.collectors.catalog import canonical_json
    path = str(tmp_path / "config-lifecycle.db")
    original = config()
    first = CollectionWorker(path, [original])
    changed = deepcopy(original)
    changed["policy"]["requests_per_minute"] = 30
    CollectionWorker(path, [changed])
    restored = CollectionWorker(path, [original])
    manager = JobManager(path)
    job = manager.start_job(source_id="limestore_ru", idempotency_key="restored")
    assert manager.claim(job_id=job.job_id, owner=restored.owner)
    version = restored.sources["limestore_ru"][1]
    with Database(path) as db:
        restored._fence(db.connection, job.job_id, "limestore_ru", version["version_id"])
        pointer = db.connection.execute("SELECT p.policy_digest FROM sources AS s JOIN source_policies AS p "
                                       "ON p.policy_id=s.current_policy_id").fetchone()[0]
        assert pointer == version["policy_digest"]
    changed = deepcopy(original)
    changed["max_products"] = 1
    replacement = CollectionWorker(path, [changed])
    assert replacement.sources["limestore_ru"][1]["version_id"] == version["version_id"]
    with Database(path) as db:
        with pytest.raises(PermissionError, match="configuration"):
            restored._fence(db.connection, job.job_id, "limestore_ru", version["version_id"])
        with pytest.raises(PermissionError, match="configuration"):
            DataRepository(db).activate_scraper_version(version["version_id"], job_id=job.job_id,
                lease_owner=restored.owner, collection_config=canonical_json(restored.sources["limestore_ru"][0]))
        raw = {"source": "limestore_ru", "canonical_url": url(101), "sku": "101", "title": "P",
               "price": "3500", "currency": "RUB", "availability": "available", "attributes": {},
               "evidence": {}, "observed_at": "2026-10-29T20:00:00+00:00"}
        normalized = normalize_product(raw, source_id="limestore_ru", source_policy=original["policy"])
        with pytest.raises(PermissionError, match="configuration"):
            DataRepository(db).persist_normalized(normalized, job_id=job.job_id, lease_owner=restored.owner,
                collection_config=canonical_json(restored.sources["limestore_ru"][0]), collection_version_id=version["version_id"])
        assert db.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_pacing_reservation_survives_restart_and_stop(tmp_path):
    import time
    path = str(tmp_path / "pacing.db")
    original = CollectionWorker(path, [config()])
    manager = JobManager(path)
    job = manager.start_job(source_id="limestore_ru", idempotency_key="pace")
    assert manager.claim(job_id=job.job_id, owner=original.owner)
    version = original.sources["limestore_ru"][1]["version_id"]
    original._pace(job.job_id, "limestore_ru", version, time.monotonic() + 10)
    with Database(path) as db:
        reserved = db.connection.execute("SELECT next_request_at FROM collection_sources").fetchone()[0]
    restarted = CollectionWorker(path, [config()])
    with Database(path) as db:
        assert db.connection.execute("SELECT next_request_at FROM collection_sources").fetchone()[0] == reserved
    original.stop.set()
    with pytest.raises(InterruptedError):
        original._pace(job.job_id, "limestore_ru", version, time.monotonic() + 10)


def test_cli_once_honors_disabled_source_without_network(tmp_path, monkeypatch):
    import sys
    from pricepulse.__main__ import main
    cfg = config()
    cfg["enabled"] = False
    source_file = tmp_path / "sources.json"
    source_file.write_text(json.dumps({"sources": [cfg]}))
    path = str(tmp_path / "cli.db")
    monkeypatch.setattr(sys, "argv", ["pricepulse", "--database", path, "--config", str(source_file), "--once"])
    assert main() == 0
    with Database(path) as db:
        assert db.connection.execute("SELECT COUNT(*) FROM collection_jobs").fetchone()[0] == 0


def test_blocked_retry_stops_an_already_selected_scheduled_job(tmp_path):
    from types import SimpleNamespace
    from pricepulse.runtime import ExecutionResult
    path = str(tmp_path / "cooldown-queue.db")
    state = {"first_status": 429}
    runner = SimpleNamespace(runtime_available=lambda: True, run=lambda request: ExecutionResult(
        status="completed", exit_code=0, stdout=json.dumps({"product_urls": [url(101)], "next_urls": []}),
        stderr="", timed_out=False, cleanup_completed=True))
    worker = CollectionWorker(path, [config()], runner=runner, broker_factory=broker_factory(state))
    manager = JobManager(path)
    retry = manager.start_job(source_id="limestore_ru", idempotency_key="old-retry", backoff_seconds=0)
    assert manager.claim(job_id=retry.job_id, owner="first-owner")
    manager.complete(job_id=retry.job_id, owner="first-owner", result={"status": "failed"})
    scheduled = manager.start_job(source_id="limestore_ru", idempotency_key="new-scheduled")
    results = worker.tick()
    assert len(results) == 1 and results[0]["status"] == "blocked"
    assert len(state["requests"]) == 3
    assert manager.get(scheduled.job_id)["status"] == "queued"
    assert manager.claim(job_id=scheduled.job_id, owner="third-owner") is None
    assert worker.tick() == [] and len(state["requests"]) == 3


def test_stale_retry_selector_cannot_replace_a_running_lease(tmp_path, monkeypatch):
    import pricepulse.jobs.manager as module
    from pricepulse.jobs.manager import utc_isoformat
    path = str(tmp_path / "retry-selector.db")
    CollectionWorker(path, [config()])
    manager = JobManager(path)
    job = manager.start_job(source_id="limestore_ru", idempotency_key="retry-race", backoff_seconds=0)
    assert manager.claim(job_id=job.job_id, owner="initial")
    manager.complete(job_id=job.job_id, owner="initial", result={"status": "failed"})
    raced = []
    class Connection:
        def __init__(self, inner):
            self.inner = inner
        def execute(self, sql, parameters=()):
            if "UPDATE collection_jobs SET status = 'queued'" in sql and not raced:
                raced.append(True)
                with Database(path) as other:
                    other.connection.execute("UPDATE collection_jobs SET status='running', lease_owner='winner', "
                        "lease_expires_at=?, attempts=attempts+1 WHERE job_id=?", (utc_isoformat(300), job.job_id))
            return self.inner.execute(sql, parameters)
    class InterleavedDatabase:
        def __init__(self, filename):
            self.inner = Database(filename)
        def __enter__(self):
            self.inner.__enter__()
            self.connection = Connection(self.inner.connection)
            return self
        def __exit__(self, *args):
            return self.inner.__exit__(*args)
        def transaction(self):
            return self.inner.transaction()
    monkeypatch.setattr(module, "Database", InterleavedDatabase)
    assert manager.claim(job_id=job.job_id, owner="stale-selector") is None
    with Database(path) as db:
        row = db.connection.execute("SELECT status, lease_owner FROM collection_jobs WHERE job_id=?", (job.job_id,)).fetchone()
        assert tuple(row) == ("running", "winner")
