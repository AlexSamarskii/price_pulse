from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run(command: list[str]) -> None:
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_product_wheel_outside_checkout() -> None:
    _run([sys.executable, "scripts/verify_wheel_install.py"])


def test_upstream_provenance() -> None:
    _run([sys.executable, "scripts/verify_upstream_provenance.py"])


def test_product_only_package() -> None:
    manifest = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "pricepulse"' in manifest
    assert 'name = "scrapeclaw"' not in manifest
    assert 'include = ["pricepulse*", "migrations*"]' in manifest
    assert not (PROJECT_ROOT / "pricepulse/runtime/pyproject.toml").exists()
    assert not (PROJECT_ROOT / "pricepulse/runtime/requirements.txt").exists() or "python-dotenv" not in (
        PROJECT_ROOT / "pricepulse/runtime/requirements.txt"
    ).read_text(encoding="utf-8")
