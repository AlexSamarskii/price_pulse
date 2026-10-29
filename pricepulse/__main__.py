from __future__ import annotations

import argparse
import os
import signal
import threading
from pathlib import Path

from pricepulse.api.routes import run_control_plane


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the PricePulse local control plane")
    parser.add_argument("--database", default=os.environ.get("PRICEPULSE_DATABASE", "pricepulse.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", help="explicit JSON catalog collection configuration")
    parser.add_argument("--once", action="store_true", help="process due collection jobs once and exit")
    arguments = parser.parse_args()
    path = Path(arguments.database)
    if path.exists() and not path.is_file():
        raise SystemExit("database path must be a file")
    worker = None
    if arguments.config:
        from pricepulse.jobs.collection import load_configs
        from pricepulse.jobs.worker import CollectionWorker

        worker = CollectionWorker(str(path), load_configs(arguments.config))
    if arguments.once:
        if worker is None:
            parser.error("--once requires --config")
        results = worker.tick()
        return 1 if any(r["status"] != "succeeded" for r in results) else 0
    server = run_control_plane(str(path), host=arguments.host, port=arguments.port)
    thread = None
    if worker is not None:
        thread = threading.Thread(target=worker.run_forever, name="catalog-worker", daemon=True)
        thread.start()

    def stop_server(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_server)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if worker is not None:
            worker.stop.set()
        server.server_close()
        if thread is not None:
            thread.join(timeout=180)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
