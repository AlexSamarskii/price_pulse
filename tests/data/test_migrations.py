import sqlite3

import pytest

from pricepulse.data.migrations import apply_migrations, rollback_migrations
from pricepulse.data.store import Database


def test_initial_migration_upgrades_and_rolls_back():
    connection = sqlite3.connect(":memory:")
    try:
        assert apply_migrations(connection) == ["0001"]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
        ).fetchone()
        connection.execute(
            "INSERT INTO observations VALUES (NULL, 'variant', '2026-01-01', '2026-01-01', 'confirmed', 'missing',"
            " NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'unknown', NULL, 'hash', '{}', '{}')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM observations")
        assert rollback_migrations(connection) == "0001"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
        ).fetchone() is None
    finally:
        connection.close()


def test_database_rollback_latest_rolls_back(tmp_path):
    with Database(tmp_path / "migration-lifecycle.db") as database:
        database.initialize()
        assert database.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
        ).fetchone()
        database.rollback_latest()
        assert database.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
        ).fetchone() is None
