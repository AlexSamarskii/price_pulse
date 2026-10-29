from __future__ import annotations

import csv
import json
import io
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from pricepulse.api.export import HISTORY_CSV_FIELDS
from pricepulse.api.routes import APIRouteHandler
from pricepulse.data.normalize import Availability, NormalizedProduct, PriceField
from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import DataQualityIssue, Source, SourcePolicy, ScraperVersion
from pricepulse.jobs.manager import JobManager


POLICY = {
    "source_id": "routes",
    "base_url": "https://fixture.test",
    "allowed_hosts": ["fixture.test"],
    "allowed_path_prefixes": ["/"],
    "robots_policy": "operator_authorized_fixture",
    "max_pages": 1,
    "requests_per_minute": 30,
    "max_concurrency": 1,
    "timeout_seconds": 10,
    "allowed_currencies": ["RUB"],
    "live_enabled": False,
}


def seed(path: str) -> None:
    with Database(path) as database:
        database.initialize()
        repository = DataRepository(database)
        repository.upsert_source(Source(source_id="routes", name="Routes", base_url="https://fixture.test"))
        repository.upsert_policy(
            SourcePolicy(
                policy_id="policy_1",
                source_id="routes",
                policy=POLICY,
                policy_digest="0" * 64,
                live_enabled=False,
            )
        )
        repository.persist_normalized(
            NormalizedProduct(
                source_id="routes",
                identity_key="identity-1",
                title="=cmd;@x",
                canonical_url="https://fixture.test/p/1",
                variant_key="variant-1",
                sku="SKU-1",
                attributes={},
                packaging={},
                unit_name="piece",
                unit_quantity=Decimal("1"),
                sale_price=PriceField("present", Decimal("1299.90"), "RUB", 2),
                list_price=PriceField("present", Decimal("1500.00"), "RUB", 2),
                unit_price=PriceField("present", Decimal("1299.90"), "RUB", 2),
                availability=Availability.AVAILABLE,
                observed_at="2026-01-01T00:00:00.000+00:00",
                evidence={"fixture": "local"},
                raw_hash="hash-1",
                )
        )
        repository.upsert_scraper_version(
            ScraperVersion(
                version_id="version_1",
                source_id="routes",
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
        )


@pytest.fixture()
def server(tmp_path):
    path = str(tmp_path / "api.db")
    seed(path)

    def handler(*args, **kwargs):
        return APIRouteHandler(path, *args, **kwargs)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread_address = httpd.server_address[1]
    import threading

    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    yield SimpleNamespace(
        url=f"http://127.0.0.1:{thread_address}",
        database_path=path,
    )
    httpd.shutdown()
    httpd.server_close()


def request(url: str, *, method: str = "GET", payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method)) as response:
            body = response.read()
            return response.status, response.headers.get("Content-Type"), body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def test_export_roundtrip_and_csv_formula_safety(server):
    status, _, body = request(f"{server.url}/health")
    assert status == 200 and json.loads(body) == {"status": "ok"}

    status, _, body = request(f"{server.url}/sources")
    assert status == 200 and json.loads(body)[0]["source_id"] == "routes"

    status, _, body = request(f"{server.url}/sources/routes/candidates")
    candidates = json.loads(body)
    assert status == 200 and len(candidates) == 1 and candidates[0]["status"] == "validated"

    status, _, body = request(f"{server.url}/products")
    products = json.loads(body)
    assert status == 200 and products[0]["title"] == "=cmd;@x"
    status, _, body = request(f"{server.url}/products/{products[0]['product_id']}/variants")
    variants = json.loads(body)
    assert status == 200 and len(variants) == 1
    variant = variants[0]
    history_response = request(f"{server.url}/variants/{variant['variant_id']}/history")
    assert history_response[0] == 200
    history = json.loads(history_response[2])
    history_item = history[0]
    assert history_item["variant_id"] == variant["variant_id"]
    assert history_item["observed_at"] == "2026-01-01T00:00:00.000+00:00"
    assert history_item["price_amount"] == "1299.90"
    assert history_item["price_currency"] == "RUB"
    assert history_item["availability"] == "available"
    assert history_item["evidence"] == {"fixture": "local"}

    status, content_type, body = request(f"{server.url}/variants/{variant['variant_id']}/export")
    assert status == 200 and content_type == "text/csv"
    text = body.decode()
    csv_rows = list(csv.DictReader(io.StringIO(text)))
    assert len(csv_rows) == len(history)
    assert "'=cmd;@x" in text
    assert "1299.90" in text
    assert "RUB" in text
    assert "2026-01-01T00:00:00.000+00:00" in text

    csv_reader = csv.DictReader(io.StringIO(text))
    assert csv_reader.fieldnames == HISTORY_CSV_FIELDS
    for csv_item, expected_item in zip(csv_rows, history):
        assert set(csv_item) == set(HISTORY_CSV_FIELDS)
        for field in HISTORY_CSV_FIELDS:
            actual = csv_item[field]
            expected = expected_item[field]
            if field in {"evidence", "provenance"}:
                actual = json.loads(actual)
            elif expected is None:
                assert actual == ""
                continue
            elif isinstance(expected, int):
                assert int(actual) == expected
                continue
            elif isinstance(expected, str) and expected.startswith(("=", "+", "-", "@")):
                expected = "'" + expected
            assert actual == expected

    status, content_type, body = request(f"{server.url}/variants/{variant['variant_id']}/export?format=json")
    assert status == 200 and content_type == "application/json"
    assert json.loads(body) == history


