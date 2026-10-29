from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from pricepulse.api.alerts import AlertService
from pricepulse.collectors.catalog import CatalogCollector, CollectionBlocked, CollectionError, PacedBroker, canonical_json
from pricepulse.collectors.capture_sanitizer import sanitize_for_log
from pricepulse.data.normalize import normalize_product
from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.egress.broker import EgressViolation
from pricepulse.jobs.collection import register_collection
from pricepulse.jobs.manager import JobManager, utc_isoformat
from pricepulse.jobs.scheduler import JobScheduler
from pricepulse.runtime import RuntimeUnavailableError, SandboxedContainerRunner


class CollectionWorker:
    def __init__(self, database_path, configs, *, runner=None, broker_factory=None, stop=None):
        self.database_path = str(database_path)
        self.sources = {config["policy"]["source_id"]: register_collection(self.database_path, config) for config in configs}
        self.runner = runner or SandboxedContainerRunner()
        self.broker_factory = broker_factory or PacedBroker
        self.stop = stop or threading.Event()
        self.owner = "catalog_" + uuid.uuid4().hex
        self.manager = JobManager(self.database_path)

    def _fence(self, connection, job_id, source_id, version_id):
        DataRepository._require_lease(connection, job_id, self.owner)
        DataRepository._require_collection(connection, job_id, version_id, canonical_json(self.sources[source_id][0]))

    def _check(self, job_id, source_id, version_id, deadline):
        if self.stop.is_set():
            raise InterruptedError("collection worker stopped")
        if time.monotonic() >= deadline:
            raise CollectionError("collection run deadline exceeded")
        with Database(self.database_path) as database:
            self._fence(database.connection, job_id, source_id, version_id)

    def _pace(self, job_id, source_id, version_id, deadline):
        config, _ = self.sources[source_id]
        interval = 60 / config["policy"]["requests_per_minute"] + 0.01
        while True:
            self._check(job_id, source_id, version_id, deadline)
            with Database(self.database_path) as database:
                with database.transaction() as connection:
                    self._fence(connection, job_id, source_id, version_id)
                    row = connection.execute("SELECT next_request_at FROM collection_sources WHERE source_id = ?", (source_id,)).fetchone()
                    now = datetime.now(timezone.utc)
                    delay = max(0, (datetime.fromisoformat(row[0]) - now).total_seconds()) if row[0] else 0
                    if delay == 0:
                        connection.execute("UPDATE collection_sources SET next_request_at = ? WHERE source_id = ?",
                            ((now + timedelta(seconds=interval)).isoformat(timespec="milliseconds"), source_id))
            if delay == 0:
                break
            if self.stop.wait(min(delay, 30)):
                raise InterruptedError("collection worker stopped during pacing")
        self._check(job_id, source_id, version_id, deadline)

    def _persist(self, products, job_id, source_id, version):
        normalized = [normalize_product(row, source_id=source_id, source_policy=version["policy"]) for row in products]
        if any(row.sale_price.state != "present" or row.availability.value not in {"available", "unavailable"} for row in normalized):
            raise CollectionError("product has unconfirmed price or availability")
        if len({p.variant_key for p in normalized}) != len(normalized):
            raise CollectionError("product contains duplicate normalized variants")
        results = []
        with Database(self.database_path) as database:
            repository = DataRepository(database)
            for row in normalized:
                self._fence(database.connection, job_id, source_id, version["version_id"])
                persisted = repository.persist_normalized(row, job_id=job_id, lease_owner=self.owner,
                    collection_config=canonical_json(self.sources[source_id][0]), collection_version_id=version["version_id"])
                if persisted.action not in {"created", "duplicate"}:
                    raise CollectionError("product identity requires manual review")
                results.append(persisted)
        return results

    def _activate(self, job_id, source_id, version):
        with Database(self.database_path) as database:
            with database.transaction() as connection:
                self._fence(connection, job_id, source_id, version["version_id"])
                connection.execute(
                    "UPDATE scraper_versions SET status = 'validated', fixture_verification = 'passed' "
                    "WHERE version_id = ? AND status = 'draft'", (version["version_id"],),
                )
            active = database.connection.execute("SELECT active_version_id FROM sources WHERE source_id = ?", (source_id,)).fetchone()[0]
            if active != version["version_id"]:
                if not DataRepository(database).activate_scraper_version(version["version_id"], job_id=job_id,
                    lease_owner=self.owner, collection_config=canonical_json(self.sources[source_id][0])):
                    raise CollectionError("validated collection version could not activate")
            with database.transaction() as connection:
                self._fence(connection, job_id, source_id, version["version_id"])
                connection.execute("UPDATE collection_jobs SET scraper_version_id = ? WHERE job_id = ?",
                                   (version["version_id"], job_id))

    def _finish(self, job_id, source_id, version, result):
        with Database(self.database_path) as database:
            with database.transaction() as connection:
                DataRepository._require_lease(connection, job_id, self.owner)
                success = result["status"] == "succeeded"
                delay = 0 if success else self.sources[source_id][0]["cooldown_seconds"]
                current = connection.execute("SELECT config_json FROM collection_sources WHERE source_id = ?", (source_id,)).fetchone()
                if current[0] != canonical_json(self.sources[source_id][0]):
                    result["status"] = "blocked"
                    result["errors"].append({"reason": "collection configuration changed before completion"})
                else:
                    connection.execute(
                        "UPDATE collection_sources SET retry_after = ?, last_error = ?, "
                        "last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END, updated_at = ? WHERE source_id = ?",
                        (utc_isoformat(delay), None if success else canonical_json(result["errors"]),
                         success, utc_isoformat(), utc_isoformat(), source_id),
                    )
        self.manager.complete(job_id=job_id, owner=self.owner, result=result)

    def execute(self, job):
        source_id, job_id = job["source_id"], job["job_id"]
        config, version = self.sources[source_id]
        if self.manager.claim(job_id=job_id, owner=self.owner, lease_seconds=3600) is None:
            return None
        deadline = time.monotonic() + 3300
        result = {"status": "failed", "source_id": source_id, "version_id": version["version_id"],
                  "product_urls": [], "collected_products": 0, "variants": 0, "observations_created": 0,
                  "observations_duplicate": 0, "errors": [], "requests": []}
        broker = None
        try:
            self._check(job_id, source_id, version["version_id"], deadline)
            if job["scraper_version_id"] not in {None, version["version_id"]}:
                raise CollectionError("queued job references an obsolete capture-specific scraper")
            if not self.runner.runtime_available():
                raise RuntimeUnavailableError("hardened runtime unavailable; no host execution fallback")
            broker = self.broker_factory(config["policy"], lambda: self._pace(job_id, source_id, version["version_id"], deadline))
            collector = CatalogCollector(config, version, self.runner, broker)
            urls = collector.discover(job_id)
            result["product_urls"] = urls
            for url in urls:
                self._check(job_id, source_id, version["version_id"], deadline)
                try:
                    products = collector.extract(url, job_id)
                    self._check(job_id, source_id, version["version_id"], deadline)
                    rows = self._persist(products, job_id, source_id, version)
                    result["collected_products"] += 1
                    result["variants"] += len(rows)
                    result["observations_created"] += sum(r.action == "created" for r in rows)
                    result["observations_duplicate"] += sum(r.action == "duplicate" for r in rows)
                except CollectionBlocked:
                    raise
                except CollectionError as exc:
                    result["errors"].append({"url": url, "reason": str(exc)})
            if result["errors"]:
                result["status"] = "failed"
            else:
                self._check(job_id, source_id, version["version_id"], deadline)
                self._activate(job_id, source_id, version)
                result["status"] = "succeeded"
        except (CollectionBlocked, EgressViolation, RuntimeUnavailableError) as exc:
            result["status"] = "blocked"
            result["errors"].append({"reason": str(exc)})
        except (CollectionError, httpx.HTTPError, ValueError, InterruptedError) as exc:
            result["errors"].append({"reason": str(exc)})
        except PermissionError as exc:
            result["status"] = "blocked"
            result["errors"].append({"reason": str(exc)})
        finally:
            if broker is not None:
                result["requests"] = broker.audit
                broker.close()
        try:
            self._finish(job_id, source_id, version, result)
        except PermissionError:
            return {"status": "lease_lost", "job_id": job_id}
        result["alerts"] = AlertService(self.database_path).evaluate()
        print(canonical_json(sanitize_for_log({"event": "collection_completed", "job_id": job_id, **result})), flush=True)
        return result

    def tick(self):
        self.manager.recover_expired()
        JobScheduler(self.database_path).due_schedules()
        now = utc_isoformat()
        with Database(self.database_path) as database:
            rows = database.connection.execute(
                """SELECT j.job_id, j.source_id, j.scraper_version_id FROM collection_jobs AS j
                   JOIN collection_sources AS cs ON cs.source_id = j.source_id
                   WHERE cs.retry_after <= ? AND j.updated_at <= ? AND (
                     j.status = 'queued' OR (j.status = 'failed' AND j.attempts < j.retry_budget
                       AND julianday(j.updated_at) + j.backoff_seconds / 86400.0 <= julianday(?)))
                   ORDER BY j.created_at, j.job_id LIMIT 10""", (now, now, now),
            ).fetchall()
        results = []
        for row in rows:
            if row["source_id"] in self.sources and self.sources[row["source_id"]][0]["enabled"] and not self.stop.is_set():
                result = self.execute(dict(row))
                if result is not None:
                    results.append(result)
        return results

    def run_forever(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                print(canonical_json(sanitize_for_log({"event": "collection_worker_error", "error": str(exc)})), flush=True)
            self.stop.wait(5)
