from __future__ import annotations

import json
import os
import subprocess
import threading
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from pricepulse.api.alerts import AlertService
from pricepulse.api.routes import (
    APIRouteHandler,
    list_alert_events,
    list_history,
    list_policies,
    list_products,
    list_sources,
    list_variants,
)
from pricepulse.data.normalize import Availability, NormalizedProduct, PriceField
from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import (
    AlertRule,
    CollectionJob,
    DataQualityIssue,
    Source,
    SourcePolicy,
)


POLICY = {
    "source_id": "dashboard",
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


def observation_data(observed_at: str, raw_hash: str, price: str = "1299.90") -> NormalizedProduct:
    return NormalizedProduct(
        source_id="dashboard",
        identity_key="identity-1",
        title='=cmd;"><script>alert(1)</script>',
        canonical_url="https://fixture.test/p/1",
        variant_key="variant-1",
        sku="SKU-1",
        attributes={},
        packaging={},
        unit_name="piece",
        unit_quantity=Decimal("1"),
        sale_price=PriceField("present", Decimal(price), "RUB", 2),
        list_price=PriceField("present", Decimal("1500.00"), "RUB", 2),
        unit_price=PriceField("present", Decimal(price), "RUB", 2),
        availability=Availability.AVAILABLE,
        observed_at=observed_at,
        evidence={"fixture": "local"},
        raw_hash=raw_hash,
    )


@pytest.fixture(name="dashboard_input")
def dashboard_input_fixture(tmp_path: Path) -> dict:
    database_path = str(tmp_path / "dashboard.db")
    with Database(database_path) as database:
        database.initialize()
        repository = DataRepository(database)
        repository.upsert_source(Source(source_id="dashboard", name="Dashboard fixture", base_url="https://fixture.test"))
        repository.upsert_policy(
            SourcePolicy(
                policy_id="policy_1",
                source_id="dashboard",
                policy=POLICY,
                policy_digest="0" * 64,
                live_enabled=False,
            )
        )
        job_id, _ = repository.get_or_create_job(
            CollectionJob(
                job_id="job_dashboard",
                source_id="dashboard",
                idempotency_key="dashboard-job",
                status="succeeded",
            )
        )
        first = repository.persist_normalized(observation_data("2026-01-01T00:00:00.000+00:00", "hash-1"), job_id=job_id)
        second = repository.persist_normalized(
            observation_data("2026-01-01T01:00:00.000+00:00", "hash-2", "1399.90"),
            job_id=job_id,
        )
        assert first.variant_id is not None and second.variant_id == first.variant_id
        repository.record_quality_issue(
            DataQualityIssue(
                issue_id="quality_1",
                issue_type="dedupe_conflict",
                details={"reason": "ambiguous_fixture"},
                source_id="dashboard",
            )
        )
        AlertService(database_path).upsert_rule(
            {
                "rule_id": "rule_dashboard",
                "metric": "price_change",
                "threshold": "1.00",
                "currency": "RUB",
                "cooldown_seconds": 0,
                "enabled": True,
            }
        )
        AlertService(database_path).evaluate()

        return {
            "sources": list_sources(database_path, limit=100, offset=0),
            "policies": list_policies(database_path, "dashboard"),
            "products": list_products(database_path, source_id="dashboard", limit=100, offset=0),
            "variants": list_variants(database_path, first.product_id),
            "history": list_history(database_path, variant_id=first.variant_id, limit=100, offset=0),
            "jobs": [
                {
                    "job_id": job_id,
                    "status": "succeeded",
                    "created_at": "2026-01-01T00:00:00.000+00:00",
                    "updated_at": "2026-01-01T00:00:00.000+00:00",
                }
            ],
            "alertsEvents": list_alert_events(database_path, limit=100, offset=0),
            "qualityIssues": [
                {
                    "issue_id": "quality_1",
                    "issue_type": "dedupe_conflict",
                    "created_at": "2026-01-01T00:00:00.000+00:00",
                }
            ],
            "errors": {},
        }


def _node(path: Path, dashboard_input: dict) -> dict:
    input_path = path / "dashboard-input.json"
    input_path.write_text(json.dumps(dashboard_input), encoding="utf-8")
    script = """
const fs = require("fs");
const dashboard = require(process.argv[2]);
const input = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.stdout.write(JSON.stringify(dashboard.deriveDashboardState(input)));
"""
    script_path = path / "derive-state.cjs"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [
            "node",
            str(script_path),
            str(Path("pricepulse/web/static/dashboard.js").resolve()),
            str(input_path.resolve()),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    return json.loads(result.stdout)


def test_dashboard_state_exposes_stale_observations(tmp_path: Path, dashboard_input: dict):
    dashboard_input["history"].append(
        {
            "observation_id": 3,
            "variant_id": dashboard_input["history"][0]["variant_id"],
            "job_id": "job_dashboard",
            "price_amount": "1399.90",
            "price_currency": "RUB",
            "status": "stale",
            "observed_at": "2026-01-01T02:00:00.000+00:00",
        }
    )

    state = _node(tmp_path, dashboard_input)

    assert state["status"] == "ready"
    assert [item["status"] for item in state["stale"]] == ["stale"]


def test_dashboard_loads_all_persisted_resources_from_local_api(tmp_path: Path, dashboard_input: dict):
    database_path = tmp_path / "dashboard.db"

    def handler(*args, **kwargs):
        return APIRouteHandler(str(database_path), *args, **kwargs)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    server = SimpleNamespace(url=f"http://127.0.0.1:{httpd.server_address[1]}")

    script = """
const dashboard = require(process.argv[2]);
const baseFetch = globalThis.fetch;
globalThis.fetch = (url, options) => baseFetch(new URL(url, process.env.DASHBOARD_BASE_URL), options);
(async () => {
  process.stdout.write(JSON.stringify(await dashboard.loadDashboardData()));
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exit(1);
});
"""
    script_path = tmp_path / "load-dashboard.cjs"
    script_path.write_text(script, encoding="utf-8")
    environment = os.environ.copy()
    environment["DASHBOARD_BASE_URL"] = server.url
    try:
        with urlopen(f"{server.url}/dashboard", timeout=5) as response:
            document = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers.get_content_type() == "text/html"
        assert document == Path("pricepulse/web/templates/dashboard.html").read_text(encoding="utf-8")
        for name in ("dashboard.css", "dashboard.js"):
            assert f"/static/{name}" in document
            with urlopen(f"{server.url}/static/{name}", timeout=5) as response:
                assert response.status == 200
                assert response.read() == Path("pricepulse/web/static", name).read_bytes()
        result = subprocess.run(
            ["node", str(script_path), str(Path("pricepulse/web/static/dashboard.js").resolve())],
            check=False,
            capture_output=True,
            text=True,
            cwd=Path.cwd(),
            env=environment,
            timeout=30,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)

    assert state["status"] == "ready"
    assert [source["source_id"] for source in state["sources"]] == ["dashboard"]
    assert len(state["history"]) == 2
    assert len(state["jobs"]) == 1 and state["jobs"][0]["status"] == "succeeded"
    assert len(state["quality"]) == 1 and state["quality"][0]["issue_type"] == "dedupe_conflict"
    assert len(state["alerts"]) == 1
    assert any("Сбор отключён политикой" in message for message in state["blocked"])


def test_dashboard_state_is_derived_from_persisted_fixture(tmp_path: Path, dashboard_input: dict):
    state = _node(tmp_path, dashboard_input)

    assert state["status"] == "ready"
    assert len(state["sources"]) == 1
    assert len(state["products"]) == 1
    assert len(state["history"]) == 2
    assert len(state["changes"]) == 1
    assert state["changes"][0]["from"] == "1299.90"
    assert state["changes"][0]["to"] == "1399.90"
    assert len(state["jobs"]) == 1
    assert state["jobs"][0]["status"] == "succeeded"
    assert len(state["quality"]) == 1
    assert state["alerts"][0]["delta_amount"] == "100.00"
    assert any("Сбор отключён политикой" in message for message in state["blocked"])


def test_dashboard_polling_refreshes_without_overlapping_loads(tmp_path):
    script = tmp_path / "poll-dashboard.cjs"
    script.write_text("""
const assert = require('node:assert/strict');
const dashboard = require(process.argv[2]);
let callback, milliseconds, calls = 0, release;
const loader = () => { calls += 1; return new Promise(resolve => { release = resolve; }); };
const timer = (fn, delay) => { callback = fn; milliseconds = delay; return 42; };
(async () => {
  assert.equal(dashboard.startPolling({}, loader, timer), 42);
  assert.equal(milliseconds, 30000);
  assert.equal(calls, 1);
  await callback();
  assert.equal(calls, 1);
  release();
  await new Promise(resolve => setImmediate(resolve));
  callback();
  assert.equal(calls, 2);
  release();
})().catch(error => { process.stderr.write(String(error)); process.exit(1); });
""", encoding="utf-8")
    result = subprocess.run(["node", str(script), str(Path("pricepulse/web/static/dashboard.js").resolve())],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def _render(tmp_path: Path, dashboard_input: dict) -> dict:
    input_path = tmp_path / "dashboard-render-input.json"
    input_path.write_text(json.dumps(dashboard_input), encoding="utf-8")
    script = """
const fs = require("fs");
const dashboard = require(process.argv[2]);
const input = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
class Element {
  constructor() { this.textContent = ""; this.hidden = false; this.dataset = {}; this.children = []; }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  setAttribute(key, value) { this[key] = value; }
  removeAttribute(key) { delete this[key]; }
  addEventListener() {}
}
global.document = { createElement: () => new Element(), createElementNS: () => new Element() };
global.Option = class extends Element { constructor(text, value) { super(); this.textContent = text; this.value = value; } };
const elements = {};
const root = { querySelector: selector => (elements[selector] ||= new Element()) };
const state = dashboard.deriveDashboardState(input);
dashboard.renderDashboard(root, state);
process.stdout.write(JSON.stringify({
  state,
  error: { hidden: elements["#dashboard-error"].hidden, text: elements["#dashboard-error"].textContent },
}));
"""
    script_path = tmp_path / "render-dashboard.cjs"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [
            "node",
            str(script_path),
            str(Path("pricepulse/web/static/dashboard.js").resolve()),
            str(input_path.resolve()),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_dashboard_ready_render_hides_fatal_error(tmp_path: Path):
    result = _render(tmp_path, {"errors": {}})

    assert result["state"]["status"] == "ready"
    assert result["state"]["error"] == ""
    assert result["error"]["hidden"] is True


def test_dashboard_failed_render_shows_error(tmp_path: Path):
    result = _render(tmp_path, {"errors": {"sources": {"status": 500, "message": "HTTP 500"}}})

    assert result["state"]["status"] == "error"
    assert result["state"]["error"] == "HTTP 500"
    assert result["error"]["hidden"] is False


def test_dashboard_template_declares_required_states():
    rendered = Path("pricepulse/web/templates/dashboard.html").read_text(encoding="utf-8")

    for text in (
        "Загрузка сохранённых данных…",
        "Не удалось загрузить данные.",
        "Источники ещё не добавлены.",
        "Товары не найдены.",
        "Для выбранного варианта пока нет наблюдений.",
        "Устаревших наблюдений в выборке нет.",
        "Сохранённых замечаний нет.",
        "Уведомлений пока нет.",
    ):
        assert text in rendered
    assert 'aria-live="polite"' in rendered
    assert 'role="alert"' in rendered
    assert "/static/dashboard.css" in rendered
    assert "/static/dashboard.js" in rendered


def test_dashboard_filters_links_and_chart_dimensions(tmp_path):
    script = tmp_path / "dashboard-interactions.cjs"
    script.write_text("""
const assert = require('node:assert/strict');
const d = require(process.argv[2]);
const now = new Date().toISOString();
const latest = {price_amount:'100',price_currency:'RUB',price_state:'present',unit_name:'piece',unit_quantity:'1',availability:'available',status:'confirmed',stale:false,observed_at:now};
const products = [
 {product_id:'a',title:'Юбка',variants:[{variant_id:'s',sku:'SKU-S',latest,delta:'10.00'},{variant_id:'m',sku:'SKU-M',latest:{...latest,stale:true},delta:null}]},
 {product_id:'b',title:'Платье',variants:[{variant_id:'b',sku:'SKU-B',latest:{...latest,availability:'unavailable'},delta:'0'}]}
];
assert.equal(d.visibleProducts(products, {search:'sku-m'}).length,1);
assert.deepEqual(d.visibleProducts(products, {filter:'changed'}).map(p=>p.product_id),['a']);
assert.deepEqual(d.visibleProducts(products, {filter:'available'}).map(p=>p.product_id),['a']);
assert.deepEqual(d.visibleProducts(products, {filter:'stale'}).map(p=>p.product_id),['a']);
assert.deepEqual(d.visibleProducts(products).map(p=>p.product_id),['b','a']);
for (const url of ['javascript:alert(1)','data:text/html,x','https://user:pass@fixture.test','file:///tmp/a']) assert.equal(d.safeLink(url),null);
assert.equal(d.safeLink('https://fixture.test/p'),'https://fixture.test/p');
const history = [
 {...latest,variant_id:'s',observation_id:1},
 {...latest,variant_id:'m',observation_id:2},
 {...latest,variant_id:'s',observation_id:3,price_currency:'USD'},
 {...latest,variant_id:'s',observation_id:4,status:'unknown'},
 {...latest,variant_id:'s',observation_id:5,unit_quantity:'2'},
 {...latest,variant_id:'s',observation_id:6,price_amount:null},
 {...latest,variant_id:'s',observation_id:7,observed_at:'2020-01-01T00:00:00Z'},
 {...latest,variant_id:'s',observation_id:8,unit_quantity:'1.0000000000000000001'}
];
const state = d.deriveDashboardState({products,history}, {productId:'a',variantId:'s'});
assert.equal(state.selectedVariantId,'s');
assert.deepEqual(d.chartPoints(state,7).map(o=>o.observation_id),[1]);
assert.equal(state.changes.length,0);
assert.equal(d.formatMoney(null,'RUB'),'Цена неизвестна');
assert.equal(d.formatMoney('1e9999','RUB'),'Цена неизвестна');
assert.equal(d.formatMoney('1e-9999','RUB'),'Цена неизвестна');
const same = d.deriveDashboardState({history:[{...latest,variant_id:'s',observation_id:1,price_amount:'10.0'}, {...latest,variant_id:'s',observation_id:2,price_amount:'10.00'}]});
assert.equal(same.changes.length,0);
for (const change of [{price_state:'unknown'},{observed_at:'not-a-date'}, {observed_at:'2099-01-01T00:00:00Z'}, {unit_quantity:'1.0000000000000000001'}, {price_amount:'1e-9999'}]) {
 const invalid = d.deriveDashboardState({history:[{...latest,variant_id:'s',observation_id:1}, {...latest,...change,variant_id:'s',observation_id:2,price_amount:change.price_amount||'200'}]});
 assert.equal(invalid.changes.length,0);
}
const equivalent = {...state, history:[{...latest,variant_id:'s',observation_id:1,unit_quantity:'1.000'}]};
assert.equal(d.chartPoints(equivalent,7).length,1);

""", encoding="utf-8")
    result = subprocess.run(["node", str(script), str(Path("pricepulse/web/static/dashboard.js").resolve())], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_dashboard_selection_races_and_failed_poll_keep_current_data(tmp_path):
    script = tmp_path / "dashboard-races.cjs"
    script.write_text("""
const assert = require('node:assert/strict');
const d = require(process.argv[2]);
class Element {
 constructor(){this.dataset={};this.children=[];this.events={};this.value='';this.hidden=false;}
 append(...children){this.children.push(...children);} replaceChildren(...children){this.children=children;}
 setAttribute(k,v){this[k]=v;} removeAttribute(k){delete this[k];}
 addEventListener(k,v){this.events[k]=v;} scrollIntoView(){}
}
global.document={createElement:()=>new Element(),createElementNS:()=>new Element()};
global.Option=class extends Element {constructor(t,v){super();this.textContent=t;this.value=v;}};
const elements={};const root={querySelector:s=>(elements[s]||=new Element()),querySelectorAll:()=>[]};
const products=['a','b'].map(id=>({product_id:id,title:id,variants:[{variant_id:id,attributes:{size:id}}]}));
let pending=[];
const loader=selection=>new Promise((resolve,reject)=>pending.push({selection,resolve,reject}));
const make=(selection)=>d.deriveDashboardState({products,generated_at:new Date().toISOString(),totals:{products:2,variants:2,observations:1},history:[{variant_id:selection.variantId||'a',observation_id:1,status:'unknown'}]},selection);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 const c=d.createController(root,loader);
 const first=c.refresh(); pending[0].resolve(make(pending[0].selection)); await first;
 assert.equal(c.getState().selectedVariantId,'a');
 let rows=elements['#products'].children;
 rows[1].children[0].children[0].children[0].children[0].events.click();
 rows=elements['#products'].children;
 rows[0].children[0].children[0].children[0].children[0].events.click();
 assert.equal(pending.length,3);
 pending[2].resolve(make(pending[2].selection));await tick();
 pending[1].resolve(make(pending[1].selection));await tick();
 assert.equal(c.getState().selectedVariantId,'a');
 const failed=c.refresh();pending[3].reject(new Error('offline'));await failed;
 assert.equal(c.getState().selectedVariantId,'a');assert.equal(c.getState().products.length,2);
 assert.equal(elements['#dashboard-error'].hidden,false);
 elements['#search'].events.input({target:{value:'b'}});
 assert.equal(elements['#products'].children.length,1);
 const refresh=c.refresh(); pending[4].resolve(make(pending[4].selection));await refresh;
 assert.equal(elements['#products'].children.length,1);
 assert.equal(c.getState().selectedVariantId,'a');
 const partial=c.refresh();pending[5].resolve({...make(pending[5].selection),history:[],historyError:true});await partial;
 assert.equal(c.getState().history.length,1);assert.equal(c.getState().historyError,true);
 assert.equal(elements['#history-warning'].hidden,false);
 c.dispose();
})().catch(error=>{process.stderr.write(error.stack);process.exit(1);});
""", encoding="utf-8")
    result = subprocess.run(["node", str(script), str(Path("pricepulse/web/static/dashboard.js").resolve())], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