def test_job_start_and_status(server):
    status, _, body = request(
        f"{server.url}/jobs",
        method="POST",
        payload={"source_id": "routes", "idempotency_key": "api-job"},
    )
    assert status == 201
    job_id = json.loads(body)["job_id"]
    status, _, body = request(
        f"{server.url}/jobs",
        method="POST",
        payload={"source_id": "routes", "idempotency_key": "api-job"},
    )
    assert status == 200 and json.loads(body)["job_id"] == job_id
    status, _, body = request(f"{server.url}/jobs/{job_id}")
    assert status == 200 and json.loads(body)["status"] == "queued"
    status, _, _ = request(f"{server.url}/jobs/job_missing")
    assert status == 404


def test_list_jobs_returns_persisted_states(server):
    status, _, body = request(
        f"{server.url}/jobs",
        method="POST",
        payload={"source_id": "routes", "idempotency_key": "list-jobs"},
    )
    assert status == 201
    job_id = json.loads(body)["job_id"]

    status, _, body = request(f"{server.url}/jobs?limit=100")
    assert status == 200
    jobs = json.loads(body)
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == job_id
    assert jobs[0]["status"] == "queued"

    status, _, _ = request(f"{server.url}/jobs?limit=999")
    assert status == 400


def test_job_type_validation_returns_400_without_new_job(server):
    status, _, body = request(
        f"{server.url}/jobs",
        method="POST",
        payload={"source_id": "routes", "idempotency_key": "type-validation", "retry_budget": "3"},
    )
    assert status == 400 and json.loads(body)["error"] == "retry_budget must be an integer"

    status, _, body = request(
        f"{server.url}/jobs",
        method="POST",
        payload={"source_id": "routes", "idempotency_key": "type-validation", "lease_seconds": "300"},
    )
    assert status == 400 and json.loads(body)["error"] == "lease_seconds must be an integer"

    status, _, body = request(
        f"{server.url}/jobs",
        method="POST",
        payload={"source_id": "routes", "idempotency_key": "type-validation", "backoff_seconds": "30"},
    )
    assert status == 400 and json.loads(body)["error"] == "backoff_seconds must be an integer"

    status, _, body = request(
        f"{server.url}/jobs",
        method="POST",
        payload={"source_id": {"value": "routes"}, "idempotency_key": "type-validation"},
    )
    assert status == 400 and json.loads(body)["error"] == "source_id must be a nonempty string"

    status, _, body = request(
        f"{server.url}/jobs",
        method="POST",
        payload={"source_id": "routes", "idempotency_key": "type-validation", "scraper_version_id": []},
    )
    assert status == 400 and json.loads(body)["error"] == "scraper_version_id must be a nonempty string"

    status, _, body = request(
        f"{server.url}/jobs",
        method="POST",
        payload={"source_id": "routes", "idempotency_key": "type-validation"},
    )
    assert status == 201
    job = json.loads(body)
    status, _, body = request(f"{server.url}/jobs/{job['job_id']}")
    assert status == 200 and json.loads(body)["status"] == "queued"


