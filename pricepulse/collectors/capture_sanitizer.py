from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


SENSITIVE_KEY_PATTERN = (
    r"(?:[a-z0-9]+[_-])*"
    r"(?:api[_-]?key|private[_-]?key|authorization|auth|cookies?|credentials?|"
    r"password|profiles?|secrets?|session|set[_-]?cookie2?|token|csrf|sid|key|"
    r"www[_-]authenticate)"
    r"(?:[_-][a-z0-9]+)*"
)
SENSITIVE_KEY_RE = re.compile(SENSITIVE_KEY_PATTERN, re.IGNORECASE)

QUERY_SECRET_RE = re.compile(
    rf"(?P<prefix>[?&])(?P<key>{SENSITIVE_KEY_PATTERN})=[^&\s\"'>]+",
    re.IGNORECASE,
)
KEY_VALUE_SECRET_RE = re.compile(
    rf"(?<![\w-])(?P<key>{SENSITIVE_KEY_PATTERN})\s*[:=]\s*(?:\[REDACTED\]|[^,&\s\r\n}}\]\"']+)",
    re.IGNORECASE,
)
QUOTED_KEY_VALUE_SECRET_RE = re.compile(
    rf"(?<![\w-])(?:(?P<quote>[\"'])(?P<key>{SENSITIVE_KEY_PATTERN})(?P=quote)|"
    rf"{SENSITIVE_KEY_PATTERN}(?=\s*[:=]\s*[\"'\[{{]))\s*[:=]\s*",
    re.IGNORECASE,
)
BEARER_SECRET_RE = re.compile(r"(?P<scheme>(?:bearer|basic|token)\s+)\S+", re.IGNORECASE)
SINGLE_QUOTED_VALUE_RE = re.compile(r"'(?:[^'\\]|\\.)*'")


def _sanitize_quoted_values(content: str) -> str:
    decoder = json.JSONDecoder()
    parts: list[str] = []
    cursor = 0
    for match in QUOTED_KEY_VALUE_SECRET_RE.finditer(content):
        if match.start() < cursor:
            continue
        try:
            _, end = decoder.raw_decode(content, match.end())
        except ValueError:
            value = SINGLE_QUOTED_VALUE_RE.match(content, match.end())
            if value is None:
                continue
            end = value.end()
        parts.extend((content[cursor:match.end()], '"[REDACTED]"'))
        cursor = end
    parts.append(content[cursor:])
    return "".join(parts)


def sanitize_content(content: str) -> str:
    content = _sanitize_quoted_values(content)
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


def sanitize_headers(headers: Mapping[str, Any]) -> tuple[dict[str, str], int]:
    sanitized: dict[str, str] = {}
    count = 0
    for key, value in headers.items():
        normalized = str(key).lower()
        if SENSITIVE_KEY_RE.fullmatch(normalized):
            count += 1
            continue
        text = str(value)
        sanitized[normalized] = sanitize_content(text)
    return sanitized, count


def sanitize_query(query_parameters: Mapping[str, Any]) -> tuple[dict[str, str], int]:
    sanitized: dict[str, str] = {}
    count = 0
    for key, value in query_parameters.items():
        normalized = str(key).lower()
        if SENSITIVE_KEY_RE.fullmatch(normalized):
            count += 1
            continue
        sanitized[normalized] = sanitize_content(str(value))
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
    redaction_count += len(QUOTED_KEY_VALUE_SECRET_RE.findall(content))
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
            str(key): "[REDACTED]" if SENSITIVE_KEY_RE.fullmatch(str(key)) else sanitize_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_log(item) for item in value)
    if isinstance(value, str):
        return sanitize_content(value)
    return value
