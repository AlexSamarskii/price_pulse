from __future__ import annotations

import json
import ipaddress
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pricepulse.api.alerts import AlertService, ResourceNotFound
from pricepulse.api.export import HISTORY_CSV_FIELDS, serialize_csv_rows, serialize_json_rows
from pricepulse.collectors.source_onboarding import onboard_source
from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import Schedule, Source, SourcePolicy
from pricepulse.jobs.manager import JobLimitExceeded, JobManager


MAX_BODY_BYTES = 1_000_000
MAX_LIMIT = 100
PROJECT_DIR = Path(__file__).resolve().parents[1]
DASHBOARD_TEMPLATE = PROJECT_DIR / "web" / "templates" / "dashboard.html"
DASHBOARD_STATIC = {
    "dashboard.css": (PROJECT_DIR / "web" / "static" / "dashboard.css", "text/css; charset=utf-8"),
    "dashboard.js": (PROJECT_DIR / "web" / "static" / "dashboard.js", "text/javascript; charset=utf-8"),
}
INTERNAL_HOST_SUFFIXES = (".internal", ".local", ".home.arpa")
KNOWN_INTERNAL_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata.internal",
    "instance-data.ec2.internal",
}


def _reject_internal_policy_host(host: str) -> None:
    normalized = host.strip().lower().rstrip(".")
    if not normalized:
        return
    if normalized in KNOWN_INTERNAL_HOSTS or normalized.endswith(INTERNAL_HOST_SUFFIXES):
        raise APIError("internal host is not allowed")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return
    if any(
        getattr(address, flag)
        for flag in ("is_loopback", "is_link_local", "is_private", "is_multicast", "is_reserved", "is_unspecified")
    ):
        raise APIError("internal host is not allowed")


def _reject_internal_source_target(policy: Mapping[str, Any]) -> None:
    if not isinstance(policy, Mapping):
        return
    allowed_hosts = policy.get("allowed_hosts", ())
    if isinstance(allowed_hosts, (list, tuple)):
        for host in allowed_hosts:
            if isinstance(host, str):
                _reject_internal_policy_host(host)
    base_url = policy.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        return
    parsed = urlsplit(base_url)
    if parsed.scheme == "https" and parsed.hostname:
        _reject_internal_policy_host(parsed.hostname)


def _open(database_path: str):
    return Database(database_path)


