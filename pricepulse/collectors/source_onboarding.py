from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit


REQUIRED_POLICY_FIELDS = {
    "source_id",
    "base_url",
    "allowed_hosts",
    "allowed_path_prefixes",
    "robots_policy",
    "max_pages",
    "requests_per_minute",
    "max_concurrency",
    "timeout_seconds",
    "allowed_currencies",
    "live_enabled",
}


class SourcePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SourceOnboarding:
    source_id: str
    goal: str
    policy: dict[str, Any]
    policy_digest: str


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SourcePolicyError(f"{field} must be a nonempty string")
    return value


def _validate_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SourcePolicyError(f"{field} is out of range")
    return value


def _string_list(value: Any, field: str, minimum: int = 1) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise SourcePolicyError(f"{field} must be a list with at least {minimum} item(s)")
    if any(not isinstance(item, str) or not item for item in value):
        raise SourcePolicyError(f"{field} contains an invalid string")
    return value


def normalize_host(host: str) -> str:
    normalized = unicodedata.normalize("NFKC", host).strip().lower().rstrip(".")
    if not normalized:
        raise SourcePolicyError("host is empty")
    if any(char in normalized for char in " /@?#") or normalized.startswith("[") or normalized.endswith("]"):
        raise SourcePolicyError("host contains an invalid character")
    try:
        return normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SourcePolicyError("host cannot be encoded as an IDNA name") from exc


def effective_path(path: str) -> str:
    if "\x00" in path or ";" in path or "\\" in path or "%" in path:
        raise SourcePolicyError("URL path contains an ambiguous character")
    current = unquote(path, encoding="utf-8", errors="strict")
    segments = current.split("/")
    if any(segment == ".." for segment in segments):
        raise SourcePolicyError("URL path escapes its allowed root")
    return current or "/"


def validate_source_policy(raw: Mapping[str, Any]) -> dict[str, Any]:
    keys = set(raw)
    missing = REQUIRED_POLICY_FIELDS - keys
    extra = keys - REQUIRED_POLICY_FIELDS
    if missing:
        raise SourcePolicyError(f"missing required fields: {sorted(missing)}")
    if extra:
        raise SourcePolicyError(f"unexpected fields: {sorted(extra)}")

    source_id = _require_string(raw["source_id"], "source_id")
    if len(source_id) > 64 or source_id != source_id.lower():
        raise SourcePolicyError("source_id has an invalid format")
    if not source_id[0].isalpha() or not source_id[0].isascii() or not all(
        char.isascii() and (char.isalnum() or char in "-_") for char in source_id
    ):
        raise SourcePolicyError("source_id has an invalid format")

    base_url = _require_string(raw["base_url"], "base_url")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SourcePolicyError("base_url must be an HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise SourcePolicyError("base_url must not contain credentials or a fragment")
    if parsed.query:
        raise SourcePolicyError("base_url must not contain query parameters")
    if any(char in parsed.path for char in ";\\%\x00") or any(
        segment == ".." for segment in parsed.path.split("/")
    ):
        raise SourcePolicyError("base_url path is ambiguous or traverses")
    host = normalize_host(parsed.hostname or "")
    allowed_hosts = [normalize_host(item) for item in _string_list(raw["allowed_hosts"], "allowed_hosts")]
    if "*" in allowed_hosts or host not in allowed_hosts:
        raise SourcePolicyError("base_url host is not explicitly allowed")
    if any("*" in item or " " in item for item in allowed_hosts):
        raise SourcePolicyError("allowed_hosts must be explicit")

    allowed_path_prefixes = _string_list(raw["allowed_path_prefixes"], "allowed_path_prefixes")
    if any(not item.startswith("/") for item in allowed_path_prefixes):
        raise SourcePolicyError("allowed_path_prefixes must be absolute")
    if any(item.startswith("//") for item in allowed_path_prefixes):
        raise SourcePolicyError("allowed_path_prefixes must not be protocol-relative")
    if any(
        segment == ".." or any(char in segment for char in ";\\%\x00")
        for item in allowed_path_prefixes
        for segment in item.split("/")
    ):
        raise SourcePolicyError("allowed_path_prefixes are ambiguous or traverse")
    if not effective_path(parsed.path).startswith(tuple(allowed_path_prefixes)):
        raise SourcePolicyError("base_url path is outside allowed_path_prefixes")

    robots_policy = raw["robots_policy"]
    if robots_policy not in {"honor", "operator_authorized_fixture"}:
        raise SourcePolicyError("robots_policy is invalid")
    live_enabled = raw["live_enabled"]
    if not isinstance(live_enabled, bool):
        raise SourcePolicyError("live_enabled must be boolean")
    if live_enabled and robots_policy == "operator_authorized_fixture":
        raise SourcePolicyError("live collection requires robots_policy=honor")

    currencies = _string_list(raw["allowed_currencies"], "allowed_currencies")
    if any(len(item) != 3 or item != item.upper() or not item.isascii() for item in currencies):
        raise SourcePolicyError("allowed_currencies must be ISO-4217-like uppercase codes")
    if len(set(currencies)) != len(currencies):
        raise SourcePolicyError("allowed_currencies contains duplicates")

    policy = dict(raw)
    policy["source_id"] = source_id
    policy["base_url"] = base_url
    policy["allowed_hosts"] = allowed_hosts
    policy["max_pages"] = _validate_int(raw["max_pages"], "max_pages", 1, 1000)
    policy["requests_per_minute"] = _validate_int(raw["requests_per_minute"], "requests_per_minute", 1, 60)
    policy["max_concurrency"] = _validate_int(raw["max_concurrency"], "max_concurrency", 1, 4)
    policy["timeout_seconds"] = _validate_int(raw["timeout_seconds"], "timeout_seconds", 1, 120)
    policy["allowed_currencies"] = currencies
    policy["live_enabled"] = live_enabled
    return policy


def validate_target_url(url: str, policy: Mapping[str, Any]) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SourcePolicyError("target URL must be HTTPS")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise SourcePolicyError("target URL must not contain credentials, query, or fragment")
    host = normalize_host(parsed.hostname or "")
    if host not in policy["allowed_hosts"]:
        raise SourcePolicyError("target host is outside SourcePolicy")
    if not effective_path(parsed.path).startswith(tuple(policy["allowed_path_prefixes"])):
        raise SourcePolicyError("target path is outside SourcePolicy")
    return url


def onboard_source(raw_policy: Mapping[str, Any], goal: str) -> SourceOnboarding:
    if not isinstance(goal, str) or not goal.strip():
        raise SourcePolicyError("goal must be a nonempty string")
    policy = validate_source_policy(raw_policy)
    policy_digest = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return SourceOnboarding(
        source_id=policy["source_id"],
        goal=goal.strip(),
        policy=policy,
        policy_digest=policy_digest,
    )


def validate_fixture_path(path: Path, *, maximum_bytes: int = 1_000_000) -> Path:
    if not path.is_file():
        raise SourcePolicyError("fixture path must be an existing file")
    if path.suffix.lower() not in {".html", ".htm"}:
        raise SourcePolicyError("fixture path must be an HTML file")
    if path.stat().st_size > maximum_bytes:
        raise SourcePolicyError("fixture path exceeds the input limit")
    return path
