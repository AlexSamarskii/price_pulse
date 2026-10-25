from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "password",
    "private_key",
    "profile",
    "profiles",
    "secret",
    "secrets",
    "session",
    "set-cookie",
    "set-cookie2",
    "token",
}
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "authorization",
    "id_token",
    "key",
    "password",
    "session",
    "sid",
    "token",
}

QUERY_SECRET_RE = re.compile(
    r"(?P<prefix>[?&])(?P<key>(?:api[_-]?key|access[_-]?token|authorization|auth|password|session|sid|token))=[^&\s\"'>]+",
    re.IGNORECASE,
)
KEY_VALUE_SECRET_RE = re.compile(
    r"(?P<key>(?:api[_-]?key|authorization|cookie|credentials|password|private[_-]?key|secret|token))\s*[:=]\s*[^,\s\r\n}\]\"']+",
    re.IGNORECASE,
)
BEARER_SECRET_RE = re.compile(r"(?P<scheme>(?:bearer|basic|token)\s+)\S+", re.IGNORECASE)


def sanitize_content(content: str) -> str:
    content = QUERY_SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}{match.group('key')}=[REDACTED]", content
    )
    content = BEARER_SECRET_RE.sub(r"\g<scheme>[REDACTED]", content)
    content = KEY_VALUE_SECRET_RE.sub(r"\g<key>=[REDACTED]", content)
    return content


@dataclass(frozen=True)
class SanitizedCapture:
    headers: dict[str, str]
    cookies: dict[str, str]
    query_parameters: dict[str, str]
    content: str
    content_digest: str
    redaction_count: int

    def to_log_payload(self) -> dict[str, Any]:
        return {
            "headers": dict(self.headers),
            "cookies": dict(self.cookies),
            "query_parameters": dict(self.query_parameters),
            "content": self.content,
            "content_digest": self.content_digest,
            "redaction_count": self.redaction_count,
        }


def _redacted_count(value: Any) -> int:
    return 1 if value == "[REDACTED]" else 0


def sanitize_headers(headers: Mapping[str, Any]) -> tuple[dict[str, str], int]:
    sanitized: dict[str, str] = {}
    count = 0
    for key, value in headers.items():
        normalized = str(key).lower()
        if normalized in SENSITIVE_KEYS or normalized.startswith("x-api-"):
            count += 1
            continue
        text = str(value)
        if normalized in {"proxy-authorization", "www-authenticate"}:
            count += 1
            continue
        sanitized[normalized] = text
    return sanitized, count


def sanitize_query(query_parameters: Mapping[str, Any]) -> tuple[dict[str, str], int]:
    sanitized: dict[str, str] = {}
    count = 0
    for key, value in query_parameters.items():
        normalized = str(key).lower()
        if normalized in SENSITIVE_QUERY_KEYS:
            count += 1
            continue
        sanitized[normalized] = str(value)
    return sanitized, count


def sanitize_capture(
    *,
    headers: Mapping[str, Any] | None = None,
    cookies: Mapping[str, Any] | None = None,
    query_parameters: Mapping[str, Any] | None = None,
    content: str,
) -> SanitizedCapture:
    sanitized_headers, header_count = sanitize_headers(headers or {})
    sanitized_cookies = {"[REDACTED]": "[REDACTED]"} if cookies else {}
    sanitized_query, query_count = sanitize_query(query_parameters or {})
    sanitized_content = sanitize_content(content)
    redaction_count = header_count + query_count + (len(cookies) if cookies else 0)
    redaction_count += len(QUERY_SECRET_RE.findall(content))
    redaction_count += len(KEY_VALUE_SECRET_RE.findall(content))
    redaction_count += len(BEARER_SECRET_RE.findall(content))
    encoded = sanitized_content.encode("utf-8")
    return SanitizedCapture(
        headers=sanitized_headers,
        cookies=sanitized_cookies,
        query_parameters=sanitized_query,
        content=sanitized_content,
        content_digest=hashlib.sha256(encoded).hexdigest(),
        redaction_count=redaction_count,
    )


def sanitize_for_log(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else sanitize_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_log(item) for item in value)
    if isinstance(value, dict):
        return json.loads(json.dumps(value))
    return value
