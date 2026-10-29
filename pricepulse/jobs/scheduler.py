from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from pricepulse.data.store import Database
from pricepulse.jobs.manager import JobManager, JobLimitExceeded


class JobScheduler:
    def __init__(self, database_path: str):
        self.database_path = database_path

    def due_schedules(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        with Database(self.database_path) as database:
            rows = database.connection.execute(
                """
                SELECT s.schedule_id, s.source_id, s.schedule, s.max_concurrency,
                       s.requests_per_minute, src.active_version_id,
                       cs.version_id AS collection_version_id, cs.retry_after, cs.config_json
                FROM schedules AS s
                JOIN sources AS src ON src.source_id = s.source_id
                LEFT JOIN collection_sources AS cs ON cs.source_id = s.source_id
                WHERE s.enabled = 1
                """,
            ).fetchall()
        due: list[dict[str, Any]] = []
        for row in rows:
            if row["config_json"] is not None and (
                not json.loads(row["config_json"])["enabled"]
                or (row["retry_after"] and current < datetime.fromisoformat(row["retry_after"]))
            ):
                continue
            schedule = self._parse_interval(row["schedule"])
            schedule_id = row["schedule_id"]
            if schedule is None or current < self._next_run(schedule_id, row["source_id"], schedule):
                continue
            idempotency_key = f"schedule:{schedule_id}:{self._bucket(current, schedule)}"
            active = row["active_version_id"]
            if row["collection_version_id"] and row["collection_version_id"] != active:
                active = None
            try:
                job_result = JobManager(self.database_path).start_job(
                    source_id=row["source_id"], idempotency_key=idempotency_key, scraper_version_id=active,
                )
            except JobLimitExceeded:
                if row["collection_version_id"] is None:
                    raise
                continue
            due.append(
                {
                    "schedule_id": schedule_id,
                    "job_id": job_result.job_id,
                    "created": job_result.created,
                }
            )
        return due

    @staticmethod
    def _parse_interval(schedule: str) -> int | None:
        try:
            payload = json.loads(schedule)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("type") != "interval" or "seconds" not in payload:
            return None
        seconds = payload["seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 60 <= seconds <= 86400:
            return None
        return seconds

    def _next_run(self, schedule_id: str, source_id: str, seconds: int) -> datetime:
        with Database(self.database_path) as database:
            row = database.connection.execute(
                """
                SELECT MAX(created_at) FROM collection_jobs
                WHERE source_id = ? AND idempotency_key LIKE ?
                """,
                (source_id, f"schedule:{schedule_id}:%"),
            ).fetchone()
            if row is None or row[0] is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            created = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            return created.astimezone(timezone.utc) + timedelta(seconds=seconds)

    @staticmethod
    def _bucket(value: datetime, seconds: int) -> str:
        return str(int(value.timestamp()) // seconds)
