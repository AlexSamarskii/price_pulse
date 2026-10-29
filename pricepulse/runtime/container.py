from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence

from pricepulse.runtime.protocol import ExecutionRequest, ExecutionResult
from pricepulse.runtime.settings import RuntimeSettings


class RuntimeUnavailableError(RuntimeError):
    pass


class SandboxedContainerRunner:
    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        docker_environment: Mapping[str, str] | None = None,
        docker_bin: str | None = None,
    ) -> None:
        self.settings = settings or RuntimeSettings.from_mapping()
        self._docker_bin = docker_bin or self.settings.docker_binary
        self._docker_env = dict(docker_environment or {"PATH": self.settings.command_path})

    def _docker(self, arguments: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [self._docker_bin, *arguments],
            env=self._docker_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )

    def runtime_available(self, timeout: float = 5.0) -> bool:
        try:
            result = self._docker(
                ["info", "--format", "{{json .ServerVersion}}"],
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())

    def _require_runtime(self) -> None:
        if not self.runtime_available():
            raise RuntimeUnavailableError("hardened container runtime is unavailable")

    def _docker_arguments(self, request: ExecutionRequest, container_name: str) -> list[str]:
        arguments = [
            "run",
            "--rm",
            "--name",
            container_name,
            "--interactive",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network=none",
            "--log-driver=none",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={request.workspace_limit_bytes}",
            "--tmpfs",
            "/dev/shm:ro,noexec,nosuid,nodev,size=1m",
            "--memory",
            f"{request.memory_limit_mb}m",
            "--cpus",
            f"{request.cpu_limit:.2f}",
            "--pids-limit",
            str(request.pids_limit),
            "--ulimit",
            "nofile=32:32",
            "--ulimit",
            f"fsize={max(1048576, len(request.input_data))}",
        ]
        for key, value in request.env.items():
            arguments.extend(["--env", f"{key}={value}"])
        arguments.append(self.settings.runtime_image)
        return arguments

    def _stop_container(self, container_name: str) -> None:
        self._docker(["kill", "--signal", "KILL", container_name], timeout=5)
        self._docker(["rm", "--force", container_name], timeout=5)

    def _container_gone(self, container_name: str) -> bool:
        result = self._docker(["inspect", "-f", "{{.State.Running}}", container_name], timeout=5)
        return result.returncode != 0

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        self._require_runtime()
        container_name = f"{self.settings.name_prefix}-{uuid.uuid4().hex}"
        arguments = self._docker_arguments(request, container_name)
        payload = json.dumps(request.to_payload(), separators=(",", ":")).encode("utf-8")
        process = subprocess.Popen(
            [self._docker_bin, *arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._docker_env,
            start_new_session=True,
        )
        host_timeout = max(45.0, request.timeout_seconds + 10.0)
        timed_out = False
        try:
            stdout, stderr = process.communicate(payload, timeout=host_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if self._container_gone(container_name):
                    break
                self._stop_container(container_name)
                time.sleep(0.1)

        cleanup_completed = self._container_gone(container_name)
        if timed_out:
            return ExecutionResult(
                status="timeout",
                exit_code=None,
                stdout="",
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=True,
                cleanup_completed=cleanup_completed,
                error=None,
            )
        if process.returncode != 0:
            return ExecutionResult(
                status="error",
                exit_code=None,
                stdout="",
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=False,
                cleanup_completed=cleanup_completed,
                error=f"container command exited with code {process.returncode}",
            )
        try:
            result = ExecutionResult.from_json(stdout)
        except ValueError as exc:
            return ExecutionResult(
                status="error",
                exit_code=None,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=False,
                cleanup_completed=cleanup_completed,
                error=str(exc),
            )
        return result
