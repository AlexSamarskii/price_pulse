from __future__ import annotations

from pricepulse.api.routes import run_control_plane


def create_control_plane(database_path: str, *, host: str = "127.0.0.1", port: int = 8000):
    return run_control_plane(database_path, host=host, port=port)