def list_sources(database_path: str, *, limit: int, offset: int) -> list[dict[str, Any]]:
    with _open(database_path) as database:
        rows = database.connection.execute(
            """
            SELECT source_id, name, base_url, active_version_id, created_at, updated_at
            FROM sources ORDER BY created_at, source_id LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]


def get_source(database_path: str, source_id: str) -> dict[str, Any] | None:
    with _open(database_path) as database:
        row = database.connection.execute(
            """
            SELECT source_id, name, base_url, active_version_id, created_at, updated_at
            FROM sources WHERE source_id = ?
            """,
            (source_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def list_policies(database_path: str, source_id: str) -> list[dict[str, Any]]:
    with _open(database_path) as database:
        rows = database.connection.execute(
            """
            SELECT policy_id, source_id, policy_json, policy_digest, live_enabled, created_at, updated_at
            FROM source_policies WHERE source_id = ? ORDER BY created_at, policy_id
            """,
            (source_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["policy"] = json.loads(item.pop("policy_json"))
            item["live_enabled"] = bool(item["live_enabled"])
            result.append(item)
        return result


def list_versions(database_path: str, source_id: str) -> list[dict[str, Any]]:
    with _open(database_path) as database:
        rows = database.connection.execute(
            """
            SELECT version_id, source_id, candidate_id, revision, status, goal, code_sha256,
                   dependencies_sha256, policy_digest, engine, provider_id, model,
                   fixture_verification, created_at
            FROM scraper_versions WHERE source_id = ? ORDER BY revision, version_id
            """,
            (source_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_version(database_path: str, version_id: str) -> dict[str, Any] | None:
    with _open(database_path) as database:
        row = database.connection.execute(
            """
            SELECT version_id, source_id, candidate_id, revision, status, goal, code_sha256,
                   dependencies_sha256, policy_digest, engine, provider_id, model,
                   fixture_verification, created_at
            FROM scraper_versions WHERE version_id = ?
            """,
            (version_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def list_products(database_path: str, *, source_id: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
    query = """
        SELECT p.product_id, p.source_id, p.identity_key, p.title, p.canonical_url,
               p.attributes_json, p.review_status, p.created_at, p.updated_at,
               v.variant_id, v.variant_key, v.sku, v.unit_name, v.unit_quantity
        FROM products AS p LEFT JOIN variants AS v ON v.product_id = p.product_id
    """
    parameters: list[Any] = []
    if source_id is not None:
        query += " WHERE p.source_id = ?"
        parameters.append(source_id)
    query += " ORDER BY p.created_at, p.product_id, v.variant_key LIMIT ? OFFSET ?"
    parameters.extend((limit, offset))
    with _open(database_path) as database:
        rows = database.connection.execute(query, parameters).fetchall()
        return [{**dict(row), "attributes": json.loads(row["attributes_json"])} for row in rows]


def list_variants(database_path: str, product_id: str) -> list[dict[str, Any]]:
    with _open(database_path) as database:
        rows = database.connection.execute(
            """
            SELECT variant_id, product_id, variant_key, sku, attributes_json, packaging_json,
                   unit_name, unit_quantity, review_status, created_at, updated_at
            FROM variants WHERE product_id = ? ORDER BY variant_key
            """,
            (product_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["attributes"] = json.loads(item.pop("attributes_json"))
            item["packaging"] = json.loads(item.pop("packaging_json"))
            result.append(item)
        return result


def product_exists(database_path: str, product_id: str) -> bool:
    with _open(database_path) as database:
        return database.connection.execute(
            "SELECT 1 FROM products WHERE product_id = ?", (product_id,)
        ).fetchone() is not None


def variant_exists(database_path: str, variant_id: str) -> bool:
    with _open(database_path) as database:
        return database.connection.execute(
            "SELECT 1 FROM variants WHERE variant_id = ?", (variant_id,)
        ).fetchone() is not None


def list_history(
    database_path: str,
    *,
    variant_id: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    with _open(database_path) as database:
        rows = database.connection.execute(
            """
            SELECT o.observation_id, o.variant_id, o.observed_at, o.ingested_at, o.status, o.price_state,
                   o.price_amount, o.price_currency, o.price_kind, o.price_precision, o.list_price_amount,
                   o.list_price_currency, o.unit_price_amount, o.unit_price_currency, o.unit_quantity,
                   o.unit_name, o.availability, o.job_id, o.raw_hash, o.evidence_json, o.provenance_json,
                   p.title
            FROM observations AS o
            JOIN variants AS v ON v.variant_id = o.variant_id
            JOIN products AS p ON p.product_id = v.product_id
            WHERE o.variant_id = ?
            ORDER BY o.observed_at DESC, o.observation_id DESC LIMIT ? OFFSET ?
            """,
            (variant_id, limit, offset),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            item["provenance"] = json.loads(item.pop("provenance_json"))
            result.append(item)
        return result


def list_alert_rules(database_path: str) -> list[dict[str, Any]]:
    with _open(database_path) as database:
        rows = database.connection.execute(
            """
            SELECT rule_id, metric, threshold, currency, unit_name, variant_id, source_id,
                   cooldown_seconds, enabled, created_at FROM alert_rules
            ORDER BY created_at, rule_id
            """,
        ).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]


def list_alert_events(database_path: str, *, limit: int, offset: int) -> list[dict[str, Any]]:
    with _open(database_path) as database:
        rows = database.connection.execute(
            """
            SELECT event_id, rule_id, observation_id, variant_id, delta_amount, delta_currency,
                   dedupe_key, occurred_at FROM alert_events
            ORDER BY occurred_at DESC, event_id DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]