def test_alert_rule_type_validation_returns_400_without_new_rule(server):
    base_payload = {"rule_id": "type-validation", "metric": "price_change", "threshold": "1", "currency": "RUB"}

    status, _, body = request(
        f"{server.url}/alerts/rules",
        method="POST",
        payload={**base_payload, "unit_name": {"x": "y"}},
    )
    assert status == 400 and json.loads(body)["error"] == "unit_name must be a string or null"

    status, _, body = request(
        f"{server.url}/alerts/rules",
        method="POST",
        payload={**base_payload, "unit_name": 1},
    )
    assert status == 400 and json.loads(body)["error"] == "unit_name must be a string or null"

    status, _, body = request(
        f"{server.url}/alerts/rules",
        method="POST",
        payload={**base_payload, "enabled": "maybe"},
    )
    assert status == 400 and json.loads(body)["error"] == "enabled must be a boolean"

    status, _, body = request(f"{server.url}/alerts/rules")
    assert status == 200 and json.loads(body) == []


def test_list_quality_issues_returns_persisted_details(server):
    with Database(server.database_path) as database:
        DataRepository(database).record_quality_issue(
            DataQualityIssue(
                issue_id="quality_1",
                issue_type="dedupe_conflict",
                details={"reason": "ambiguous_fixture"},
                source_id="routes",
            )
        )

    status, _, body = request(f"{server.url}/quality-issues?limit=100")
    assert status == 200
    issues = json.loads(body)
    assert len(issues) == 1
    assert issues[0]["issue_id"] == "quality_1"
    assert issues[0]["issue_type"] == "dedupe_conflict"
    assert issues[0]["details"] == {"reason": "ambiguous_fixture"}

    status, _, _ = request(f"{server.url}/quality-issues?offset=-1")
    assert status == 400


def test_dashboard_and_static_files_are_served(server):
    status, content_type, body = request(f"{server.url}/dashboard")
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert "Загрузка сохранённых данных" in body.decode("utf-8")
    assert request(f"{server.url}")[0] == 200

    for filename, content_type, marker in (
        ("dashboard.css", "text/css; charset=utf-8", b".dashboard"),
        ("dashboard.js", "text/javascript; charset=utf-8", b"deriveDashboardState"),
    ):
        status, response_type, body = request(f"{server.url}/static/{filename}")
        assert status == 200 and response_type == content_type and marker in body

    assert request(f"{server.url}/static/../../app.py")[0] == 404
    assert request(f"{server.url}/static/app.py")[0] == 404


def test_unknown_resources_return_404(server):
    status, _, body = request(f"{server.url}/products?limit=999")
    assert status == 400
    status, _, _ = request(f"{server.url}/metadata/secret")
    assert status == 404
    assert request(f"{server.url}/sources/missing/candidates")[0] == 404
    assert request(f"{server.url}/products?source_id=missing")[0] == 404
    assert request(f"{server.url}/products/missing/variants")[0] == 404
    assert request(f"{server.url}/variants/missing/history")[0] == 404
    assert request(f"{server.url}/variants/missing/export")[0] == 404
    for payload in (
        {"rule_id": "missing-variant", "metric": "price_change", "threshold": "1", "currency": "RUB", "variant_id": "missing"},
        {"rule_id": "missing-source", "metric": "price_change", "threshold": "1", "currency": "RUB", "source_id": "missing"},
    ):
        status, _, body = request(f"{server.url}/alerts/rules", method="POST", payload=payload)
        assert status == 404
        assert "does not exist" in json.loads(body)["error"]


def test_rejects_invalid_content_length(server):
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", server.url.rsplit(":", 1)[1], timeout=1)
    connection.putrequest("POST", "/jobs")
    connection.putheader("Content-Type", "application/json")
    connection.putheader("Content-Length", "-1")
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 413
    connection.close()


def test_create_and_retrieve_source(server):
    status, _, body = request(f"{server.url}/sources", method="POST", payload={"policy": POLICY, "goal": "prices"})
    assert status == 201
    payload = json.loads(body)
    assert payload["source_id"] == "routes"
    status, _, body = request(f"{server.url}/sources/routes/policies")
    assert status == 200 and len(json.loads(body)) == 2


def test_create_source_with_schedule(server):
    schedule = {"type": "interval", "seconds": 300}
    status, _, body = request(
        f"{server.url}/sources",
        method="POST",
        payload={"policy": POLICY, "goal": "prices", "schedule": schedule},
    )
    assert status == 201 and json.loads(body)["source_id"] == "routes"


