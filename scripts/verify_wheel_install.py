#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from collections.abc import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and verify the PricePulse wheel outside the checkout")
    parser.add_argument("--python", default=sys.executable)
    arguments = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="pricepulse-wheel-") as temporary:
        workspace = Path(temporary).resolve()
        output_dir = workspace / "dist"
        venv_dir = workspace / "venv"
        python_path = venv_dir / "bin" / "python"
        environment = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "USER", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "UV_CACHE_DIR")
            if key in os.environ
        }
        environment.update(UV_OFFLINE="1", PYTHONNOUSERSITE="1", PYTHON_DOTENV_DISABLED="1")

        def run(command: Sequence[str]) -> None:
            process = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=90,
            )
            if process.returncode != 0:
                raise RuntimeError(f"command failed: {' '.join(command)}\n{process.stdout}\n{process.stderr}")

        run(["uv", "--no-config", "venv", "--python", str(Path(arguments.python).absolute()), str(venv_dir)])
        run(["uv", "--no-config", "pip", "install", "--python", str(python_path), "--requirement", str(PROJECT_ROOT / "pricepulse/runtime/requirements.lock")])
        run(["uv", "--no-config", "build", "--wheel", "--out-dir", str(output_dir), str(PROJECT_ROOT)])
        wheels = sorted(output_dir.glob("pricepulse-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one PricePulse wheel, found {len(wheels)}")
        run(["uv", "--no-config", "pip", "install", "--python", str(python_path), "--no-deps", str(wheels[0])])

        verification = workspace / "verify_installed.py"
        verification.write_text(
            "\n".join(
                [
                    "import importlib.metadata",
                    "import pathlib",
                    "import sys",
                    "import pricepulse",
                    "import pricepulse.__main__",
                    "from pricepulse.data.store import Database",
                    "installed = pathlib.Path(sys.prefix).resolve()",
                    "assert pathlib.Path(pricepulse.__file__).resolve().is_relative_to(installed)",
                    "assert pathlib.Path(pricepulse.__main__.__file__).resolve().is_relative_to(installed)",
                    "assert importlib.metadata.version('pricepulse') == pricepulse.__version__",
                    "entry_points = importlib.metadata.entry_points(group='console_scripts')",
                    "assert any(entry.name == 'pricepulse' for entry in entry_points)",
                    "web = pathlib.Path(pricepulse.__file__).parent / 'web'",
                    "assert (web / 'templates' / 'dashboard.html').is_file()",
                    "assert (web / 'static' / 'dashboard.css').is_file()",
                    "assert (web / 'static' / 'dashboard.js').is_file()",
                    "with Database('installed.db') as database:",
                    "    database.initialize()",
                    "    assert database.connection.execute(\"SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'\").fetchone()",
                    "print('product-wheel-pass')",
                ]
            ),
            encoding="utf-8",
        )
        run([str(python_path), "-I", str(verification)])
        run([str(venv_dir / "bin" / "pricepulse"), "--help"])

    print('{"status":"pass","wheel":"pricepulse"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