def list_quality_issues(database_path: str, *, limit: int, offset: int) -> list[dict[str, Any]]:
    with _open(database_path) as database:
        rows = database.connection.execute(
            """
            SELECT issue_id, issue_type, details_json, source_id, job_id, variant_id, created_at
            FROM data_quality_issues ORDER BY created_at, issue_id LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result


class APIError(ValueError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def pagination(url: str) -> tuple[int, int]:
    values = parse_qs(urlsplit(url).query, keep_blank_values=False)
    limit_text = values.get("limit", ["20"])[0]
    offset_text = values.get("offset", ["0"])[0]
    try:
        limit, offset = int(limit_text), int(offset_text)
    except ValueError as exc:
        raise APIError("limit and offset must be integers") from exc
    if not 1 <= limit <= MAX_LIMIT or not 0 <= offset <= 100000:
        raise APIError("pagination parameters are out of range", status=400)
    return limit, offset


class APIRouteHandler(BaseHTTPRequestHandler):
    def __init__(self, database_path: str, *args: Any, **kwargs: Any):
        self.database_path = database_path
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _dispatch(self, method: str) -> None:
        try:
            payload, status, content_type = self._route(method)
            body = payload.encode("utf-8")
        except APIError as exc:
            body = json.dumps({"error": str(exc)}, sort_keys=True, separators=(",", ":")).encode()
            status, content_type = exc.status, "application/json"
        except (LookupError, PermissionError, ValueError) as exc:
            if isinstance(exc, ResourceNotFound):
                status, content_type = 404, "application/json"
            else:
                status, content_type = 400, "application/json"
            body = json.dumps({"error": str(exc)}, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise APIError("Content-Type must be application/json", status=415)
        if self.headers.get("Transfer-Encoding"):
            raise APIError("chunked request bodies are not supported", status=411)
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise APIError("Content-Length must be an integer") from exc
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            raise APIError("request body is too large", status=413)
        raw = self.rfile.read(content_length)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError("request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise APIError("request body must be a JSON object")
        return value

    def _send_payload(self, payload: Any, status: int = 200, content_type: str = "application/json") -> tuple[str, int, str]:
        if isinstance(payload, str):
            return payload, status, content_type
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str), status, content_type

    def _route(self, method: str) -> tuple[str, int, str]:
        parsed = urlsplit(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if method == "GET" and parts == ["health"]:
            return self._send_payload({"status": "ok"})
        if method == "GET" and (parts == [] or parts == ["dashboard"]):
            try:
                document = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
            except OSError as exc:
                raise APIError("dashboard is unavailable", status=503) from exc
            return self._send_payload(document, 200, "text/html; charset=utf-8")
        if method == "GET" and len(parts) == 2 and parts[0] == "static":
            static = DASHBOARD_STATIC.get(parts[1])
            if static is None:
                raise APIError("static file does not exist", status=404)
            path, content_type = static
            try:
                document = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise APIError("static file is unavailable", status=503) from exc
            return self._send_payload(document, 200, content_type)
        if method == "GET" and parts == ["sources"]:
            limit, offset = pagination(self.path)
            return self._send_payload(list_sources(self.database_path, limit=limit, offset=offset))
        if method == "POST" and parts == ["sources"]:
            return self._create_source(self._json_body())
        if method == "GET" and len(parts) == 2 and parts[0] == "sources":
            item = get_source(self.database_path, parts[1])
            if item is None:
                raise APIError("source does not exist", status=404)
            return self._send_payload(item)
        if method == "GET" and len(parts) == 3 and parts[0] == "sources" and parts[2] == "policies":
            if get_source(self.database_path, parts[1]) is None:
                raise APIError("source does not exist", status=404)
            return self._send_payload(list_policies(self.database_path, parts[1]))
        if method == "GET" and len(parts) == 3 and parts[0] == "sources" and parts[2] == "candidates":
            if get_source(self.database_path, parts[1]) is None:
                raise APIError("source does not exist", status=404)
            return self._send_payload(list_versions(self.database_path, parts[1]))
        if method == "POST" and len(parts) == 4 and parts[0] == "sources" and parts[3] == "activate":
            return self._activate(parts[1], parts[2])
        if method == "GET" and parts == ["products"]:
            limit, offset = pagination(self.path)
            source_id = parse_qs(parsed.query).get("source_id", [None])[0]
            if source_id is not None and get_source(self.database_path, source_id) is None:
                raise APIError("source does not exist", status=404)
            return self._send_payload(list_products(self.database_path, source_id=source_id, limit=limit, offset=offset))
        if method == "GET" and len(parts) == 3 and parts[0] == "products" and parts[2] == "variants":
            if not product_exists(self.database_path, parts[1]):
                raise APIError("product does not exist", status=404)
            return self._send_payload(list_variants(self.database_path, parts[1]))
        if method == "GET" and len(parts) == 3 and parts[0] == "variants" and parts[2] == "history":
            limit, offset = pagination(self.path)
            if not variant_exists(self.database_path, parts[1]):
                raise APIError("variant does not exist", status=404)
            return self._send_payload(list_history(self.database_path, variant_id=parts[1], limit=limit, offset=offset))
        if method == "GET" and len(parts) == 3 and parts[0] == "variants" and parts[2] == "export":
            if not variant_exists(self.database_path, parts[1]):
                raise APIError("variant does not exist", status=404)
            export_format = parse_qs(parsed.query).get("format", ["csv"])[0]
            if export_format == "csv":
                return self._export("text/csv", parts[1])
            if export_format == "json":
                return self._export("application/json", parts[1])
            raise APIError("export format must be csv or json")
        if method == "GET" and parts == ["alerts", "rules"]:
            return self._send_payload(list_alert_rules(self.database_path))
        if method == "POST" and parts == ["alerts", "rules"]:
            rule_id = AlertService(self.database_path).upsert_rule(self._json_body())
            return self._send_payload({"rule_id": rule_id}, 201)
        if method == "POST" and parts == ["alerts", "evaluate"]:
            self._json_body()
            return self._send_payload(AlertService(self.database_path).evaluate())
        if method == "GET" and parts == ["alerts", "events"]:
            limit, offset = pagination(self.path)
            return self._send_payload(list_alert_events(self.database_path, limit=limit, offset=offset))
        if method == "POST" and parts == ["jobs"]:
            return self._start_job(self._json_body())
        if method == "GET" and parts == ["jobs"]:
            limit, offset = pagination(self.path)
            return self._send_payload(JobManager(self.database_path).list_jobs(limit=limit, offset=offset))
        if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
            job = JobManager(self.database_path).get(parts[1])
            if job is None:
                raise APIError("job does not exist", status=404)
            return self._send_payload(job)
        if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
            self._json_body()
            cancelled = JobManager(self.database_path).cancel(parts[1])
            if not cancelled:
                raise APIError("queued job does not exist", status=404)
            return self._send_payload({"job_id": parts[1], "status": "cancelled"}, 200)
        if method == "GET" and parts == ["quality-issues"]:
            limit, offset = pagination(self.path)
            return self._send_payload(list_quality_issues(self.database_path, limit=limit, offset=offset))
        raise APIError("route not found", status=404)

    def _create_source(self, payload: dict[str, Any]) -> tuple[str, int, str]:
        policy = payload.get("policy")
        if not isinstance(policy, Mapping):
            raise APIError("policy must be an object")
        _reject_internal_source_target(policy)
        schedule = payload.get("schedule")
        if schedule is not None:
            self._validate_schedule(schedule)
        onboarded = onboard_source(policy, payload.get("goal", ""))
        with Database(self.database_path) as database:
            repository = DataRepository(database)
            source = Source(
                source_id=onboarded.source_id,
                name=onboarded.source_id,
                base_url=onboarded.policy["base_url"],
            )
            # Activation owns active_version_id; metadata updates must never overwrite it.
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO sources(source_id, name, base_url, active_version_id, created_at, updated_at)
                    VALUES (?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        name = excluded.name,
                        base_url = excluded.base_url,
                        updated_at = excluded.updated_at
                    """,
                    (source.source_id, source.name, source.base_url, source.created_at, source.updated_at),
                )
            repository.upsert_policy(
                SourcePolicy(
                    policy_id=f"policy_{onboarded.policy_digest[:16]}",
                    source_id=onboarded.source_id,
                    policy=onboarded.policy,
                    policy_digest=onboarded.policy_digest,
                    live_enabled=bool(onboarded.policy["live_enabled"]),
                )
            )
            if schedule is not None:
                repository.upsert_schedule(
                    Schedule(
                        schedule_id=f"schedule_{uuid.uuid4().hex[:16]}",
                        source_id=onboarded.source_id,
                        schedule=json.dumps(schedule, sort_keys=True, separators=(",", ":")),
                        max_concurrency=int(onboarded.policy["max_concurrency"]),
                        requests_per_minute=int(onboarded.policy["requests_per_minute"]),
                    )
                )
        return self._send_payload({"source_id": onboarded.source_id, "policy_digest": onboarded.policy_digest}, 201)

    @staticmethod
    def _validate_schedule(schedule: Any) -> None:
        if not isinstance(schedule, dict) or schedule.get("type") != "interval" or "seconds" not in schedule:
            raise APIError("schedule must contain interval type and seconds")
        seconds = schedule["seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 60 <= seconds <= 86400:
            raise APIError("schedule seconds is out of range")

    def _activate(self, source_id: str, version_id: str) -> tuple[str, int, str]:
        if get_source(self.database_path, source_id) is None:
            raise APIError("source does not exist", status=404)
        version = get_version(self.database_path, version_id)
        if version is None or version["source_id"] != source_id:
            raise APIError("candidate does not belong to source", status=404)
        with Database(self.database_path) as database:
            activated = DataRepository(database).activate_scraper_version(version_id)
        if not activated:
            raise APIError("candidate must be validated with passed fixture verification", status=409)
        return self._send_payload({"version_id": version_id, "status": "active"}, 200)

    def _start_job(self, payload: dict[str, Any]) -> tuple[str, int, str]:
        source_id = payload.get("source_id", "")
        idempotency_key = payload.get("idempotency_key", "")
        scraper_version_id = payload.get("scraper_version_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise APIError("source_id must be a nonempty string")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise APIError("idempotency_key must be a nonempty string")
        if scraper_version_id is not None and (not isinstance(scraper_version_id, str) or not scraper_version_id.strip()):
            raise APIError("scraper_version_id must be a nonempty string")
        for field in ("retry_budget", "lease_seconds", "backoff_seconds"):
            value = payload.get(field, {"retry_budget": 3, "lease_seconds": 300, "backoff_seconds": 30}[field])
            if isinstance(value, bool) or not isinstance(value, int):
                raise APIError(f"{field} must be an integer")
        manager = JobManager(self.database_path)
        try:
            result = manager.start_job(
                source_id=source_id,
                idempotency_key=idempotency_key,
                scraper_version_id=scraper_version_id,
                retry_budget=payload.get("retry_budget", 3),
                lease_seconds=payload.get("lease_seconds", 300),
                backoff_seconds=payload.get("backoff_seconds", 30),
            )
        except JobLimitExceeded as exc:
            raise APIError(str(exc), status=429) from exc
        except LookupError as exc:
            raise APIError(str(exc), status=404) from exc
        return self._send_payload(
            {"job_id": result.job_id, "created": result.created},
            201 if result.created else 200,
        )

    def _export(self, content_type: str, variant_id: str) -> tuple[str, int, str]:
        rows = list_history(self.database_path, variant_id=variant_id, limit=10000, offset=0)
        if content_type == "text/csv":
            return self._send_payload(serialize_csv_rows(rows, fields=HISTORY_CSV_FIELDS), 200, "text/csv")
        return self._send_payload(serialize_json_rows(rows), 200, content_type)


def run_control_plane(database_path: str, *, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    def handler(*args: Any, **kwargs: Any):
        return APIRouteHandler(database_path, *args, **kwargs)

    with Database(database_path) as database:
        database.initialize()
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("control plane may only bind to loopback")
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
