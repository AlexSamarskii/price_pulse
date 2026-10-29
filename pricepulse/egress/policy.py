from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
import posixpath
from urllib.parse import unquote


MAX_RESPONSE_BYTES = 2_000_000
MAX_PATH_DECODINGS = 16
DISALLOWED_PATH_CHARS = {";", "\\", "\x00"}


def effective_path(path: str) -> str:
    current = path or "/"
    for _ in range(MAX_PATH_DECODINGS):
        if any(char in current for char in DISALLOWED_PATH_CHARS):
            raise ValueError("path contains an ambiguous segment")
        decoded = unquote(current, encoding="utf-8", errors="strict")
        if decoded == current:
            break
        current = decoded
    else:
        raise ValueError("path is encoded too deeply")
    if "%" in current:
        raise ValueError("path contains an invalid escape")
    canonical = posixpath.normpath(current)
    if canonical == ".":
        canonical = "/"
    if current.endswith("/") and not canonical.endswith("/"):
        canonical += "/"
    return canonical


@dataclass(frozen=True)
class EgressPolicy:
    allowed_hosts: tuple[str, ...]
    allowed_path_prefixes: tuple[str, ...]
    timeout_seconds: float
    max_redirects: int
    requests_per_minute: int
    max_concurrency: int
    max_response_bytes: int = MAX_RESPONSE_BYTES
    robots_policy: str = "honor"

    @classmethod
    def from_source_policy(cls, source_policy: Mapping[str, object]) -> "EgressPolicy":
        allowed_hosts = tuple(
            str(host).strip().lower() for host in source_policy.get("allowed_hosts", ())
        )
        raw_paths = tuple(str(path) for path in source_policy.get("allowed_path_prefixes", ()))
        if any(
            "%" in path or any(char in path for char in DISALLOWED_PATH_CHARS)
            for path in raw_paths
        ):
            raise ValueError("SourcePolicy allowed_path_prefixes are ambiguous")
        if any(segment == ".." for path in raw_paths for segment in path.split("/")):
            raise ValueError("SourcePolicy allowed_path_prefixes cannot traverse")
        allowed_paths = tuple(effective_path(path) for path in raw_paths)
        if not allowed_hosts or any(not host or "*" in host for host in allowed_hosts):
            raise ValueError("SourcePolicy requires explicit allowed_hosts without wildcards")
        if not allowed_paths or any(not path.startswith("/") for path in allowed_paths):
            raise ValueError("SourcePolicy requires absolute allowed_path_prefixes")
        if source_policy.get("live_enabled") is not True:
            raise ValueError("live egress requires live_enabled=true")
        if source_policy.get("robots_policy", "honor") != "honor":
            raise ValueError("live egress requires robots_policy=honor")
        try:
            timeout = float(source_policy["timeout_seconds"])
            requests_per_minute = int(source_policy["requests_per_minute"])
            max_concurrency = int(source_policy["max_concurrency"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("SourcePolicy egress limits are malformed") from exc
        if not 1 <= timeout <= 120 or not 1 <= requests_per_minute <= 60 or not 1 <= max_concurrency <= 4:
            raise ValueError("SourcePolicy egress limits are out of range")
        return cls(
            allowed_hosts=allowed_hosts,
            allowed_path_prefixes=allowed_paths,
            timeout_seconds=timeout,
            max_redirects=3,
            requests_per_minute=requests_per_minute,
            max_concurrency=max_concurrency,
            robots_policy="honor",
        )

    def allows_host(self, host: str) -> bool:
        return host.strip().lower() in self.allowed_hosts

    def allows_path(self, path: str) -> bool:
        effective = effective_path(path)
        for prefix in self.allowed_path_prefixes:
            if prefix == "/":
                return True
            subtree_prefix = prefix if prefix.endswith("/") else f"{prefix}/"
            if effective == prefix or effective.startswith(subtree_prefix):
                return True
        return False
