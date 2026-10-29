from __future__ import annotations

import json
import os
import threading
import signal
import subprocess
import sys
import tempfile
from typing import IO

from pricepulse.runtime.protocol import (
    ExecutionRequest,
    MAX_OUTPUT_BYTES,
    MAX_INPUT_BYTES,
    MAX_SCRIPT_BYTES,
    MAX_ENV_BYTES,
    ProtocolError,
)


MAX_REQUEST_BYTES = 4 * ((MAX_SCRIPT_BYTES + MAX_INPUT_BYTES + 2) // 3) + MAX_ENV_BYTES * 6 + 4096


def _read_stream(stream: IO[bytes], limit: int, process: subprocess.Popen[bytes]) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = os.read(stream.fileno(), 4096)
        if not chunk:
            break
        if total >= limit:
            truncated = True
            os.killpg(process.pid, signal.SIGKILL)
            break
        if total + len(chunk) > limit:
            chunks.append(chunk[: limit - total])
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


def _run_generated_code(request: ExecutionRequest) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="job-", dir="/tmp") as workspace:
        script_path = os.path.join(workspace, "generated.py")
        with open(script_path, "wb") as script_file:
            script_file.write(request.script)
        with open(os.path.join(workspace, "input.json"), "wb") as input_file:
            input_file.write(request.input_data)

        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": workspace,
            "TMPDIR": workspace,
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            **request.env,
        }
        environment["PATH"] = "/usr/bin:/bin"
        environment["HOME"] = workspace
        environment["TMPDIR"] = workspace
        process = subprocess.Popen(
            [sys.executable, "-I", script_path],
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        streams: dict[str, tuple[bytes, bool]] = {}

        def read_stream(name: str, stream: IO[bytes]) -> None:
            streams[name] = _read_stream(stream, request.output_limit_bytes, process)

        stdout_reader = threading.Thread(target=read_stream, args=("stdout", process.stdout))
        stderr_reader = threading.Thread(target=read_stream, args=("stderr", process.stderr))
        stdout_reader.start()
        stderr_reader.start()
        stdout: bytes = b""
        stderr: bytes = b""
        output_truncated = False
        try:
            process.wait(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            timed_out = True
        else:
            timed_out = False
        stdout_reader.join()
        stderr_reader.join()
        stdout, stdout_truncated = streams["stdout"]
        stderr, stderr_truncated = streams["stderr"]
        output_truncated = stdout_truncated or stderr_truncated

        limit = request.output_limit_bytes
        stdout = stdout[:limit]
        stderr = stderr[:limit]
        status = "timeout" if timed_out else "completed"
        result = {
            "status": status,
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "timed_out": timed_out,
            "cleanup_completed": True,
            "error": "output limit exceeded" if output_truncated else None,
        }
        return result


def run_worker() -> None:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        result = {
            "status": "error",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "cleanup_completed": True,
            "error": "request exceeds the input limit",
        }
        sys.stdout.write(json.dumps(result, separators=(",", ":")))
        return
    try:
        request = ExecutionRequest.from_payload(json.loads(raw.decode("utf-8")))
        result = _run_generated_code(request)
    except (ProtocolError, json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as exc:
        result = {
            "status": "error",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "cleanup_completed": True,
            "error": str(exc),
        }
    payload = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    if len(payload.encode("ascii")) > MAX_OUTPUT_BYTES:
        payload = json.dumps(
            {
                "status": "error",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "cleanup_completed": True,
                "error": "worker output limit exceeded",
            },
            separators=(",", ":"),
        )
    sys.stdout.write(payload)


if __name__ == "__main__":
    run_worker()
