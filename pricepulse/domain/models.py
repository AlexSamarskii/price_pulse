from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


JOB_STATUSES = {
    "queued",
    "running",
    "succeeded",
    "failed",
    "blocked",
    "cancelled",
}
SCRAPER_VERSION_STATUSES = {"draft", "validated", "active", "quarantined"}
OBSERVATION_STATUSES = {"confirmed", "stale", "unknown"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(slots=True)
class Source:
    source_id: str
    name: str
    base_url: str
    active_version_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class SourcePolicy:
    policy_id: str
    source_id: str
    policy: dict[str, Any]
    policy_digest: str
    live_enabled: bool
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ScraperVersion:
    version_id: str
    source_id: str
    candidate_id: str
    revision: int
    status: str
    goal: str
    code_sha256: str
    dependencies_sha256: str
    policy_digest: str
    engine: str
    provider_id: str | None = None
    model: str | None = None
    fixture_verification: str = "pending_offline_gate"
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.status not in SCRAPER_VERSION_STATUSES:
            raise ValueError("scraper version status is invalid")
        if self.revision < 1:
            raise ValueError("scraper version revision must be positive")


@dataclass(slots=True)
class Product:
    product_id: str | None
    source_id: str
    identity_key: str
    title: str
    canonical_url: str
    attributes: dict[str, Any] = field(default_factory=dict)
    review_status: str = "accepted"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Variant:
    variant_id: str | None
    product_id: str
    variant_key: str
    sku: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    packaging: dict[str, Any] = field(default_factory=dict)
    unit_name: str | None = None
    unit_quantity: str | None = None
    review_status: str = "accepted"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Observation:
    observation_id: int | None
    variant_id: str
    observed_at: str
    ingested_at: str
    status: str
    price_state: str
    price_amount: str | None = None
    price_currency: str | None = None
    price_kind: str | None = None
    price_precision: int | None = None
    list_price_amount: str | None = None
    list_price_currency: str | None = None
    unit_price_amount: str | None = None
    unit_price_currency: str | None = None
    unit_quantity: str | None = None
    unit_name: str | None = None
    availability: str | None = None
    job_id: str | None = None
    raw_hash: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in OBSERVATION_STATUSES:
            raise ValueError("observation status is invalid")
        if self.status != "confirmed" and self.price_amount is not None:
            raise ValueError("only confirmed observations may contain a price")


@dataclass(slots=True)
class CollectionJob:
    job_id: str
    source_id: str
    idempotency_key: str
    status: str
    scraper_version_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    attempts: int = 0
    retry_budget: int = 3
    backoff_seconds: int = 0
    result_digest: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.status not in JOB_STATUSES:
            raise ValueError("collection job status is invalid")


@dataclass(slots=True)
class Schedule:
    schedule_id: str
    source_id: str
    schedule: str
    max_concurrency: int
    requests_per_minute: int
    enabled: bool = True
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class AlertRule:
    rule_id: str
    metric: str
    threshold: str
    currency: str
    unit_name: str | None = None
    variant_id: str | None = None
    source_id: str | None = None
    cooldown_seconds: int = 0
    enabled: bool = True
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class AlertEvent:
    event_id: int | None
    rule_id: str
    observation_id: int
    variant_id: str
    delta_amount: str
    delta_currency: str
    dedupe_key: str
    occurred_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class DataQualityIssue:
    issue_id: str
    issue_type: str
    details: dict[str, Any]
    source_id: str | None = None
    job_id: str | None = None
    variant_id: str | None = None
    created_at: str = field(default_factory=utc_now)
