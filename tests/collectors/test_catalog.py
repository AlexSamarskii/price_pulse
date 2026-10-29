from __future__ import annotations

import hashlib
import json
import httpx
import pytest

from pricepulse.collectors.catalog import make_version
from pricepulse.collectors.offline_gate import run_offline_gate
from pricepulse.runtime import ExecutionRequest


def url(number):
    return f"https://limestore.com/ru_ru/product/{number}-model_{number}_black"


def card(number, price=3500):
    values = []

    def pack(value):
        index = len(values)
        values.append(None)
        if isinstance(value, dict):
            values[index] = {k: pack(v) for k, v in value.items()}
        elif isinstance(value, list):
            values[index] = [pack(v) for v in value]
        else:
            values[index] = value
        return index

    title = f"Product {number}"
    selected = {"name": title, "code": f"model_{number}_black", "color": {"name": "black"},
                "skus": [{"skuId": f"sku-{number}-{size}", "price": price, "old_price": None,
                          "stock": {"online": 5}, "size": {"value": size, "unit": "INT"}}
                         for size in ("S", "M")]}
    pack(selected)
    pack({"name": title, "code": f"model_{number}_red", "color": {"name": "red"},
          "skus": [{"skuId": "unrelated-red", "price": 9999, "stock": {"online": 0},
                    "size": {"value": "L", "unit": "INT"}}]})
    item = {"@type": "Product", "name": title, "sku": str(number), "url": url(number), "color": "black",
            "offers": {"@type": "Offer", "price": price, "priceCurrency": "RUB"}}
    return ('<html><script type="application/ld+json">' + json.dumps(item)
            + '</script><script type="application/json" id="__NUXT_DATA__">' + json.dumps(values)
            + '</script></html>')


def listing():
    urls = [url(101), url(102), "https://127.0.0.1/ru_ru/product/escape", "https://elsewhere.test/ru_ru/product/escape",
            url(103) + "?session=untrusted", "https://limestore.com/ru_ru/cart", url(101)]
    item = {"@type": "CollectionPage", "mainEntity": {"itemListElement": [{"url": u} for u in urls]}}
    return '<html><script type="application/ld+json">' + json.dumps(item) + '</script></html>'


def config():
    return {"adapter": "limestore", "enabled": True, "catalog_urls": ["https://limestore.com/ru_ru/catalog/women_new"],
            "interval_seconds": 60, "cooldown_seconds": 60, "max_products": 2, "max_catalog_pages": 1,
            "policy": {"source_id": "limestore_ru", "base_url": "https://limestore.com/ru_ru/catalog/women_new",
                       "allowed_hosts": ["limestore.com"],
                       "allowed_path_prefixes": ["/ru_ru/catalog/women_new", "/ru_ru/product/", "/robots.txt"],
                       "robots_policy": "honor", "max_pages": 3, "requests_per_minute": 60,
                       "max_concurrency": 1, "timeout_seconds": 5, "allowed_currencies": ["RUB"], "live_enabled": True}}


def payload(html, mode, source_url):
    return json.dumps({"html": html, "mode": mode, "source_id": "limestore_ru", "url": source_url,
                       "capture_sha256": hashlib.sha256(html.encode()).hexdigest(),
                       "observed_at": "2026-10-29T20:00:00.000+00:00"}).encode()


def test_catalog_and_selected_variants_execute_in_container(runtime_runner):
    from pricepulse.collectors.source_onboarding import onboard_source

    cfg = config()
    onboarding = onboard_source(cfg["policy"], "Collect catalog")
    version = make_version(onboarding.policy, onboarding.policy_digest)
    request = ExecutionRequest(job_id="catalog-discovery", script=version["code"].encode(), env={}, timeout_seconds=10,
                               input_data=payload(listing(), "catalog", cfg["catalog_urls"][0]))
    output = runtime_runner.run(request)
    assert output.status == "completed" and output.exit_code == 0 and output.cleanup_completed
    assert json.loads(output.stdout) == {"product_urls": [url(101), url(102)], "next_urls": []}
    result = run_offline_gate(version, runtime_runner, job_id="selected-card",
                              input_data=payload(card(101), "product", url(101)))
    assert result.status == "passed"
    assert len(result.products) == 2
    assert {p["sku"] for p in result.products} == {"sku-101-S", "sku-101-M"}
    assert all(p["attributes"]["color"] == "black" and p["price"] == "3500" for p in result.products)
    assert all(p["evidence"]["source_url"] == url(101) for p in result.products)
    bad = run_offline_gate(version, runtime_runner, job_id="empty-card",
                           input_data=payload("<html>empty</html>", "product", url(101)))
    assert bad.status == "failed" and bad.products == ()


def test_query_redirect_is_rejected_before_actual_send():
    from pricepulse.collectors.catalog import PacedBroker, CollectionBlocked
    paths, admissions = [], []
    def handle(request):
        paths.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(302, headers={"location": url(101) + "?page=2"})
    broker = PacedBroker(config()["policy"], lambda: admissions.append(True),
                        transport=httpx.MockTransport(handle), resolver=lambda _: "93.184.216.34")
    try:
        with pytest.raises(CollectionBlocked, match="redirect is outside"):
            broker.request("GET", config()["catalog_urls"][0])
        assert len(paths) == len(admissions) == len(broker.audit) == 2
        assert all("?" not in path for path in paths)
    finally:
        broker.close()


@pytest.mark.parametrize("status,body,blocked", [(401, "denied", True), (403, "denied", True),
    (429, "limited", True), (200, "<h1>verify you are human</h1>", True), (200, "", False), (500, "error", False)])
def test_bad_responses_never_reach_extraction(status, body, blocked):
    from pricepulse.collectors.catalog import CatalogCollector, PacedBroker, CollectionBlocked, CollectionError
    cfg = config()
    def handle(request):
        return httpx.Response(200, text="User-agent: *\nAllow: /\n") if request.url.path == "/robots.txt" else httpx.Response(status, text=body)
    broker = PacedBroker(cfg["policy"], lambda: None, transport=httpx.MockTransport(handle), resolver=lambda _: "93.184.216.34")
    collector = CatalogCollector(cfg, {}, None, broker)
    try:
        with pytest.raises(CollectionBlocked if blocked else CollectionError):
            collector.capture(url(101), "product")
    finally:
        broker.close()
