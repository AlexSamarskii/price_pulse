from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType


class MigrationError(RuntimeError):
    pass


VERSIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"pricepulse_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"cannot load migration {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for attribute in ("version", "upgrade", "downgrade"):
        if not hasattr(module, attribute):
            raise MigrationError(f"migration {path.name} is missing {attribute}")
    return module


def _migrations() -> list[ModuleType]:
    modules = [_load_module(path) for path in sorted(VERSIONS_DIR.glob("*.py")) if path.name != "__init__.py"]
    versions = [module.version for module in modules]
    if len(set(versions)) != len(versions):
        raise MigrationError("migration versions are duplicated")
    return modules


def apply_migrations(connection: sqlite3.Connection, *, versions_dir: Path | None = None) -> list[str]:
    global VERSIONS_DIR
    previous_dir = VERSIONS_DIR
    if versions_dir is not None:
        VERSIONS_DIR = versions_dir
    try:
        modules = _migrations()
        connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        newly_applied = []
        for module in modules:
            if module.version in applied:
                continue
            try:
                module.upgrade(connection)
                connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (module.version,))
                newly_applied.append(module.version)
            except sqlite3.Error as error:
                raise MigrationError(f"migration {module.version} failed: {error}") from error
        return newly_applied
    finally:
        VERSIONS_DIR = previous_dir


def rollback_migrations(connection: sqlite3.Connection, *, versions_dir: Path | None = None) -> str | None:
    global VERSIONS_DIR
    previous_dir = VERSIONS_DIR
    if versions_dir is not None:
        VERSIONS_DIR = versions_dir
    try:
        _migrations()
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
            return None
        row = connection.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            return None
        module = next((candidate for candidate in _migrations() if candidate.version == row[0]), None)
        if module is None:
            raise MigrationError(f"migration {row[0]} is not installed")
        module.downgrade(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (module.version,))
        return module.version
    finally:
        VERSIONS_DIR = previous_dir
