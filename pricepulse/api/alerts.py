from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import AlertRule


MAX_COOLDOWN_SECONDS = 30 * 24 * 60 * 60


class ResourceNotFound(LookupError):
    pass


class AlertService:
    def __init__(self, database_path: str):
        self.database_path = database_path

    def upsert_rule(self, payload: dict[str, Any]) -> str:
        rule_id = payload.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError("rule_id must be a nonempty string")
        metric = payload.get("metric")
        if metric not in {"price_change", "availability"}:
            raise ValueError("metric must be price_change or availability")
        threshold = payload.get("threshold")
        try:
            threshold_value = Decimal(str(threshold))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("threshold must be decimal") from exc
        if not threshold_value.is_finite():
            raise ValueError("threshold must be finite")
        if threshold_value < 0:
            raise ValueError("threshold must be nonnegative")
        currency = payload.get("currency")
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isupper():
            raise ValueError("currency must be a three-letter uppercase code")
        cooldown = payload.get("cooldown_seconds", 0)
        if isinstance(cooldown, bool) or not isinstance(cooldown, int) or not 0 <= cooldown <= MAX_COOLDOWN_SECONDS:
            raise ValueError("cooldown_seconds is out of range")
        variant_id = payload.get("variant_id")
        source_id = payload.get("source_id")
        if variant_id is not None and not isinstance(variant_id, str):
            raise ValueError("variant_id must be a string")
        if source_id is not None and not isinstance(source_id, str):
            raise ValueError("source_id must be a string")
        unit_name = payload.get("unit_name")
        if unit_name is not None and not isinstance(unit_name, str):
            raise ValueError("unit_name must be a string or null")
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        with Database(self.database_path) as database:
            repository = DataRepository(database)
            variant_source = None
            if variant_id is not None and database.connection.execute(
                "SELECT 1 FROM variants WHERE variant_id = ?", (variant_id,)
            ).fetchone() is None:
                raise ResourceNotFound("variant does not exist")
            if variant_id is not None:
                variant_source = database.connection.execute(
                    """
                    SELECT p.source_id FROM variants AS v
                    JOIN products AS p ON p.product_id = v.product_id
                    WHERE v.variant_id = ?
                    """,
                    (variant_id,),
                ).fetchone()[0]
            if source_id is not None and database.connection.execute(
                "SELECT 1 FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone() is None:
                raise ResourceNotFound("source does not exist")
            if variant_source is not None and source_id is not None and variant_source != source_id:
                raise ValueError("variant does not belong to source")
            rule = AlertRule(
                rule_id=rule_id,
                metric=metric,
                threshold=str(threshold_value),
                currency=currency,
                unit_name=unit_name or None,
                variant_id=variant_id,
                source_id=source_id,
                cooldown_seconds=cooldown,
                enabled=enabled,
            )
            repository.upsert_alert_rule(rule)
        return rule_id

    def evaluate(self) -> dict[str, int]:
        created = 0
        evaluated = 0
        with Database(self.database_path) as database:
            connection = database.connection
            repository = DataRepository(database)
            rules = connection.execute(
                """
                SELECT rule_id, metric, threshold, currency, unit_name, variant_id,
                       source_id, cooldown_seconds, enabled
                FROM alert_rules WHERE enabled = 1
                """
            ).fetchall()
            for rule in rules:
                evaluation_time = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                query = """
                    SELECT o.observation_id, o.variant_id, o.price_amount, o.price_currency,
                           o.unit_name, o.observed_at, o.availability
                    FROM observations AS o
                    JOIN variants AS av ON av.variant_id = o.variant_id
                    JOIN products AS ap ON ap.product_id = av.product_id
                    WHERE o.status = 'confirmed'
                """
                params: list[Any] = []
                if rule["metric"] == "price_change":
                    query += " AND o.price_state = 'present' AND o.price_currency = ?"
                    params.append(rule["currency"])
                if rule["variant_id"] is not None:
                    query += " AND o.variant_id = ?"
                    params.append(rule["variant_id"])
                if rule["source_id"] is not None:
                    query += " AND ap.source_id = ?"
                    params.append(rule["source_id"])
                query += " ORDER BY o.observed_at, o.observation_id"
                rows = connection.execute(query, params).fetchall()
                previous: dict[str, Decimal | str] = {}
                for row in rows:
                    variant_id = row["variant_id"]
                    if rule["unit_name"] is not None and row["unit_name"] != rule["unit_name"]:
                        continue
                    last = previous.get(variant_id)
                    if rule["metric"] == "availability":
                        availability = row["availability"]
                        if availability not in {"available", "unavailable"}:
                            previous.pop(variant_id, None)
                            continue
                        previous[variant_id] = availability
                        if last is None:
                            continue
                        evaluated += 1
                        if last == availability:
                            continue
                        delta_amount = f"{last} -> {availability}"
                        delta_currency = ""
                        dedupe_key = f"{rule['rule_id']}:availability:{variant_id}:{row['observation_id']}"
                    else:
                        amount = Decimal(row["price_amount"])
                        previous[variant_id] = amount
                        if last is None:
                            continue
                        evaluated += 1
                        delta = amount - last
                        if delta == 0 or abs(delta) < Decimal(rule["threshold"]):
                            continue
                        delta_amount = str(delta)
                        delta_currency = rule["currency"]
                        if int(rule["cooldown_seconds"]) == 0:
                            cooldown_bucket = "once"
                        else:
                            cooldown_bucket = str(int(datetime.fromisoformat(evaluation_time).timestamp()) // int(rule["cooldown_seconds"]))
                        dedupe_key = f"{rule['rule_id']}:{variant_id}:{row['observation_id']}:{delta}:{cooldown_bucket}"
                    if connection.execute(
                        "SELECT 1 FROM alert_events WHERE dedupe_key = ?", (dedupe_key,),
                    ).fetchone() is not None:
                        continue
                    event_id = repository.record_alert_event(
                        rule_id=rule["rule_id"],
                        variant_id=variant_id,
                        observation_id=int(row["observation_id"]),
                        delta_amount=delta_amount,
                        delta_currency=delta_currency,
                        dedupe_key=dedupe_key,
                        occurred_at=evaluation_time,
                        cooldown_seconds=int(rule["cooldown_seconds"]),
                    )
                    if event_id is not None:
                        created += 1
        return {"evaluated": evaluated, "created": created, "missing": 0}
