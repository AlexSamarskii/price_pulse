from __future__ import annotations

import argparse
import os
from pathlib import Path

from pricepulse.api.routes import run_control_plane


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the PricePulse local control plane")
    parser.add_argument("--database", default=os.environ.get("PRICEPULSE_DATABASE", "pricepulse.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    path = Path(arguments.database)
    if path.exists() and not path.is_file():
        raise SystemExit("database path must be a file")
    server = run_control_plane(str(path), host=arguments.host, port=arguments.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