def test_update_source_preserves_active_version(server):
    request(f"{server.url}/sources/routes/version_1/activate", method="POST", payload={})
    policy = {**POLICY, "timeout_seconds": 20}
    status, _, _ = request(f"{server.url}/sources", method="POST", payload={"policy": policy, "goal": "prices"})
    assert status == 201
    status, _, body = request(f"{server.url}/sources/routes")
    assert status == 200 and json.loads(body)["active_version_id"] == "version_1"


def test_update_source_preserves_concurrent_activation(server, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event, local

    import pricepulse.api.routes as routes

    update_ready = Event()
    activation_done = Event()
    context = local()
    original_onboard = routes.onboard_source
    original_transaction = Database.transaction

    def mark_source_update(*args, **kwargs):
        result = original_onboard(*args, **kwargs)
        context.source_update = True
        return result

    @contextmanager
    def pause_source_write(database):
        if getattr(context, "source_update", False):
            context.source_update = False
            update_ready.set()
            assert activation_done.wait(5), "concurrent activation did not complete"
        with original_transaction(database) as connection:
            yield connection

    monkeypatch.setattr(routes, "onboard_source", mark_source_update)
    monkeypatch.setattr(Database, "transaction", pause_source_write)
    policy = {**POLICY, "timeout_seconds": 20}
    with ThreadPoolExecutor(max_workers=1) as executor:
        update = executor.submit(
            request, f"{server.url}/sources", method="POST", payload={"policy": policy, "goal": "prices"}
        )
        try:
            assert update_ready.wait(5), "source update did not reach its write"
            status, _, body = request(f"{server.url}/sources/routes/version_1/activate", method="POST", payload={})
            assert status == 200, body
        finally:
            activation_done.set()
        status, _, body = update.result(timeout=10)
        assert status == 201, body

    status, _, body = request(f"{server.url}/sources/routes")
    assert status == 200 and json.loads(body)["active_version_id"] == "version_1"
    status, _, body = request(f"{server.url}/sources/routes/policies")
    assert status == 200 and any(item["policy"]["timeout_seconds"] == 20 for item in json.loads(body))


def test_invalid_schedule_rejects_without_policies(server):
    payload = {"policy": POLICY, "goal": "prices", "schedule": {"type": "interval", "seconds": 10}}
    status, _, body = request(f"{server.url}/sources", method="POST", payload=payload)
    assert status == 400 and "schedule seconds" in json.loads(body)["error"]
    policies = json.loads(request(f"{server.url}/sources/routes/policies")[2])
    assert len(policies) == 1


@pytest.mark.parametrize(
    ("host",),
    [
        ("169.254.169.254",),
        ("127.0.0.1",),
        ("localhost",),
        ("10.0.0.2",),
        ("metadata.internal",),
    ],
)
def test_reject_internal_source_targets(server, host):
    source_id = f"internal_{host.replace('.', '_').replace('-', '_')}"
    policy = {**POLICY, "source_id": source_id, "base_url": f"https://{host}/catalog", "allowed_hosts": [host]}
    status, _, body = request(f"{server.url}/sources", method="POST", payload={"policy": policy, "goal": "prices"})
    assert status == 400 and "internal host" in json.loads(body)["error"]
    assert request(f"{server.url}/sources/{source_id}")[0] == 404


def test_rejects_file_source_target(server):
    policy = {**POLICY, "base_url": "file:///tmp/prices.html", "allowed_hosts": ["fixture.test"]}
    status, _, body = request(f"{server.url}/sources", method="POST", payload={"policy": policy, "goal": "prices"})
    assert status == 400 and "base_url must be an HTTPS URL" in json.loads(body)["error"]


def test_rejects_null_policy_without_onboarding(server):
    status, _, body = request(f"{server.url}/sources", method="POST", payload={"policy": None, "goal": "prices"})
    assert status == 400 and "policy must be an object" in json.loads(body)["error"]
    policies = json.loads(request(f"{server.url}/sources/routes/policies")[2])
    assert len(policies) == 1


def test_activation_gates_unvalidated_candidate(server):
    status, _, body = request(f"{server.url}/sources/routes/version_1/activate", method="POST", payload={})
    assert status == 200 and json.loads(body)["status"] == "active"
    status, _, body = request(f"{server.url}/sources/routes")
    assert json.loads(body)["active_version_id"] == "version_1"
