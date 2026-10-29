from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

from pricepulse.collectors.capture_sanitizer import sanitize_for_log
from pricepulse.data.store import Database


MAX_PRODUCTS = 200
MAX_VARIANTS = 1000
MAX_SNAPSHOT_BYTES = 1_500_000


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _decimal(value):
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _quantity_identity(value):
    quantity = _decimal(value)
    if quantity is None:
        return None
    _, digits, exponent = quantity.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return "".join(map(str, digits)) + "e" + str(exponent)


def _attributes(raw):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data[key])[:160] for key in ("color", "size", "size_system") if data.get(key) is not None}


def _observation(row, prefix, now, interval):
    if row[prefix + "id"] is None:
        return None
    observed = _time(row[prefix + "at"])
    stale = row[prefix + "status"] == "stale" or observed is None or observed > now + timedelta(minutes=2) or (now - observed).total_seconds() > max(3600, interval * 2)
    confirmed = row[prefix + "status"] == "confirmed" and row[prefix + "price_state"] == "present"
    currency = row[prefix + "currency"]
    unit = row[prefix + "unit"]
    quantity = _decimal(row[prefix + "quantity"])
    dimensions_valid = isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) and (unit is None or isinstance(unit, str) and 0 < len(unit) <= 100) and quantity is not None
    amount = _decimal(row[prefix + "amount"]) if confirmed and dimensions_valid else None
    if amount is not None and (not Decimal("0.000001") <= amount <= Decimal("1e12") or len(amount.as_tuple().digits) > 30):
        amount = None
    return {"observation_id": row[prefix + "id"], "observed_at": row[prefix + "at"],
            "status": row[prefix + "status"], "price_amount": str(amount) if amount is not None else None,
            "price_currency": row[prefix + "currency"], "availability": row[prefix + "availability"],
            "unit_name": row[prefix + "unit"], "unit_quantity": row[prefix + "quantity"], "stale": stale}


