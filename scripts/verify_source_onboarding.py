#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pricepulse.collectors.source_onboarding import (
    SourcePolicyError,
    onboard_source,
    validate_fixture_path,
    validate_target_url,
)
from scripts.verify_upstream_provenance import verify as verify_upstream


REQUIRED_GUIDE_SECTIONS = (
    "## Operator approval",
    "## External prerequisites and reproduction",
    "## Pending environment",
    "## Blocked and challenge states",
    "## Cooldown and retry limits",
    "## Explicit limitations",
)


def _assert_upstream_provenance(manifest_path: Path) -> None:
    verify_upstream(
        manifest_path,
        manifest_path.with_name("source_snapshot.tar.gz"),
        manifest_path.with_name("integration_manifest.json"),
    )


def _write_fixture(workdir: Path) -> Path:
    fixture_path = workdir / "onboarding-fixture.html"
    fixture_path.write_text(
        "<!doctype html><html><body><main>PricePulse local onboarding fixture</main></body></html>",
        encoding="utf-8",
    )
    return validate_fixture_path(fixture_path)


def _onboard_local_fixture(fixture_path: Path) -> dict[str, Any]:
    policy = onboard_source(
        {
            "source_id": "onboarding_local_fixture",
            "base_url": "https://fixtures.example/catalog",
            "allowed_hosts": ["fixtures.example"],
            "allowed_path_prefixes": ["/catalog"],
            "robots_policy": "operator_authorized_fixture",
            "max_pages": 1,
            "requests_per_minute": 1,
            "max_concurrency": 1,
            "timeout_seconds": 1,
            "allowed_currencies": ["RUB"],
            "live_enabled": False,
        },
        "Extract title, price, currency, and availability from the local fixture.",
    )
    if validate_target_url(policy.policy["base_url"], policy.policy) != policy.policy["base_url"]:
        raise AssertionError("local fixture target URL failed policy round-trip")

    sibling_path = "/catalog-admin"
    if policy.policy["allowed_path_prefixes"] != ["/catalog"]:
        raise AssertionError("unexpected fixture policy path prefix")
    try:
        validate_target_url(
            f"https://{policy.policy['allowed_hosts'][0]}{sibling_path}",
            policy.policy,
        )
    except SourcePolicyError:
        pass
    else:
        raise AssertionError("same-host sibling path was not rejected")

    return {
        "source_id": policy.source_id,
        "goal": policy.goal,
        "policy_digest": policy.policy_digest,
        "fixture_name": fixture_path.name,
        "fixture_digest": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
    }


def verify_guide(guide_path: Path, manifest_path: Path) -> dict[str, Any]:
    guide = guide_path.read_text(encoding="utf-8")
    missing_sections = [
        section for section in REQUIRED_GUIDE_SECTIONS if section not in guide
    ]
    if missing_sections:
        raise AssertionError(f"guide is missing required sections: {missing_sections}")

    required_phrases = (
        "requires an operator approval",
        "exact command that cannot run",
        "expected observable result",
        "Do not bypass the challenge",
        "enters a cooldown",
        "does not provide universal store support",
        "does not execute",
        "generated code on the control plane",
    )
    missing_phrases = [phrase for phrase in required_phrases if phrase not in guide]
    if missing_phrases:
        raise AssertionError(f"guide is missing required statements: {missing_phrases}")

    _assert_upstream_provenance(manifest_path)
    with tempfile.TemporaryDirectory(prefix="pricepulse-onboarding-") as temp_dir:
        fixture_path = _write_fixture(Path(temp_dir))
        return _onboard_local_fixture(fixture_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guide", type=Path, default=Path("pricepulse/collectors/source_onboarding.md"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("docs/upstream/source_manifest.json"),
    )
    arguments = parser.parse_args()

    evidence = verify_guide(arguments.guide, arguments.manifest)
    print(json.dumps({"status": "pass", "evidence": evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
