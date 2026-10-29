from __future__ import annotations

import csv
import io
import json
from typing import Any


MAX_EXPORT_ROWS = 10000
MAX_EXPORT_BYTES = 10_000_000

HISTORY_CSV_FIELDS = [
    "observation_id",
    "variant_id",
    "observed_at",
    "ingested_at",
    "status",
    "price_state",
    "price_amount",
    "price_currency",
    "price_kind",
    "price_precision",
    "list_price_amount",
    "list_price_currency",
    "unit_price_amount",
    "unit_price_currency",
    "unit_quantity",
    "unit_name",
    "availability",
    "job_id",
    "raw_hash",
    "title",
    "evidence",
    "provenance",
]


def _formula_safe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return "'" + value if value and value[0] in "=+-@" else value


def serialize_json_rows(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    if len(payload.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise ValueError("JSON export exceeds the byte limit")
    return payload


def serialize_csv_rows(rows: list[dict[str, Any]], *, fields: list[str] | None = None) -> str:
    if not rows:
        return ""
    resolved_fields = fields or sorted({key for row in rows for key in row})
    unknown = set(fields or []) - set(resolved_fields)
    if unknown:
        raise ValueError("CSV fields contain unknown columns")
    import io

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=resolved_fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        serialized: dict[str, Any] = {}
        for field in resolved_fields:
            value = row.get(field)
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            serialized[field] = _formula_safe(value)
        writer.writerow(serialized)
    payload = buffer.getvalue()
    if len(payload.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise ValueError("CSV export exceeds the byte limit")
    return payload


def parse_json_export(payload: str) -> list[dict[str, Any]]:
    if len(payload.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise ValueError("JSON import exceeds the byte limit")
    rows = json.loads(payload)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("JSON export must be a list of objects")
    return rows