def _read_sources(connection, source_id, now):
    where, params = ("WHERE s.source_id = ?", (source_id,)) if source_id else ("", ())
    rows = connection.execute(
        f"""SELECT s.source_id, s.name, s.base_url, p.live_enabled, cs.config_json, schedule.enabled AS schedule_enabled,
                   schedule.schedule AS schedule_json,
                   cs.retry_after, cs.last_success_at, cs.last_error,
                   (SELECT MAX(j.created_at) FROM collection_jobs AS j
                    WHERE j.source_id = s.source_id AND instr(j.idempotency_key, 'schedule:catalog_' || s.source_id || ':') = 1) AS last_job_at
            FROM sources AS s LEFT JOIN source_policies AS p ON p.policy_id = s.current_policy_id AND p.source_id = s.source_id
            LEFT JOIN collection_sources AS cs ON cs.source_id = s.source_id
            LEFT JOIN schedules AS schedule ON schedule.schedule_id = 'catalog_' || s.source_id AND schedule.source_id = s.source_id
            {where} ORDER BY s.created_at, s.source_id LIMIT 101""", params,
    ).fetchall()
    sources = []
    for row in rows[:100]:
        try:
            config = json.loads(row["config_json"]) if row["config_json"] else {}
        except (ValueError, TypeError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        try:
            schedule = json.loads(row["schedule_json"] or "{}")
        except (ValueError, TypeError):
            schedule = {}
        interval = schedule.get("seconds") if isinstance(schedule, dict) and schedule.get("type") == "interval" else None
        interval = interval if type(interval) is int and 60 <= interval <= 31536000 else None
        enabled = bool(config.get("enabled", False) and row["live_enabled"] == 1 and row["schedule_enabled"] == 1 and interval)
        last = _time(row["last_job_at"])
        retry = _time(row["retry_after"])
        next_run = (last + timedelta(seconds=interval) if last else now) if enabled and interval else None
        if next_run and retry:
            next_run = max(next_run, retry)
        sources.append({"source_id": row["source_id"], "name": row["name"][:100], "base_url": row["base_url"][:2048],
                        "enabled": enabled, "live_enabled": row["live_enabled"] == 1, "interval_seconds": interval,
                        "next_run_at": next_run.isoformat() if next_run else None, "last_success_at": row["last_success_at"],
                        "retry_after": row["retry_after"], "has_error": bool(row["last_error"]),
                        "cooldown": bool(retry and retry > now)})
    return sources, len(rows) > 100


def _read_products(connection, source_id, sources, now):
    where, params = ("WHERE p.source_id = ?", (source_id,)) if source_id else ("", ())
    rows = connection.execute(
        f"""SELECT p.product_id, p.source_id, p.title, p.canonical_url, p.review_status,
                   (SELECT COUNT(*) FROM variants AS v WHERE v.product_id = p.product_id) AS variant_total
            FROM products AS p {where} ORDER BY p.created_at, p.product_id LIMIT ?""", (*params, MAX_PRODUCTS + 1),
    ).fetchall()
    products = {row["product_id"]: {**dict(row), "title": row["title"][:500], "variants": []} for row in rows[:MAX_PRODUCTS]}
    if not products:
        return [], len(rows) > MAX_PRODUCTS, False
    markers = ",".join("?" for _ in products)
    variants = connection.execute(
        f"""SELECT v.product_id, v.variant_id, v.sku, v.attributes_json,
            o.observation_id AS current_id, o.observed_at AS current_at, o.status AS current_status,
            o.price_state AS current_price_state, o.price_amount AS current_amount, o.price_currency AS current_currency,
            o.availability AS current_availability, o.unit_name AS current_unit, o.unit_quantity AS current_quantity,
            previous.observation_id AS previous_id, previous.observed_at AS previous_at, previous.status AS previous_status,
            previous.price_state AS previous_price_state, previous.price_amount AS previous_amount,
            previous.price_currency AS previous_currency, previous.availability AS previous_availability,
            previous.unit_name AS previous_unit, previous.unit_quantity AS previous_quantity
            FROM variants AS v LEFT JOIN observations AS o ON o.observation_id = (
              SELECT observation_id FROM observations WHERE variant_id = v.variant_id
              ORDER BY observed_at DESC, observation_id DESC LIMIT 1)
            LEFT JOIN observations AS previous ON previous.observation_id = (
              SELECT observation_id FROM observations WHERE variant_id = v.variant_id
                AND status = 'confirmed' AND price_state = 'present'
                AND (observed_at < o.observed_at OR (observed_at = o.observed_at AND observation_id < o.observation_id))
                AND price_currency = o.price_currency AND unit_name IS o.unit_name
                AND dashboard_quantity(unit_quantity) = dashboard_quantity(o.unit_quantity)
              ORDER BY observed_at DESC, observation_id DESC LIMIT 1)
            WHERE v.product_id IN ({markers}) ORDER BY v.product_id, v.variant_key LIMIT ?""",
        (*products, MAX_VARIANTS + 1),
    ).fetchall()
    intervals = {s["source_id"]: s["interval_seconds"] or 1800 for s in sources}
    for row in variants[:MAX_VARIANTS]:
        product = products[row["product_id"]]
        interval = intervals.get(product["source_id"], 1800)
        latest = _observation(row, "current_", now, interval)
        previous = _observation(row, "previous_", now, interval)
        delta = None
        if latest and previous and not latest["stale"] and not previous["stale"] and latest["price_amount"] is not None and previous["price_amount"] is not None:
            with localcontext() as context:
                context.prec = 64
                delta = str(Decimal(latest["price_amount"]) - Decimal(previous["price_amount"]))
        product["variants"].append({"variant_id": row["variant_id"], "sku": (row["sku"] or "")[:160],
                                    "attributes": _attributes(row["attributes_json"]),
                                    "latest": latest, "previous": previous, "delta": delta})
    for product in products.values():
        latest = [v["latest"] for v in product["variants"] if v["latest"] and v["latest"]["price_amount"] is not None]
        dimensions = {(p["price_currency"], p["unit_name"], Decimal(p["unit_quantity"])) for p in latest}
        product["currency"] = latest[0]["price_currency"] if len(dimensions) == 1 else None
        product["price_min"] = str(min(Decimal(p["price_amount"]) for p in latest)) if len(dimensions) == 1 else None
        product["price_max"] = str(max(Decimal(p["price_amount"]) for p in latest)) if len(dimensions) == 1 else None
        product["mixed_dimensions"] = len(dimensions) > 1
        product["partial"] = len(product["variants"]) < product["variant_total"]
        if len(product["canonical_url"]) > 2048:
            product["canonical_url"] = ""
    return list(products.values()), len(rows) > MAX_PRODUCTS, len(variants) > MAX_VARIANTS


def dashboard_snapshot(database_path, *, source_id=None):
    now = datetime.now(timezone.utc)
    with Database(database_path) as database:
        connection = database.connection
        connection.create_function("dashboard_quantity", 1, _quantity_identity, deterministic=True)
        connection.execute("BEGIN")
        try:
            sources, sources_truncated = _read_sources(connection, source_id, now)
            if source_id and not sources:
                raise LookupError("source does not exist")
            products, products_truncated, variants_truncated = _read_products(connection, source_id, sources, now)
            where, params = ("WHERE source_id = ?", (source_id,)) if source_id else ("", ())
            jobs = [dict(row) for row in connection.execute(
                f"SELECT job_id, source_id, status, attempts, created_at, updated_at FROM collection_jobs {where} "
                "ORDER BY created_at DESC, job_id DESC LIMIT 20", params)]
            quality = [dict(row) for row in connection.execute(
                f"SELECT issue_id, source_id, issue_type, created_at FROM data_quality_issues {where} "
                "ORDER BY created_at DESC LIMIT 20", params)]
            alert_where = "WHERE p.source_id = ?" if source_id else ""
            alerts = [dict(row) for row in connection.execute(
                f"""SELECT e.event_id, e.rule_id, e.variant_id, e.observation_id, e.delta_amount,
                           e.delta_currency, e.occurred_at, p.title, p.source_id
                    FROM alert_events AS e JOIN variants AS v ON v.variant_id = e.variant_id
                    JOIN products AS p ON p.product_id = v.product_id {alert_where}
                    ORDER BY e.occurred_at DESC, e.event_id DESC LIMIT 20""", params)]
            for alert in alerts:
                alert["title"] = alert["title"][:500]
            counts = {}
            for table in ("products", "variants", "observations"):
                if table == "products":
                    query = f"SELECT COUNT(*) FROM products {where}"
                elif table == "variants":
                    query = f"SELECT COUNT(*) FROM variants AS v JOIN products AS p ON p.product_id=v.product_id {where}"
                else:
                    query = f"SELECT COUNT(*) FROM observations AS o JOIN variants AS v ON v.variant_id=o.variant_id JOIN products AS p ON p.product_id=v.product_id {where}"
                if source_id:
                    query = query.replace("WHERE source_id", "WHERE p.source_id" if table != "products" else "WHERE source_id")
                counts[table] = connection.execute(query, params).fetchone()[0]
            snapshot = {"generated_at": now.isoformat(), "sources": sources, "products": products,
                        "jobs": jobs, "alerts": alerts, "quality": quality, "totals": counts,
                        "truncated": {"sources": sources_truncated, "products": products_truncated, "variants": variants_truncated}}
            snapshot = sanitize_for_log(snapshot)
            for key in ("products", "alerts", "quality", "jobs", "sources"):
                while len(json.dumps(snapshot, ensure_ascii=False).encode()) > MAX_SNAPSHOT_BYTES and snapshot[key]:
                    snapshot[key].pop()
                    snapshot["truncated"][key] = True
            return snapshot
        finally:
            connection.execute("ROLLBACK")
