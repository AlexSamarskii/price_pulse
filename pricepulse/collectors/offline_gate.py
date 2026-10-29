from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from pricepulse.runtime import ExecutionRequest, ExecutionResult, RuntimeUnavailableError
from pricepulse.collectors.ast_checker import check_script_safety


class OfflineGateError(ValueError):
    pass


@dataclass(frozen=True)
class OfflineGateResult:
    status: str
    products: tuple[dict[str, Any], ...]
    reason: str | None
    execution: ExecutionResult


def validate_product(value: Mapping[str, Any]) -> bool:
    if not isinstance(value.get("source"), str) or not value["source"]:
        return False
    if not (isinstance(value.get("product_id"), str) and value["product_id"]) and not isinstance(value.get("canonical_url"), str):
        return False
    for field in ("sku", "title", "price", "currency", "availability", "observed_at"):
        if not isinstance(value.get(field), str) or not value[field]:
            return False
    if not isinstance(value.get("attributes"), dict):
        return False
    evidence = value.get("evidence")
    return isinstance(evidence, dict)


def _verify_version_identity(version: Mapping[str, Any]) -> None:
    code = version.get("code")
    policy = version.get("policy")
    if not isinstance(code, str) or not code or not isinstance(policy, dict):
        raise OfflineGateError("scraper version identity is incomplete")

    required = {
        "candidate_id",
        "version_id",
        "source_id",
        "code_sha256",
        "dependencies_sha256",
        "policy_digest",
        "revision",
    }
    if any(not version.get(field) for field in required):
        raise OfflineGateError("scraper version identity is incomplete")

    policy_digest = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    code_digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    dependencies = {
        "engine": version.get("engine"),
        "extraction_goal": version.get("goal"),
        "provider_id": version.get("provider_id"),
        "model": version.get("model"),
        "source_policy_digest": version.get("policy_digest"),
    }
    dependencies_digest = hashlib.sha256(
        json.dumps(dependencies, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    expected_version_id = hashlib.sha256(
        f"{version.get('candidate_id')}:{code_digest}:{dependencies_digest}:{version.get('revision')}".encode("utf-8")
    ).hexdigest()[:32]

    if version.get("code_sha256") != code_digest:
        raise OfflineGateError("scraper code digest does not match its code")
    if version.get("policy_digest") != policy_digest:
        raise OfflineGateError("scraper policy digest does not match its policy")
    if version.get("dependencies_sha256") != dependencies_digest:
        raise OfflineGateError("scraper dependency digest does not match its dependencies")
    if version.get("version_id") != expected_version_id:
        raise OfflineGateError("scraper version id does not match its identities")


def run_offline_gate(
    version: Mapping[str, Any],
    runner: Any,
    *,
    job_id: str,
    timeout_seconds: float = 10.0,
) -> OfflineGateResult:
    _verify_version_identity(version)
    code = version["code"]
    safe, reason = check_script_safety(code)
    if not safe:
        return OfflineGateResult(
            status="failed",
            products=(),
            reason=reason,
            execution=ExecutionResult(
                status="error",
                exit_code=None,
                stdout="",
                stderr=reason,
                timed_out=False,
                cleanup_completed=True,
                error=reason,
            ),
        )
    request = ExecutionRequest(
        job_id=job_id,
        script=code.encode("utf-8"),
        env={"PRICEPULSE_JOB_POLICY_DIGEST": str(version.get("policy_digest", ""))},
        timeout_seconds=timeout_seconds,
    )
    try:
        execution = runner.run(request)
    except RuntimeUnavailableError as exc:
        execution = ExecutionResult(
            status="blocked_by_environment",
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            cleanup_completed=True,
            error=str(exc),
        )
        return OfflineGateResult(
            status="blocked_by_environment",
            products=(),
            reason=str(exc),
            execution=execution,
        )
    if execution.status != "completed":
        return OfflineGateResult(
            status="failed",
            products=(),
            reason=execution.error or execution.stderr,
            execution=execution,
        )
    try:
        parsed = json.loads(execution.stdout)
    except json.JSONDecodeError as exc:
        return OfflineGateResult(
            status="failed",
            products=(),
            reason="candidate did not return valid JSON",
            execution=execution,
        )
    if not isinstance(parsed, list) or not parsed or not all(
        isinstance(item, dict) and validate_product(item) for item in parsed
    ):
        return OfflineGateResult(
            status="failed",
            products=(),
            reason="candidate returned empty, invalid, or incomplete extraction data",
            execution=execution,
        )
    return OfflineGateResult(
        status="passed",
        products=tuple(parsed),
        reason=None,
        execution=execution,
    )
