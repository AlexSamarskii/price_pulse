from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pricepulse.collectors.capture_sanitizer import SanitizedCapture
from pricepulse.collectors.ast_checker import check_script_safety
from pricepulse.collectors.source_onboarding import SourceOnboarding, validate_fixture_path


CANDIDATE_CODE = '''import base64
import json
from html.parser import HTMLParser


class ProductParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.current = None
        self.field = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "article" and "data-product-id" in attributes:
            self.current = {
                "source": attributes.get("data-source", ""),
                "product_id": attributes["data-product-id"],
                "sku": attributes.get("data-sku", attributes["data-product-id"]),
                "title": "",
                "price": "",
                "currency": attributes.get("data-currency", ""),
                "availability": attributes.get("data-availability", ""),
                "attributes": {},
                "observed_at": attributes.get("data-observed-at", ""),
                "evidence": {"selector": "article[data-product-id]", "element_index": len(self.items)},
            }
        elif self.current is not None:
            name = attributes.get("data-field")
            if tag in ("h1", "h2", "h3") and self.current["title"] == "":
                self.field = "title"
            elif name:
                self.field = name

    def handle_data(self, data):
        if self.current is not None and self.field:
            if self.field == "title":
                self.current[self.field] += data
            else:
                self.current[self.field] += data

    def handle_endtag(self, tag):
        if tag == "article" and self.current is not None:
            for key in ("title", "price", "currency"):
                self.current[key] = self.current[key].strip()
            if not self.current["source"]:
                self.current["source"] = "__PRICEPULSE_SOURCE_ID__"
            if not self.current["currency"]:
                self.current["currency"] = "__PRICEPULSE_DEFAULT_CURRENCY__"
            self.current["availability"] = self.current["availability"].strip()
            self.items.append(self.current)
            self.current = None
            self.field = None


document = base64.b64decode("__PRICEPULSE_FIXTURE_B64__").decode("utf-8")
parser = ProductParser()
parser.feed(document)
print(json.dumps(parser.items, ensure_ascii=False))
'''


class SynthesisError(ValueError):
    pass


@dataclass(frozen=True)
class ScraperVersion:
    candidate_id: str
    version_id: str
    source_id: str
    status: str
    revision: int
    goal: str
    code: str
    code_sha256: str
    dependencies_sha256: str
    policy: dict[str, Any]
    policy_digest: str
    engine: str
    provider_id: str | None
    model: str | None
    fixture_verification: str


def generate_fixture_candidate(
    onboarding: SourceOnboarding,
    *,
    fixture_path: Path,
    provider_id: str | None = None,
    model: str | None = None,
    revision: int = 1,
) -> ScraperVersion:
    validate_fixture_path(fixture_path)
    if revision < 1:
        raise SynthesisError("revision must be positive")
    content = fixture_path.read_text(encoding="utf-8")
    code = (
        CANDIDATE_CODE.replace("__PRICEPULSE_SOURCE_ID__", onboarding.source_id)
        .replace("__PRICEPULSE_DEFAULT_CURRENCY__", onboarding.policy["allowed_currencies"][0])
        .replace("__PRICEPULSE_FIXTURE_B64__", base64.b64encode(content.encode("utf-8")).decode("ascii"))
    )
    safe, reason = check_script_safety(code)
    if not safe:
        raise SynthesisError(f"candidate failed AST safety: {reason}")
    code_digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    dependencies = {
        "engine": "pricepulse-httpx-fixture",
        "extraction_goal": onboarding.goal,
        "provider_id": provider_id,
        "model": model,
        "source_policy_digest": onboarding.policy_digest,
    }
    dependencies_digest = hashlib.sha256(
        json.dumps(dependencies, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    candidate_id = hashlib.sha256(
        f"{onboarding.source_id}:{onboarding.policy_digest}:{fixture_path.name}".encode("utf-8")
    ).hexdigest()[:24]
    version_id = hashlib.sha256(
        f"{candidate_id}:{code_digest}:{dependencies_digest}:{revision}".encode("utf-8")
    ).hexdigest()[:32]
    return ScraperVersion(
        candidate_id=candidate_id,
        version_id=version_id,
        source_id=onboarding.source_id,
        status="draft",
        revision=revision,
        goal=onboarding.goal,
        code=code,
        code_sha256=code_digest,
        dependencies_sha256=dependencies_digest,
        policy=onboarding.policy,
        policy_digest=onboarding.policy_digest,
        engine="pricepulse-httpx-fixture",
        provider_id=provider_id,
        model=model,
        fixture_verification="pending_offline_gate",
    )


def attach_probe_context(version: ScraperVersion, capture: SanitizedCapture) -> dict[str, Any]:
    return {
        "version_id": version.version_id,
        "policy_digest": version.policy_digest,
        "capture_digest": capture.content_digest,
    }
