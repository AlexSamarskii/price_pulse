from __future__ import annotations

import json
from pathlib import Path

from pricepulse.collectors.catalog import allowed_url, canonical_json, make_version
from pricepulse.collectors.source_onboarding import onboard_source
from pricepulse.data.repository import DataRepository
from pricepulse.data.store import Database
from pricepulse.domain.models import Schedule, ScraperVersion, Source, SourcePolicy, utc_now


def validate_config(raw):
    required = {"adapter", "policy", "catalog_urls", "interval_seconds", "max_products", "max_catalog_pages"}
    if not isinstance(raw, dict) or set(raw) - required - {"enabled", "cooldown_seconds"} or required - set(raw):
        raise ValueError("collection config has missing or unexpected fields")
    if raw["adapter"] != "limestore":
        raise ValueError("unsupported collection adapter")
    config = dict(raw)
    onboarded = onboard_source(config["policy"], "Collect approved LIME catalog prices and online variants")
    config["policy"] = onboarded.policy
    if onboarded.policy["allowed_hosts"] != ["limestore.com"] or not onboarded.policy["live_enabled"]:
        raise ValueError("LIME collection requires its explicit live host and robots policy")
    urls = config["catalog_urls"]
    if not isinstance(urls, list) or not 1 <= len(urls) <= 10 or not all(
        isinstance(u, str) and allowed_url(u, config["policy"], "catalog") for u in urls
    ):
        raise ValueError("catalog URLs are outside the source contract")
    config.setdefault("enabled", True)
    config.setdefault("cooldown_seconds", 1800)
    if type(config["enabled"]) is not bool:
        raise ValueError("collection enabled must be boolean")
    for key, minimum, maximum in (("interval_seconds", 60, 86400), ("max_products", 1, 50),
                                  ("max_catalog_pages", len(urls), 10), ("cooldown_seconds", 60, 86400)):
        if type(config[key]) is not int or not minimum <= config[key] <= maximum:
            raise ValueError(f"collection {key} is out of range")
    if config["max_catalog_pages"] + config["max_products"] > onboarded.policy["max_pages"]:
        raise ValueError("collection page budget is smaller than the catalog and card limits")
    if onboarded.policy["max_concurrency"] != 1:
        raise ValueError("catalog worker requires source concurrency=1")
    return config, make_version(onboarded.policy, onboarded.policy_digest)


def load_configs(path):
    path = Path(path)
    if any(part == ".idea" or part == ".env" or part.startswith(".env.") for part in path.parts):
        raise ValueError("sensitive config paths are prohibited")
    if path.stat().st_size > 65536:
        raise ValueError("collection config exceeds the size limit")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"sources"} or not isinstance(payload["sources"], list):
        raise ValueError("config must contain a sources array")
    if not 1 <= len(payload["sources"]) <= 10:
        raise ValueError("config source count is out of range")
    configs = [validate_config(raw)[0] for raw in payload["sources"]]
    if len({c["policy"]["source_id"] for c in configs}) != len(configs):
        raise ValueError("duplicate collection source")
    return configs


def register_collection(database_path, config):
    config, version = validate_config(config)
    source_id = config["policy"]["source_id"]
    with Database(database_path) as database:
        database.initialize()
        repository = DataRepository(database)
        existing = database.connection.execute("SELECT 1 FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        if existing is None:
            repository.upsert_source(Source(source_id, "LIME RU", config["policy"]["base_url"]))
        repository.upsert_policy(SourcePolicy("collection_" + version["policy_digest"][:16], source_id,
            config["policy"], version["policy_digest"], True))
        fields = {key: version[key] for key in ("version_id", "source_id", "candidate_id", "revision", "goal",
                  "code_sha256", "dependencies_sha256", "policy_digest", "engine", "provider_id", "model")}
        if database.connection.execute("SELECT 1 FROM scraper_versions WHERE version_id = ?", (version["version_id"],)).fetchone() is None:
            repository.upsert_scraper_version(ScraperVersion(**fields, status="draft"))
        repository.upsert_schedule(Schedule("catalog_" + source_id, source_id,
            canonical_json({"type": "interval", "seconds": config["interval_seconds"]}),
            max_concurrency=1, requests_per_minute=config["policy"]["requests_per_minute"], enabled=config["enabled"]))
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO collection_sources(source_id, config_json, version_id, updated_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET
                   config_json = excluded.config_json, version_id = excluded.version_id, updated_at = excluded.updated_at""",
                (source_id, canonical_json(config), version["version_id"], utc_now()),
            )
    return config, version
