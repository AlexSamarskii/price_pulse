from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pricepulse.data.normalize import NormalizedProduct, PriceField
from pricepulse.data.store import Database
from pricepulse.domain.models import (
    AlertRule,
    CollectionJob,
    DataQualityIssue,
    Schedule,
    ScraperVersion,
    Source,
    SourcePolicy,
)


_UUID_NAMESPACE = uuid.UUID("e7f3d5c8-5cd8-514f-a585-a19db6ec9667")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{uuid.uuid5(_UUID_NAMESPACE, ':'.join(parts))}"


@dataclass(frozen=True, slots=True)
class PersistenceResult:
    product_id: str
    variant_id: str
    observation_id: int | None
    action: str


@dataclass(frozen=True, slots=True)
class JobClaim:
    job_id: str
    attempts: int
    claimed: bool


class DataRepository:
    def __init__(self, database: Database):
        self.database = database

    def upsert_source(self, source: Source) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sources(source_id, name, base_url, active_version_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    name = excluded.name,
                    base_url = excluded.base_url,
                    active_version_id = excluded.active_version_id,
                    updated_at = excluded.updated_at
                """,
                (
                    source.source_id,
                    source.name,
                    source.base_url,
                    source.active_version_id,
                    source.created_at,
                    source.updated_at,
                ),
            )

    def upsert_policy(self, policy: SourcePolicy) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO source_policies(
                    policy_id, source_id, policy_json, policy_digest, live_enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    policy_json = excluded.policy_json,
                    policy_digest = excluded.policy_digest,
                    live_enabled = excluded.live_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    policy.policy_id,
                    policy.source_id,
                    _json(policy.policy),
                    policy.policy_digest,
                    int(policy.live_enabled),
                    policy.created_at,
                    policy.updated_at,
                ),
            )
            connection.execute("UPDATE sources SET current_policy_id = ? WHERE source_id = ?",
                               (policy.policy_id, policy.source_id))

    def upsert_scraper_version(self, version: ScraperVersion) -> None:
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT source_id, candidate_id, revision, status, goal, code_sha256,
                       dependencies_sha256, policy_digest, engine, provider_id, model,
                       fixture_verification, created_at
                FROM scraper_versions WHERE version_id = ?
                """,
                (version.version_id,),
            ).fetchone()
            if existing is not None:
                immutable_fields = (
                    "source_id", "candidate_id", "revision", "goal", "code_sha256",
                    "dependencies_sha256", "policy_digest", "engine", "provider_id",
                    "model", "created_at",
                )
                if any(existing[field] != getattr(version, field) for field in immutable_fields):
                    raise ValueError("scraper version metadata is immutable; create a new version")
                if existing["status"] == "active":
                    if version.status != "active" or version.fixture_verification != existing["fixture_verification"]:
                        raise ValueError("active scraper version cannot be changed by upsert")
                    return
                if version.status == "active":
                    raise ValueError("scraper version must be activated through the activation gate")
                connection.execute(
                    "UPDATE scraper_versions SET status = ?, fixture_verification = ? WHERE version_id = ?",
                    (version.status, version.fixture_verification, version.version_id),
                )
                return
            if version.status == "active":
                raise ValueError("scraper version must be activated through the activation gate")
            connection.execute(
                """
                INSERT INTO scraper_versions(
                    version_id, source_id, candidate_id, revision, status, goal,
                    code_sha256, dependencies_sha256, policy_digest, engine,
                    provider_id, model, fixture_verification, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.version_id,
                    version.source_id,
                    version.candidate_id,
                    version.revision,
                    version.status,
                    version.goal,
                    version.code_sha256,
                    version.dependencies_sha256,
                    version.policy_digest,
                    version.engine,
                    version.provider_id,
                    version.model,
                    version.fixture_verification,
                    version.created_at,
                ),
            )

    def upsert_schedule(self, schedule: Schedule) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO schedules(
                    schedule_id, source_id, schedule, max_concurrency,
                    requests_per_minute, enabled, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(schedule_id) DO UPDATE SET
                    schedule = excluded.schedule,
                    max_concurrency = excluded.max_concurrency,
                    requests_per_minute = excluded.requests_per_minute,
                    enabled = excluded.enabled
                """,
                (
                    schedule.schedule_id,
                    schedule.source_id,
                    schedule.schedule,
                    schedule.max_concurrency,
                    schedule.requests_per_minute,
                    int(schedule.enabled),
                    schedule.created_at,
                ),
            )

    def activate_scraper_version(self, version_id: str, *, job_id: str | None = None, lease_owner: str | None = None,
                                collection_config: str | None = None) -> bool:
        with self.database.transaction() as connection:
            if job_id is not None:
                self._require_lease(connection, job_id, lease_owner)
                if collection_config is not None:
                    self._require_collection(connection, job_id, version_id, collection_config)
            version = connection.execute(
                """
                SELECT source_id, status, fixture_verification
                FROM scraper_versions
                WHERE version_id = ?
                """,
                (version_id,),
            ).fetchone()
            if version is None or version["status"] != "validated" or version["fixture_verification"] != "passed":
                return False
            if job_id is not None and connection.execute(
                "SELECT source_id FROM collection_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()[0] != version["source_id"]:
                raise ValueError("job does not belong to scraper source")
            connection.execute(
                "UPDATE scraper_versions SET status = 'validated' WHERE source_id = ? AND status = 'active'",
                (version["source_id"],),
            )
            connection.execute(
                "UPDATE scraper_versions SET status = 'active' WHERE version_id = ?", (version_id,)
            )
            connection.execute(
                "UPDATE sources SET active_version_id = ?, updated_at = ? WHERE source_id = ?",
                (version_id, datetime.now(timezone.utc).isoformat(), version["source_id"]),
            )
            return True

    def get_or_create_job(self, job: CollectionJob) -> tuple[str, bool]:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT job_id, source_id FROM collection_jobs WHERE idempotency_key = ?",
                (job.idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["source_id"] != job.source_id:
                    raise ValueError("job idempotency key belongs to another source")
                return row["job_id"], False
            connection.execute(
                """
                INSERT INTO collection_jobs(
                    job_id, source_id, idempotency_key, status, scraper_version_id, lease_owner,
                    lease_expires_at, attempts, retry_budget, backoff_seconds, result_digest,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.source_id,
                    job.idempotency_key,
                    job.status,
                    job.scraper_version_id,
                    job.lease_owner,
                    job.lease_expires_at,
                    job.attempts,
                    job.retry_budget,
                    job.backoff_seconds,
                    job.result_digest,
                    job.created_at,
                    job.updated_at,
                ),
            )
            return job.job_id, True

    def claim_job(self, job_id: str, owner: str, lease_expires_at: str, now: str) -> JobClaim:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT source_id, status, attempts, retry_budget, lease_expires_at
                FROM collection_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return JobClaim(job_id, 0, False)
            attempts, budget = int(row["attempts"]), int(row["retry_budget"])
            expired = bool(row["lease_expires_at"] and row["lease_expires_at"] <= now)
            claimable = row["status"] == "queued" or (row["status"] == "running" and expired)
            if not claimable or attempts >= budget:
                return JobClaim(job_id, attempts, False)
            configured = connection.execute(
                "SELECT config_json, retry_after FROM collection_sources WHERE source_id = ?", (row["source_id"],),
            ).fetchone()
            if configured is not None and (
                not json.loads(configured["config_json"])["enabled"] or configured["retry_after"] > now
            ):
                return JobClaim(job_id, attempts, False)
            if configured is not None and connection.execute(
                "SELECT 1 FROM collection_jobs WHERE source_id = ? AND job_id != ? AND status = 'running' "
                "AND (lease_expires_at IS NULL OR lease_expires_at > ?)", (row["source_id"], job_id, now),
            ).fetchone() is not None:
                return JobClaim(job_id, attempts, False)
            updated = connection.execute(
                """
                UPDATE collection_jobs
                SET status = 'running', lease_owner = ?, lease_expires_at = ?, attempts = attempts + 1, updated_at = ?
                WHERE job_id = ? AND (
                    status = 'queued'
                    OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= ?))
                )
                """,
                (owner, lease_expires_at, now, job_id, now),
            ).rowcount
            return JobClaim(job_id, attempts + int(updated), bool(updated))

    def complete_job(
        self,
        job_id: str,
        lease_owner: str,
        result_digest: str,
        result_json: str,
        status: str,
        now: str,
    ) -> int:
        if status not in {"succeeded", "failed", "blocked"}:
            raise ValueError("completed job status is invalid")
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT result_id FROM job_results WHERE job_id = ? AND result_digest = ?",
                (job_id, result_digest),
            ).fetchone()
            if existing is not None:
                return int(existing["result_id"])
            authorized = connection.execute(
                """
                SELECT 1 FROM collection_jobs
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                (job_id, lease_owner, now),
            ).fetchone()
            if authorized is None:
                raise PermissionError("job completion requires the active unexpired lease")
            cursor = connection.execute(
                "INSERT INTO job_results(job_id, result_digest, result_json, created_at) VALUES (?, ?, ?, ?)",
                (job_id, result_digest, result_json, now),
            )
            transitioned = connection.execute(
                """
                UPDATE collection_jobs
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL, result_digest = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                (status, result_digest, now, job_id, lease_owner, now),
            )
            if transitioned.rowcount != 1:
                raise PermissionError("job completion requires the active unexpired lease")
            return int(cursor.lastrowid)

    def persist_normalized(
        self,
        product: NormalizedProduct,
        *,
        job_id: str | None = None,
        ingested_at: str | None = None,
        lease_owner: str | None = None,
        collection_config: str | None = None,
        collection_version_id: str | None = None,
    ) -> PersistenceResult:
        with self.database.transaction() as connection:
            if lease_owner is not None:
                self._require_lease(connection, job_id, lease_owner)
                if collection_config is not None:
                    self._require_collection(connection, job_id, collection_version_id, collection_config)
            if job_id is not None:
                job = connection.execute(
                    "SELECT source_id FROM collection_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if job is None or job["source_id"] != product.source_id:
                    raise ValueError("job does not belong to the normalized product source")

            product_id = _stable_id("product", product.source_id, product.identity_key)
            product_row = connection.execute(
                "SELECT title FROM products WHERE source_id = ? AND identity_key = ?",
                (product.source_id, product.identity_key),
            ).fetchone()
            if product_row is None:
                connection.execute(
                    """
                    INSERT INTO products(
                        product_id, source_id, identity_key, title, canonical_url,
                        attributes_json, review_status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                    """,
                    (
                        product_id,
                        product.source_id,
                        product.identity_key,
                        product.title,
                        product.canonical_url,
                        _json(product.attributes),
                        product.observed_at,
                        product.observed_at,
                    ),
                )
            elif product_row["title"] != product.title:
                return self._review_product(
                    product_id,
                    product.identity_key,
                    "same source identity has conflicting titles",
                )

            variant_id = _stable_id("variant", product_id, product.variant_key)
            connection.execute(
                """
                INSERT OR IGNORE INTO variants(
                    variant_id, product_id, variant_key, sku, attributes_json, packaging_json,
                    unit_name, unit_quantity, review_status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    variant_id,
                    product_id,
                    product.variant_key,
                    product.sku,
                    _json(product.attributes),
                    _json(product.packaging),
                    product.unit_name,
                    str(product.unit_quantity),
                    product.observed_at,
                    product.observed_at,
                ),
            )

            duplicate = connection.execute(
                "SELECT observation_id FROM observations WHERE variant_id = ? AND observed_at = ? AND raw_hash = ?",
                (variant_id, product.observed_at, product.raw_hash),
            ).fetchone()
            if duplicate is not None:
                return PersistenceResult(product_id, variant_id, int(duplicate["observation_id"]), "duplicate")

            if product.sale_price.state == "present" and product.sale_price.amount is not None and product.sale_price.amount <= 0:
                return self._review_product(product_id, product.identity_key, "non-positive price is invalid")
            if (
                product.sale_price.state == "present"
                and product.list_price.state == "present"
                and product.sale_price.currency != product.list_price.currency
            ):
                return self._review_product(product_id, product.identity_key, "sale and list prices have different currencies")

            cursor = connection.execute(
                """
                INSERT INTO observations(
                    variant_id, observed_at, ingested_at, status, price_state, price_amount,
                    price_currency, price_kind, price_precision, list_price_amount,
                    list_price_currency, unit_price_amount, unit_price_currency, unit_quantity,
                    unit_name, availability, job_id, raw_hash, evidence_json, provenance_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    variant_id,
                    product.observed_at,
                    ingested_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    "unknown" if product.sale_price.state == "unknown" else "confirmed",
                    product.sale_price.state,
                    str(product.sale_price.amount) if product.sale_price.amount is not None else None,
                    product.sale_price.currency,
                    "sale" if product.sale_price.state == "present" else None,
                    product.sale_price.precision,
                    str(product.list_price.amount) if product.list_price.amount is not None else None,
                    product.list_price.currency,
                    str(product.unit_price.amount) if product.unit_price.amount is not None else None,
                    product.unit_price.currency,
                    str(product.unit_price.unit_quantity) if product.unit_price.state == "present" else str(product.unit_quantity),
                    product.unit_price.unit_name if product.unit_price.state == "present" else product.unit_name,
                    product.availability.value,
                    job_id,
                    product.raw_hash,
                    _json(product.evidence),
                    _json({"source_id": product.source_id, "canonical_url": product.canonical_url}),
                ),
            )
            return PersistenceResult(product_id, variant_id, int(cursor.lastrowid), "created")

    @staticmethod
    def _require_lease(connection, job_id: str | None, owner: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        row = connection.execute(
            "SELECT 1 FROM collection_jobs WHERE job_id = ? AND status = 'running' "
            "AND lease_owner = ? AND lease_expires_at > ?", (job_id, owner, now),
        ).fetchone()
        if row is None:
            raise PermissionError("operation requires the active unexpired job lease")

    @staticmethod
    def _require_collection(connection, job_id, version_id, config_json):
        row = connection.execute(
            """SELECT cs.version_id, cs.config_json, cs.retry_after, p.policy_digest, v.policy_digest AS version_policy
               FROM collection_jobs AS j JOIN collection_sources AS cs ON cs.source_id = j.source_id
               JOIN sources AS s ON s.source_id = j.source_id
               JOIN source_policies AS p ON p.policy_id = s.current_policy_id
               JOIN scraper_versions AS v ON v.version_id = cs.version_id
               WHERE j.job_id = ?""", (job_id,),
        ).fetchone()
        if (row is None or row["version_id"] != version_id or row["config_json"] != config_json
                or not json.loads(row["config_json"])["enabled"] or row["policy_digest"] != row["version_policy"]
                or row["retry_after"] > datetime.now(timezone.utc).isoformat(timespec="milliseconds")):
            raise PermissionError("collection configuration or policy changed while job was running")

    def previous_price(self, variant_id: str, observed_at: str, currency: str | None) -> PriceField:
        connection = self.database.connection
        row = connection.execute(
            """
            SELECT price_amount, price_currency, unit_name, unit_quantity
            FROM observations
            WHERE variant_id = ? AND observed_at < ? AND status = 'confirmed'
              AND price_state = 'present' AND price_currency = ?
            ORDER BY observed_at DESC, observation_id DESC
            LIMIT 1
            """,
            (variant_id, observed_at, currency),
        ).fetchone()
        if row is None:
            return PriceField.missing()
        from decimal import Decimal

        return PriceField(
            "present",
            Decimal(row["price_amount"]),
            row["price_currency"],
            max(-Decimal(row["price_amount"]).as_tuple().exponent, 0),
            row["unit_name"],
            Decimal(row["unit_quantity"]),
        )

    def upsert_alert_rule(self, rule: AlertRule) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO alert_rules(
                    rule_id, metric, threshold, currency, unit_name, variant_id, source_id,
                    cooldown_seconds, enabled, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    metric = excluded.metric,
                    threshold = excluded.threshold,
                    currency = excluded.currency,
                    unit_name = excluded.unit_name,
                    variant_id = excluded.variant_id,
                    source_id = excluded.source_id,
                    cooldown_seconds = excluded.cooldown_seconds,
                    enabled = excluded.enabled
                """,
                (
                    rule.rule_id,
                    rule.metric,
                    rule.threshold,
                    rule.currency,
                    rule.unit_name,
                    rule.variant_id,
                    rule.source_id,
                    rule.cooldown_seconds,
                    int(rule.enabled),
                    rule.created_at,
                ),
            )

    def record_alert_event(
        self,
        *,
        rule_id: str,
        variant_id: str,
        observation_id: int,
        delta_amount: str,
        delta_currency: str,
        dedupe_key: str,
        occurred_at: str,
        cooldown_seconds: int,
    ) -> int | None:
        with self.database.transaction() as connection:
            latest = connection.execute(
                """
                SELECT occurred_at FROM alert_events
                WHERE rule_id = ? AND variant_id = ?
                ORDER BY occurred_at DESC LIMIT 1
                """,
                (rule_id, variant_id),
            ).fetchone()
            if latest is not None:
                last_time = datetime.fromisoformat(latest["occurred_at"].replace("Z", "+00:00"))
                current_time = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                if (current_time - last_time).total_seconds() < cooldown_seconds:
                    return None
            existing = connection.execute(
                "SELECT event_id FROM alert_events WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if existing is not None:
                return int(existing["event_id"])
            cursor = connection.execute(
                """
                INSERT INTO alert_events(
                    rule_id, observation_id, variant_id, delta_amount, delta_currency,
                    dedupe_key, occurred_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    observation_id,
                    variant_id,
                    delta_amount,
                    delta_currency,
                    dedupe_key,
                    occurred_at,
                ),
            )
            return int(cursor.lastrowid)

    def record_quality_issue(self, issue: DataQualityIssue) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO data_quality_issues(
                    issue_id, issue_type, details_json, source_id, job_id, variant_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue.issue_id,
                    issue.issue_type,
                    _json(issue.details),
                    issue.source_id,
                    issue.job_id,
                    issue.variant_id,
                    issue.created_at,
                ),
            )

    def _review_product(self, product_id: str, identity_key: str, reason: str) -> PersistenceResult:
        connection = self.database.connection
        issue_id = _stable_id("quality", "dedupe_conflict", product_id, reason)
        connection.execute(
            """
            INSERT OR IGNORE INTO data_quality_issues(
                issue_id, issue_type, details_json, variant_id, created_at
            )
            VALUES (?, 'dedupe_conflict', ?, NULL, ?)
            """,
            (
                issue_id,
                _json({"product_id": product_id, "identity_key": identity_key, "reason": reason}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return PersistenceResult(product_id, product_id, None, "review")
