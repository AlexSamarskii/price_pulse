from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import CollectionJob


MAX_IDEMPOTENCY_KEY_LENGTH = 256
MAX_LEASE_SECONDS = 3600
MIN_LEASE_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600
MAX_CONSECUTIVE_FAILURES = 5


@dataclass(frozen=True, slots=True)
class JobResult:
    job_id: str
    created: bool


class JobLimitExceeded(RuntimeError):
    pass


def utc_isoformat(seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    )


def parse_utc(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc)


class JobManager:
    def __init__(self, database_path: str):
        self.database_path = database_path

    def start_job(
        self,
        *,
        source_id: str,
        idempotency_key: str,
        scraper_version_id: str | None = None,
        retry_budget: int = 3,
        lease_seconds: int = 300,
        backoff_seconds: int = 30,
    ) -> JobResult:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a nonempty string")
        if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError("idempotency_key exceeds its length limit")
        if isinstance(retry_budget, bool) or not 0 <= retry_budget <= 10:
            raise ValueError("retry_budget must be between 0 and 10")
        if isinstance(lease_seconds, bool) or not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
            raise ValueError("lease_seconds must be between 30 and 3600")
        if isinstance(backoff_seconds, bool) or not 0 <= backoff_seconds <= MAX_BACKOFF_SECONDS:
            raise ValueError("backoff_seconds must be between 0 and 3600")
        now = utc_isoformat()

        with Database(self.database_path) as database:
            connection = database.connection
            with database.transaction():
                if connection.execute("SELECT 1 FROM sources WHERE source_id = ?", (source_id,)).fetchone() is None:
                    raise LookupError("source does not exist")
                existing = connection.execute(
                    "SELECT job_id, source_id FROM collection_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["source_id"] != source_id:
                        raise ValueError("job idempotency key belongs to another source")
                    return JobResult(existing["job_id"], False)

                policy = connection.execute(
                    """
                    SELECT policy_json FROM source_policies
                    WHERE source_id = ? ORDER BY created_at DESC, policy_id DESC LIMIT 1
                    """,
                    (source_id,),
                ).fetchone()
                if policy is not None:
                    policy_payload = json.loads(policy["policy_json"])
                    active_jobs = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM collection_jobs
                            WHERE source_id = ?
                              AND (
                                    status = 'queued'
                                    OR (status = 'running'
                                        AND (lease_expires_at IS NULL OR lease_expires_at > ?))
                              )
                            """,
                            (source_id, now),
                        ).fetchone()[0]
                    )
                    if active_jobs >= int(policy_payload["max_concurrency"]):
                        raise JobLimitExceeded("source concurrency limit reached")
                    recent = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM collection_jobs
                            WHERE source_id = ? AND created_at >= ?
                            """,
                            (source_id, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="milliseconds")),
                        ).fetchone()[0]
                    )
                    if recent >= int(policy_payload["requests_per_minute"]):
                        raise JobLimitExceeded("source rate limit reached")
                    failures = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM collection_jobs
                            WHERE source_id = ? AND status = 'failed' AND updated_at >= ?
                            """,
                            (source_id, (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="milliseconds")),
                        ).fetchone()[0]
                    )
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        raise JobLimitExceeded("source circuit breaker is open")
                if scraper_version_id is not None:
                    version = connection.execute(
                        """
                        SELECT 1 FROM scraper_versions
                        WHERE version_id = ? AND source_id = ? AND status = 'active'
                        """,
                        (scraper_version_id, source_id),
                    ).fetchone()
                    if version is None:
                        raise LookupError("scraper version is not active for this source")
                    active = connection.execute(
                        "SELECT active_version_id FROM sources WHERE source_id = ?",
                        (source_id,),
                    ).fetchone()
                    if active is None or active["active_version_id"] != scraper_version_id:
                        raise LookupError("scraper version is not the source's active version")

                job = CollectionJob(
                    job_id=f"job_{uuid.uuid4()}",
                    source_id=source_id,
                    idempotency_key=idempotency_key,
                    status="queued",
                    scraper_version_id=scraper_version_id,
                    retry_budget=retry_budget,
                    backoff_seconds=backoff_seconds,
                )
                connection.execute(
                    """
                    INSERT INTO collection_jobs(
                        job_id, source_id, idempotency_key, status, scraper_version_id,
                        lease_owner, lease_expires_at, attempts, retry_budget,
                        backoff_seconds, result_digest, created_at, updated_at
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
                return JobResult(job.job_id, True)

    def claim(
        self,
        *,
        job_id: str,
        owner: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        if not owner.strip() or len(owner) > 128:
            raise ValueError("job owner is invalid")
        if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
            raise ValueError("lease_seconds must be between 30 and 3600")
        now = utc_isoformat()
        expires = utc_isoformat(lease_seconds)
        with Database(self.database_path) as database:
            current = parse_utc(now)
            failed = database.connection.execute(
                """
                SELECT job_id, updated_at, backoff_seconds FROM collection_jobs
                WHERE status = 'failed' AND attempts < retry_budget
                """
            ).fetchall()
            for row in failed:
                ready_at = parse_utc(row["updated_at"]) + timedelta(seconds=int(row["backoff_seconds"]))
                if ready_at <= current:
                    database.connection.execute(
                        "UPDATE collection_jobs SET status = 'queued', updated_at = ? WHERE job_id = ?",
                        (now, row["job_id"]),
                    )
            claim = DataRepository(database).claim_job(job_id, owner, expires, now)
            if not claim.claimed:
                return None
            row = database.connection.execute(
                """
                SELECT job_id, source_id, status, scraper_version_id, attempts, retry_budget,
                       backoff_seconds, lease_expires_at
                FROM collection_jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            assert row is not None
            return dict(row)

    def complete(
        self,
        *,
        job_id: str,
        owner: str,
        result: dict[str, Any],
    ) -> str:
        result_json = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        digest = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        status = result.get("status")
        if status not in {"succeeded", "failed", "blocked"}:
            raise ValueError("completed job result must have succeeded, failed, or blocked status")
        with Database(self.database_path) as database:
            DataRepository(database).complete_job(
                job_id=job_id,
                lease_owner=owner,
                result_digest=digest,
                result_json=result_json,
                status=status,
                now=utc_isoformat(),
            )
        return digest

    def get(self, job_id: str) -> dict[str, Any] | None:
        with Database(self.database_path) as database:
            connection = database.connection
            row = connection.execute(
                """
                SELECT job_id, source_id, idempotency_key, status, scraper_version_id, lease_owner,
                       lease_expires_at, attempts, retry_budget, backoff_seconds, result_digest,
                       created_at, updated_at
                FROM collection_jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result_row = connection.execute(
                """
                SELECT result_digest, result_json, created_at FROM job_results
                WHERE job_id = ? ORDER BY result_id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if result_row is not None:
                result["result"] = json.loads(result_row["result_json"])
                result["result_created_at"] = result_row["created_at"]
        return result

    def list_jobs(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        with Database(self.database_path) as database:
            rows = database.connection.execute(
                """
                SELECT job_id, source_id, idempotency_key, status, scraper_version_id, lease_owner,
                       lease_expires_at, attempts, retry_budget, backoff_seconds, result_digest,
                       created_at, updated_at
                FROM collection_jobs ORDER BY created_at, job_id LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    def cancel(self, job_id: str) -> bool:
        now = utc_isoformat()
        with Database(self.database_path) as database:
            cursor = database.connection.execute(
                """
                UPDATE collection_jobs SET status = 'cancelled', lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (now, job_id),
            )
            return cursor.rowcount == 1

    def recover_expired(self, *, now: str | None = None) -> int:
        current = now or utc_isoformat()
        with Database(self.database_path) as database:
            connection = database.connection
            expired = connection.execute(
                """
                SELECT job_id, attempts, retry_budget, backoff_seconds FROM collection_jobs
                WHERE status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (current,),
            ).fetchall()
            for row in expired:
                if int(row["attempts"]) >= int(row["retry_budget"]):
                    connection.execute(
                        """
                        UPDATE collection_jobs SET status = 'failed', lease_owner = NULL,
                               lease_expires_at = NULL, result_digest = 'lease_exhausted', updated_at = ?
                        WHERE job_id = ? AND status = 'running' AND lease_expires_at <= ?
                        """,
                        (current, row["job_id"], current),
                    )
                else:
                    delay = max(int(row["backoff_seconds"]), 1)
                    connection.execute(
                        """
                        UPDATE collection_jobs SET status = 'queued', lease_owner = NULL,
                               lease_expires_at = NULL, updated_at = ?
                        WHERE job_id = ? AND status = 'running' AND lease_expires_at <= ?
                        """,
                        (utc_isoformat(delay), row["job_id"], current),
                    )
            return len(expired)
