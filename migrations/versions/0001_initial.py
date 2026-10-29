from __future__ import annotations

version = "0001"
description = "PricePulse MVP SQLite schema"


def upgrade(connection) -> None:
    connection.executescript(
        """
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            active_version_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (active_version_id) REFERENCES scraper_versions(version_id)
        );

        CREATE TABLE source_policies (
            policy_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            policy_digest TEXT NOT NULL,
            live_enabled INTEGER NOT NULL CHECK (live_enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
        );

        CREATE TABLE scraper_versions (
            version_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            status TEXT NOT NULL CHECK (status IN ('draft', 'validated', 'active', 'quarantined')),
            goal TEXT NOT NULL,
            code_sha256 TEXT NOT NULL,
            dependencies_sha256 TEXT NOT NULL,
            policy_digest TEXT NOT NULL,
            engine TEXT NOT NULL,
            provider_id TEXT,
            model TEXT,
            fixture_verification TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
        );

        CREATE TABLE products (
            product_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            title TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            attributes_json TEXT NOT NULL,
            review_status TEXT NOT NULL CHECK (review_status IN ('accepted', 'review')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX products_identity ON products(source_id, identity_key);

        CREATE TABLE variants (
            variant_id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            variant_key TEXT NOT NULL,
            sku TEXT,
            attributes_json TEXT NOT NULL,
            packaging_json TEXT NOT NULL,
            unit_name TEXT,
            unit_quantity TEXT,
            review_status TEXT NOT NULL CHECK (review_status IN ('accepted', 'review')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX variants_identity ON variants(product_id, variant_key);

        CREATE TABLE observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('confirmed', 'stale', 'unknown')),
            price_state TEXT NOT NULL CHECK (price_state IN ('present', 'missing', 'unknown')),
            price_amount TEXT,
            price_currency TEXT,
            price_kind TEXT,
            price_precision INTEGER,
            list_price_amount TEXT,
            list_price_currency TEXT,
            unit_price_amount TEXT,
            unit_price_currency TEXT,
            unit_quantity TEXT,
            unit_name TEXT,
            availability TEXT CHECK (availability IN ('available', 'unavailable', 'unknown')),
            job_id TEXT,
            raw_hash TEXT,
            evidence_json TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id) ON DELETE RESTRICT,
            UNIQUE (variant_id, observed_at, raw_hash, job_id)
        );

        CREATE TRIGGER observations_append_only_update
        BEFORE UPDATE ON observations
        BEGIN
            SELECT RAISE(ABORT, 'observations are append-only');
        END;

        CREATE TRIGGER observations_append_only_delete
        BEFORE DELETE ON observations
        BEGIN
            SELECT RAISE(ABORT, 'observations are append-only');
        END;

        CREATE TABLE collection_jobs (
            job_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'blocked', 'cancelled')),
            scraper_version_id TEXT,
            lease_owner TEXT,
            lease_expires_at TEXT,
            attempts INTEGER NOT NULL CHECK (attempts >= 0),
            retry_budget INTEGER NOT NULL CHECK (retry_budget >= 0),
            backoff_seconds INTEGER NOT NULL CHECK (backoff_seconds >= 0),
            result_digest TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE,
            FOREIGN KEY (scraper_version_id) REFERENCES scraper_versions(version_id)
        );

        CREATE UNIQUE INDEX collection_jobs_idempotency ON collection_jobs(idempotency_key);

        CREATE TABLE job_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            result_digest TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES collection_jobs(job_id) ON DELETE CASCADE,
            UNIQUE (job_id, result_digest)
        );

        CREATE TABLE schedules (
            schedule_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            schedule TEXT NOT NULL,
            max_concurrency INTEGER NOT NULL CHECK (max_concurrency > 0),
            requests_per_minute INTEGER NOT NULL CHECK (requests_per_minute > 0),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
        );

        CREATE TABLE alert_rules (
            rule_id TEXT PRIMARY KEY,
            metric TEXT NOT NULL,
            threshold TEXT NOT NULL,
            currency TEXT NOT NULL,
            unit_name TEXT,
            variant_id TEXT,
            source_id TEXT,
            cooldown_seconds INTEGER NOT NULL CHECK (cooldown_seconds >= 0),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id) ON DELETE CASCADE,
            FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
        );

        CREATE TABLE alert_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT NOT NULL,
            observation_id INTEGER NOT NULL,
            variant_id TEXT NOT NULL,
            delta_amount TEXT NOT NULL,
            delta_currency TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY (rule_id) REFERENCES alert_rules(rule_id) ON DELETE CASCADE,
            FOREIGN KEY (observation_id) REFERENCES observations(observation_id) ON DELETE RESTRICT,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id) ON DELETE CASCADE,
            UNIQUE (rule_id, observation_id, dedupe_key)
        );

        CREATE TABLE data_quality_issues (
            issue_id TEXT PRIMARY KEY,
            issue_type TEXT NOT NULL,
            details_json TEXT NOT NULL,
            source_id TEXT,
            job_id TEXT,
            variant_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE,
            FOREIGN KEY (job_id) REFERENCES collection_jobs(job_id) ON DELETE SET NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id) ON DELETE CASCADE
        );
        """
    )


def downgrade(connection) -> None:
    connection.executescript(
        """
        DROP TRIGGER IF EXISTS observations_append_only_update;
        DROP TRIGGER IF EXISTS observations_append_only_delete;
        DROP TABLE IF EXISTS data_quality_issues;
        DROP TABLE IF EXISTS alert_events;
        DROP TABLE IF EXISTS alert_rules;
        DROP TABLE IF EXISTS schedules;
        DROP TABLE IF EXISTS job_results;
        DROP TABLE IF EXISTS collection_jobs;
        DROP TABLE IF EXISTS observations;
        DROP TABLE IF EXISTS variants;
        DROP TABLE IF EXISTS products;
        DROP TABLE IF EXISTS scraper_versions;
        DROP TABLE IF EXISTS source_policies;
        DROP TABLE IF EXISTS sources;
        """
    )
