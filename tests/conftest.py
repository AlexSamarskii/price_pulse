from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_IMAGE = "pricepulse-runtime:local"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pricepulse.runtime import SandboxedContainerRunner
from pricepulse.runtime.settings import RuntimeSettings


@pytest.fixture(scope="session")
def runtime_runner() -> SandboxedContainerRunner:
    settings = RuntimeSettings.from_mapping()
    runner = SandboxedContainerRunner(settings)
    if not runner.runtime_available():
        pytest.skip("hardened container runtime is unavailable")
    image_probe = subprocess.run(
        ["docker", "image", "inspect", RUNTIME_IMAGE],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if image_probe.returncode != 0:
        build = subprocess.run(
            ["docker", "build", "--quiet", "-f", "Dockerfile.runtime", "-t", RUNTIME_IMAGE, "."],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert build.returncode == 0, build.stderr.decode("utf-8", errors="replace")
    return runner


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return PROJECT_ROOT / "tests" / "fixtures"
