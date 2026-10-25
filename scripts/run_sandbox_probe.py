#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pricepulse.runtime import ExecutionRequest, RuntimeUnavailableError, SandboxedContainerRunner


ENV_ATTACK = """
import os
print(os.environ.get('PRICEPULSE_PROBE_SECRET', 'missing'))
print(os.environ.get('PRICEPULSE_JOB_ALLOWED', 'missing'))
"""

FILE_ATTACK = """
for path in ('/var/run/docker.sock', '/root/.ssh/id_rsa', '/etc/pricepulse-escape'):
    try:
        with open(path, 'rb' if path != '/etc/pricepulse-escape' else 'w'):
            print(f'{path}:open')
    except OSError:
        print(f'{path}:denied')
"""

FORK_ATTACK = """
import os
import time
child = os.fork()
if child == 0:
    os.setsid()
    print('child-started', flush=True)
    time.sleep(2)
else:
    print('parent-started', flush=True)
    time.sleep(2)
"""

OUTPUT_ATTACK = "import sys; sys.stdout.write('x' * 100000); sys.stdout.flush()"

NETWORK_ATTACK = """
import socket
try:
    socket.create_connection(('169.254.169.254', 80), timeout=0.5)
except OSError:
    print('network-denied')
else:
    print('network-open')
"""

ATTACK_SCRIPTS = {
    "environment": ENV_ATTACK,
    "filesystem": FILE_ATTACK,
    "fork": FORK_ATTACK,
    "output": OUTPUT_ATTACK,
    "network": NETWORK_ATTACK,
}

def run_attack(runner: SandboxedContainerRunner, name: str, script: str) -> dict[str, Any]:
    os.environ["PRICEPULSE_PROBE_SECRET"] = "synthetic-control-plane-canary"
    request = ExecutionRequest(
        job_id=f"sandbox-probe-{name}",
        script=script.encode("utf-8"),
        env={"PRICEPULSE_JOB_ALLOWED": "synthetic-worker-canary"},
        timeout_seconds=0.5 if name == "fork" else 15.0,
        output_limit_bytes=1024,
    )
    result = runner.run(request)
    return {
        "attack": name,
        "status": result.status,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "cleanup_completed": result.cleanup_completed,
        "error": result.error,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def validate(results: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    expected_by_attack = {
        "environment": ("missing", "synthetic-worker-canary"),
        "filesystem": ("denied",),
        "fork": ("timeout",),
        "output": ("output limit exceeded",),
        "network": ("network-denied",),
    }
    for result in results:
        attack = result["attack"]
        if attack == "fork":
            if result["status"] != "timeout" or not result["cleanup_completed"]:
                failures.append(f"{attack}: process tree cleanup failed")
            continue
        if attack == "output" and result["status"] == "timeout" and result["error"] == "output limit exceeded":
            continue
        if result["status"] != "completed":
            failures.append(f"{attack}: expected completion, got {result['status']}")
            continue
        for expected in expected_by_attack[attack]:
            if expected not in result["stdout"]:
                failures.append(f"{attack}: expected marker {expected!r} was absent")
        if "synthetic-control-plane-canary" in result["stdout"]:
            failures.append(f"{attack}: control-plane canary leaked")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline hardened-runtime canaries")
    parser.add_argument(
        "--attacks",
        nargs="+",
        choices=tuple(ATTACK_SCRIPTS),
        default=tuple(ATTACK_SCRIPTS),
        help="subset of synthetic attacks to run",
    )
    arguments = parser.parse_args()
    runner = SandboxedContainerRunner()
    if not runner.runtime_available():
        print(
            json.dumps({"status": "pending_environment", "reason": "hardened container runtime is unavailable"}),
        )
        return 2

    try:
        results = [run_attack(runner, name, ATTACK_SCRIPTS[name]) for name in arguments.attacks]
    except RuntimeUnavailableError as exc:
        print(json.dumps({"status": "pending_environment", "reason": str(exc)}))
        return 2
    failures = validate(results)
    report = {"status": "pass" if not failures else "fail", "failures": failures, "results": results}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
