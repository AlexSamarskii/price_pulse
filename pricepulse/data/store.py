from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pricepulse.data.migrations import apply_migrations, rollback_migrations


class DatabaseError(RuntimeError):
    pass


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self._connection: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseError("database is not open")
        return self._connection

    def __enter__(self) -> "Database":
        self._connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        if str(self.path) != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        return self

    def __exit__(self, *_: object) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        if connection.in_transaction:
            raise DatabaseError("nested database transactions are not supported")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def initialize(self) -> None:
        apply_migrations(self.connection)

    def rollback_latest(self) -> None:
        rollback_migrations(self.connection)
