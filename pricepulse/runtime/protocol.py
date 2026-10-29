from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


JOB_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
ALLOWED_ENV_KEY_RE = re.compile(r"^PRICEPULSE_JOB_[A-Z0-9_]{1,100}$")
MAX_SCRIPT_BYTES = 1_000_000
MAX_INPUT_BYTES = 2_500_000
MAX_ENV_BYTES = 32_768
MAX_TIMEOUT_SECONDS = 120.0
MAX_OUTPUT_BYTES = 4_000_000


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class ExecutionRequest:
    job_id: str
    script: bytes
    env: Mapping[str, str]
    timeout_seconds: float
    cpu_limit: float = 0.5
    memory_limit_mb: int = 128
    pids_limit: int = 32
    output_limit_bytes: int = 65_536
    workspace_limit_bytes: int = 4_000_000
    input_data: bytes = b""

    def __post_init__(self) -> None:
        if not JOB_ID_RE.fullmatch(self.job_id):
            raise ProtocolError("job_id has an invalid format")
        if not self.script:
            raise ProtocolError("script must not be empty")
        if len(self.script) > MAX_SCRIPT_BYTES:
            raise ProtocolError("script exceeds the input limit")
        if not isinstance(self.input_data, bytes) or len(self.input_data) > MAX_INPUT_BYTES:
            raise ProtocolError("capture exceeds the input limit")
        if not 0.1 <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ProtocolError("timeout_seconds is out of range")
        if not 0.05 <= self.cpu_limit <= 4:
            raise ProtocolError("cpu_limit is out of range")
        if not 32 <= self.memory_limit_mb <= 2048:
            raise ProtocolError("memory_limit_mb is out of range")
        if not 1 <= self.pids_limit <= 256:
            raise ProtocolError("pids_limit is out of range")
        if not 1 <= self.output_limit_bytes <= MAX_OUTPUT_BYTES:
            raise ProtocolError("output_limit_bytes is out of range")
        if not 1 <= self.workspace_limit_bytes <= 16_000_000:
            raise ProtocolError("workspace_limit_bytes is out of range")
        encoded_env_size = 0
        for key, value in self.env.items():
            if not ENV_KEY_RE.fullmatch(key):
                raise ProtocolError(f"environment key {key!r} is invalid")
            if not ALLOWED_ENV_KEY_RE.fullmatch(key):
                raise ProtocolError(f"environment key {key!r} is outside the job allowlist")
            encoded_value = value.encode("utf-8", errors="surrogatepass")
            encoded_env_size += len(key.encode("ascii")) + len(encoded_value)
            if len(encoded_value) > 8192:
                raise ProtocolError(f"environment value for {key!r} is too large")
        if encoded_env_size > MAX_ENV_BYTES:
            raise ProtocolError("environment exceeds the total allowlist limit")

    def to_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "script_b64": base64.b64encode(self.script).decode("ascii"),
            "env": dict(self.env),
            "timeout_seconds": self.timeout_seconds,
            "output_limit_bytes": self.output_limit_bytes,
            "input_b64": base64.b64encode(self.input_data).decode("ascii"),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ExecutionRequest":
        if not isinstance(payload, Mapping):
            raise ProtocolError("payload must be an object")
        try:
            script = base64.b64decode(payload["script_b64"], validate=True)
            input_data = base64.b64decode(payload.get("input_b64", ""), validate=True)
            env = payload["env"]
        except (KeyError, TypeError, binascii.Error) as exc:
            raise ProtocolError("payload is malformed") from exc
        try:
            timeout = float(payload["timeout_seconds"])
            output_limit = int(payload["output_limit_bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError("payload limits are malformed") from exc
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ProtocolError("environment must be a string mapping")
        return cls(
            job_id=str(payload.get("job_id", "worker")),
            script=script,
            env=env,
            timeout_seconds=timeout,
            output_limit_bytes=output_limit,
            input_data=input_data,
        )


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    cleanup_completed: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: bytes) -> "ExecutionResult":
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("worker returned malformed JSON") from exc
        required = {
            "status",
            "exit_code",
            "stdout",
            "stderr",
            "timed_out",
            "cleanup_completed",
            "error",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ProtocolError("worker result has an invalid schema")
        if value["status"] not in {"completed", "timeout", "error"}:
            raise ProtocolError("worker result has an invalid status")
        if not isinstance(value["stdout"], str) or not isinstance(value["stderr"], str):
            raise ProtocolError("worker output must be text")
        if not isinstance(value["timed_out"], bool) or not isinstance(value["cleanup_completed"], bool):
            raise ProtocolError("worker result flags must be boolean")
        if value["exit_code"] is not None and not isinstance(value["exit_code"], int):
            raise ProtocolError("worker exit_code is invalid")
        if value["error"] is not None and not isinstance(value["error"], str):
            raise ProtocolError("worker error is invalid")
        return cls(**value)
