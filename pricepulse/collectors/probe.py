from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from pricepulse.collectors.capture_sanitizer import sanitize_capture
from pricepulse.collectors.prompt_guard import guard_capture_for_prompt
from pricepulse.collectors.source_onboarding import SourceOnboarding, validate_target_url
from pricepulse.collectors.challenge_detector import CaptchaDetector
from pricepulse.runtime import ExecutionRequest, ExecutionResult, RuntimeUnavailableError
from pricepulse.runtime.protocol import MAX_SCRIPT_BYTES


MAX_CAPTURE_BYTES = 500_000
RUNNER_ENV_KEY = "PRICEPULSE_JOB_POLICY_DIGEST"
PROBE_STATUSES = {"completed", "challenge", "blocked", "error", "inert_injection"}


class ProbeError(ValueError):
    pass


class EgressClient(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        raise NotImplementedError


@dataclass(frozen=True)
class ProbeAnalysis:
    status: str
    prompt: str
    injection_detected: bool
    capture_digest: str
    execution: ExecutionResult
    challenge: dict[str, Any] | None = None
    reason: str | None = None


def _http_error_result(response_status: int) -> ProbeAnalysis:
    is_access_limited = response_status in {401, 403, 429}
    status = "blocked" if is_access_limited else "error"
    reason = (
        f"source is blocked by HTTP {response_status}"
        if is_access_limited
        else f"probe failed due to HTTP {response_status}"
    )
    return ProbeAnalysis(
        status=status,
        prompt="",
        injection_detected=False,
        capture_digest="",
        execution=ExecutionResult(
            status=status,
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            cleanup_completed=True,
            error=reason,
        ),
        reason=reason,
    )


def _empty_capture_result() -> ProbeAnalysis:
    reason = "probe failed due to empty capture"
    return ProbeAnalysis(
        status="error",
        prompt="",
        injection_detected=False,
        capture_digest="",
        execution=ExecutionResult(
            status="error",
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            cleanup_completed=True,
            error=reason,
        ),
        reason=reason,
    )


PROBE_SCRIPT_TEMPLATE = '''import base64
import json

payload = json.loads(base64.b64decode("__PROBE_PAYLOAD_B64__").decode("utf-8"))
status_code = int(payload["status_code"])
if status_code in (401, 403, 429):
    probe_status = "blocked"
elif 400 <= status_code <= 599:
    probe_status = "error"
elif payload["challenge"]:
    probe_status = "challenge"
elif payload["injection_detected"]:
    probe_status = "inert_injection"
else:
    probe_status = "completed"

result = {
    "status": probe_status,
    "capture_digest": payload["capture_digest"],
    "redaction_count": payload["redaction_count"],
    "challenge": payload["challenge"],
    "reason": (
        "source is blocked by policy"
        if probe_status == "blocked"
        else f"probe failed due to HTTP {status_code}"
        if probe_status == "error"
        else None
    ),
}
print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
'''


def _response_capture(response: Any) -> tuple[str, Mapping[str, str], Mapping[str, str], int]:
    body = response.read()
    if isinstance(body, str):
        content = body
    elif isinstance(body, bytes):
        content = body.decode("utf-8", errors="replace")
    else:
        raise ProbeError("egress response body is not text")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_CAPTURE_BYTES:
        raise ProbeError("capture exceeds the probe input limit")
    raw_headers = response.headers
    if hasattr(raw_headers, "multi_items"):
        items = raw_headers.multi_items()
    else:
        items = raw_headers.items()
    headers = {key: value for key, value in items}
    cookies = {"response": response.headers.get("set-cookie", "")} if "set-cookie" in response.headers else {}
    status_code = response.status_code
    if not 100 <= int(status_code) <= 599:
        raise ProbeError("egress response status is invalid")
    return content, headers, cookies, int(status_code)


def _probe_payload(
    capture: Any,
    *,
    policy_digest: str,
    status_code: int,
    challenge: dict[str, Any] | None,
    injection_detected: bool,
) -> dict[str, Any]:
    return {
        "policy_digest": policy_digest,
        "status_code": status_code,
        "capture_digest": capture.content_digest,
        "redaction_count": capture.redaction_count,
        "headers": capture.headers,
        "query_parameters": capture.query_parameters,
        "challenge": challenge,
        "injection_detected": injection_detected,
    }


def _build_request(
    payload: Mapping[str, Any],
    *,
    job_id: str,
    timeout_seconds: float,
) -> ExecutionRequest:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    script = PROBE_SCRIPT_TEMPLATE.replace("__PROBE_PAYLOAD_B64__", base64.b64encode(encoded).decode("ascii"))
    if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise ProbeError("probe analysis exceeds the worker input limit")
    return ExecutionRequest(
        job_id=job_id,
        script=script.encode("utf-8"),
        env={RUNNER_ENV_KEY: str(payload["policy_digest"])},
        timeout_seconds=timeout_seconds,
    )


def _unavailable_result(exc: RuntimeUnavailableError) -> ProbeAnalysis:
    execution = ExecutionResult(
        status="blocked_by_environment",
        exit_code=None,
        stdout="",
        stderr="",
        timed_out=False,
        cleanup_completed=True,
        error=str(exc),
    )
    return ProbeAnalysis(
        status="blocked_by_environment",
        prompt="",
        injection_detected=False,
        capture_digest="",
        execution=execution,
        reason=str(exc),
    )


def _validate_execution(
    execution: ExecutionResult,
    *,
    expected_digest: str,
) -> dict[str, Any]:
    if execution.status != "completed":
        raise ProbeError(execution.error or execution.stderr or "probe worker did not complete")
    if execution.exit_code != 0:
        raise ProbeError("probe worker exited with an error")
    try:
        value = json.loads(execution.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError("probe worker returned invalid JSON") from exc
    required = {"status", "capture_digest", "redaction_count", "challenge", "reason"}
    if not isinstance(value, dict) or set(value) != required:
        raise ProbeError("probe worker returned an invalid schema")
    if value["status"] not in PROBE_STATUSES:
        raise ProbeError("probe worker returned an unknown status")
    if value["capture_digest"] != expected_digest:
        raise ProbeError("probe worker capture digest does not match sanitized input")
    return value


def run_probe_analysis(
    onboarding: SourceOnboarding,
    *,
    runner: Any,
    job_id: str,
    content: str | None = None,
    broker: EgressClient | None = None,
    target_url: str | None = None,
    status_code: int = 200,
    headers: Mapping[str, Any] | None = None,
    cookies: Mapping[str, Any] | None = None,
    query_parameters: Mapping[str, Any] | None = None,
    timeout_seconds: float = 10.0,
) -> ProbeAnalysis:
    if content is None:
        if broker is None or target_url is None:
            raise ProbeError("probe requires either a local capture or an approved egress broker")
        if not onboarding.policy["live_enabled"]:
            raise ProbeError("live probing requires live_enabled=true")
        target_url = validate_target_url(target_url, onboarding.policy)
        response = broker.request("GET", target_url)
        content, response_headers, response_cookies, response_status = _response_capture(response)
        status_code = response_status
        headers = response_headers if headers is None else headers
        cookies = response_cookies if cookies is None else cookies
        if 400 <= response_status <= 599:
            return _http_error_result(response_status)
    elif broker is not None or target_url is not None:
        raise ProbeError("local capture must not be mixed with egress input")
    else:
        target_url = onboarding.policy["base_url"]

    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CAPTURE_BYTES:
        raise ProbeError("capture content is missing or exceeds the probe input limit")
    if not 100 <= int(status_code) <= 599:
        raise ProbeError("capture status code is invalid")
    if content == "":
        return _empty_capture_result()

    target_url = validate_target_url(target_url, onboarding.policy)
    capture = sanitize_capture(
        headers=headers,
        cookies=cookies,
        query_parameters=query_parameters,
        content=content,
    )
    challenge_value = CaptchaDetector().detect_html(capture.content)
    challenge = challenge_value.to_dict() if challenge_value is not None else None
    guarded = guard_capture_for_prompt(capture, onboarding.policy_digest)
    payload = _probe_payload(
        capture,
        policy_digest=onboarding.policy_digest,
        status_code=int(status_code),
        challenge=challenge,
        injection_detected=guarded.injection_detected,
    )
    request = _build_request(payload, job_id=job_id, timeout_seconds=timeout_seconds)
    try:
        execution = runner.run(request)
    except RuntimeUnavailableError as exc:
        return _unavailable_result(exc)
    if not isinstance(execution, ExecutionResult):
        raise ProbeError("runtime returned an invalid execution result")
    value = _validate_execution(execution, expected_digest=capture.content_digest)
    return ProbeAnalysis(
        status=str(value["status"]),
        prompt=guarded.prompt,
        injection_detected=guarded.injection_detected,
        capture_digest=capture.content_digest,
        execution=execution,
        challenge=value["challenge"],
        reason=value["reason"],
    )
