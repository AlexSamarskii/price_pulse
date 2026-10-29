from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pricepulse.runtime import ExecutionRequest, RuntimeUnavailableError, SandboxedContainerRunner
from pricepulse.runtime.settings import RuntimeSettings


def test_separate_capture_input_is_bounded_and_reusable(runtime_runner):
    import json
    from pricepulse.runtime.protocol import MAX_INPUT_BYTES, ProtocolError

    code = b"from pathlib import Path\nprint(Path('input.json').read_text())"
    for value in ("first capture", "second capture"):
        request = ExecutionRequest(job_id="capture-input", script=code, env={}, timeout_seconds=10,
                                   input_data=json.dumps({"data": value}).encode())
        assert ExecutionRequest.from_payload(request.to_payload()).input_data == request.input_data
        result = runtime_runner.run(request)
        assert result.status == "completed" and result.exit_code == 0 and result.cleanup_completed
        assert json.loads(result.stdout) == {"data": value}
    with pytest.raises(ProtocolError, match="capture exceeds"):
        ExecutionRequest(job_id="oversize", script=code, env={}, timeout_seconds=10,
                         input_data=b"x" * (MAX_INPUT_BYTES + 1))
    large = ExecutionRequest(job_id="large-capture", env={}, timeout_seconds=10,
        script=b"from pathlib import Path\nprint(len(Path('input.json').read_bytes()))",
        input_data=b"x" * (MAX_INPUT_BYTES - 64))
    result = runtime_runner.run(large)
    assert result.status == "completed" and result.exit_code == 0 and result.cleanup_completed
    assert int(result.stdout.strip()) == MAX_INPUT_BYTES - 64


def execute(
    runner: SandboxedContainerRunner,
    script: str,
    *,
    timeout: float = 1.0,
    output_limit: int = 65_536,
    env: dict[str, str] | None = None,
) -> object:
    request = ExecutionRequest(
        job_id="runtime-test",
        script=script.encode("utf-8"),
        env=env or {},
        timeout_seconds=max(timeout, 10.0) if timeout >= 1.0 else timeout,
        output_limit_bytes=output_limit,
    )
    return runner.run(request)


def test_runtime_unavailable_fails_closed() -> None:
    settings = RuntimeSettings.from_mapping({"docker_binary": "/nonexistent/pricepulse-docker"})
    runner = SandboxedContainerRunner(settings, docker_bin="/nonexistent/pricepulse-docker")
    request = ExecutionRequest(
        job_id="fail-closed",
        script=b"print('never')",
        env={},
        timeout_seconds=0.2,
    )
    with pytest.raises(RuntimeUnavailableError):
        runner.run(request)


def test_worker_is_non_root_with_read_only_root(runtime_runner: SandboxedContainerRunner) -> None:
    result = execute(
        runtime_runner,
        """
import os
print(os.geteuid())
try:
    with open('/etc/pricepulse-escape', 'w') as stream:
        stream.write('escape')
except OSError:
    print('read-only-root-denied')
else:
    print('root-write-allowed')
""",
    )
    assert result.status == "completed"
    assert "65532" in result.stdout
    assert "read-only-root-denied" in result.stdout


def test_control_plane_secret_is_not_inherited(runtime_runner: SandboxedContainerRunner) -> None:
    os.environ["PRICEPULSE_TEST_SECRET"] = "control-plane-canary"
    result = execute(
        runtime_runner,
        """
import os
print(os.environ.get('PRICEPULSE_TEST_SECRET', 'missing'))
print(os.environ.get('PRICEPULSE_JOB_ALLOWED', 'missing'))
""",
        env={"PRICEPULSE_JOB_ALLOWED": "worker-canary"},
    )
    assert result.status == "completed"
    assert "control-plane-canary" not in result.stdout
    assert "missing" in result.stdout
    assert "worker-canary" in result.stdout


def test_control_plane_environment_names_are_rejected() -> None:
    for key in ("OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "PATH", "HOME"):
        with pytest.raises(ValueError, match="outside the job allowlist"):
            ExecutionRequest(
            job_id="env-allowlist",
            script=b"print('never')",
            env={key: "synthetic-secret"},
            timeout_seconds=0.2,
        )


def test_host_security_resources_are_not_mounted(runtime_runner: SandboxedContainerRunner) -> None:
    result = execute(
        runtime_runner,
        """
for path in ('/var/run/docker.sock', '/root/.ssh/id_rsa'):
    try:
        with open(path, 'rb'):
            print(f'{path}:open')
    except OSError:
        print(f'{path}:denied')
""",
    )
    assert result.status == "completed"
    assert "denied" in result.stdout
    assert ":open" not in result.stdout


def test_shared_memory_arguments_are_read_only() -> None:
    runner = SandboxedContainerRunner(RuntimeSettings.from_mapping(), docker_bin="docker")
    request = ExecutionRequest(job_id="shm", script=b"print('x')", env={}, timeout_seconds=0.2)
    arguments = runner._docker_arguments(request, "test-container")
    assert "--tmpfs" in arguments
    assert "/dev/shm:ro,noexec,nosuid,nodev,size=1m" in arguments


def test_shared_memory_is_not_writable(runtime_runner: SandboxedContainerRunner) -> None:
    result = execute(
        runtime_runner,
        "with open('/dev/shm/probe', 'wb') as stream: stream.write(b'x')",
        timeout=5.0,
    )
    assert result.status == "completed"
    assert result.exit_code == 1
    assert "Read-only file system" in result.stderr
    assert result.cleanup_completed


def test_compose_declares_shared_memory_read_only() -> None:
    compose = Path(__file__).resolve().parents[2] / "deploy" / "runtime-compose.yaml"
    assert "/dev/shm:ro,noexec,nosuid,nodev,size=1m" in compose.read_text()


def test_network_is_disabled(runtime_runner: SandboxedContainerRunner) -> None:
    result = execute(
        runtime_runner,
        """
import socket
try:
    socket.create_connection(('169.254.169.254', 80), timeout=0.5)
except OSError:
    print('network-denied')
else:
    print('network-open')
""",
        timeout=5.0,
    )
    assert result.status == "completed"
    assert "network-denied" in result.stdout


def test_output_flood_is_limited(runtime_runner: SandboxedContainerRunner) -> None:
    result = execute(
        runtime_runner,
        "import sys; sys.stdout.write('x' * 100000); sys.stdout.flush()",
        output_limit=1024,
    )
    assert result.error == "output limit exceeded"
    assert len(result.stdout) <= 1024


def test_timeout_process_tree_is_destroyed(runtime_runner: SandboxedContainerRunner) -> None:
    result = execute(
        runtime_runner,
        """
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
""",
        timeout=0.5,
    )
    assert result.status == "timeout"
    assert result.cleanup_completed
    probe = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=pricepulse-sandbox-", "--format", "{{.ID}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert probe.returncode == 0
    assert probe.stdout.strip() == b""
