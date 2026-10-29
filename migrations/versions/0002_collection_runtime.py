from __future__ import annotations

version = "0002"
description = "Durable catalog collection configuration and pacing"


def upgrade(connection) -> None:
    connection.execute("ALTER TABLE sources ADD COLUMN current_policy_id TEXT")
    connection.execute(
        "UPDATE sources SET current_policy_id = (SELECT policy_id FROM source_policies AS p "
        "WHERE p.source_id = sources.source_id ORDER BY created_at DESC, policy_id DESC LIMIT 1)"
    )
    connection.execute(
        """CREATE TABLE collection_sources (
            source_id TEXT PRIMARY KEY,
            config_json TEXT NOT NULL,
            version_id TEXT NOT NULL,
            next_request_at TEXT NOT NULL DEFAULT '',
            retry_after TEXT NOT NULL DEFAULT '',
            last_success_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        )"""
    )


def downgrade(connection) -> None:
    connection.execute("DROP TABLE collection_sources")
    connection.execute("ALTER TABLE sources DROP COLUMN current_policy_id")
