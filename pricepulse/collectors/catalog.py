from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pricepulse.collectors.capture_sanitizer import sanitize_capture
from pricepulse.collectors.challenge_detector import CaptchaDetector
from pricepulse.collectors.offline_gate import _verify_version_identity, run_offline_gate
from pricepulse.egress.broker import EgressBroker
from pricepulse.egress.policy import EgressPolicy
from pricepulse.runtime import ExecutionRequest


class CollectionError(ValueError):
    pass


class CollectionBlocked(CollectionError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def make_version(policy, policy_digest):
    code = Path(__file__).with_name("limestore_worker.py").read_text(encoding="utf-8")
    goal = "Collect selected LIME catalog product colors and online size variants"
    dependencies = {"engine": "pricepulse-limestore", "extraction_goal": goal,
                    "provider_id": None, "model": None, "source_policy_digest": policy_digest}
    candidate_id = digest(policy["source_id"] + ":limestore")[:24]
    code_sha = digest(code)
    dependencies_sha = digest(canonical_json(dependencies))
    version = {"candidate_id": candidate_id, "version_id": digest(f"{candidate_id}:{code_sha}:{dependencies_sha}:1")[:32],
               "source_id": policy["source_id"], "revision": 1, "code": code, "code_sha256": code_sha,
               "dependencies_sha256": dependencies_sha, "policy": policy, "policy_digest": policy_digest,
               "goal": goal, "engine": "pricepulse-limestore", "provider_id": None, "model": None}
    _verify_version_identity(version)
    return version


def allowed_url(url, policy, kind):
    parts = urlsplit(url)
    prefix = "/ru_ru/product/" if kind == "product" else "/ru_ru/catalog/"
    return (parts.scheme == "https" and parts.netloc == "limestore.com" and not parts.query and not parts.fragment
            and parts.path.startswith(prefix) and EgressPolicy.from_source_policy(policy).allows_path(parts.path)
            and not any(c in parts.path for c in "%\\;\x00")
            and not any(p in {".", ".."} for p in parts.path.split("/")))


class PacedBroker(EgressBroker):
    def __init__(self, policy, before_send, *, transport=None, resolver=None):
        kwargs = {"transport": transport}
        if resolver is not None:
            kwargs["resolver"] = resolver
        super().__init__(EgressPolicy.from_source_policy(policy), **kwargs)
        self.before_send = before_send
        self.source_policy = policy
        self.audit = []

    def _send_once(self, request):
        url = str(request.url)
        if request.url.path == "/robots.txt":
            if (urlsplit(url).netloc != "limestore.com" or request.url.scheme != "https"
                    or request.url.query or request.url.fragment):
                raise CollectionBlocked("robots redirect is outside the collection contract")
        elif not allowed_url(url, self.source_policy, "product" if request.url.path.startswith("/ru_ru/product/") else "catalog"):
            raise CollectionBlocked("request or redirect is outside the collection contract")
        self.before_send()
        response = super()._send_once(request)
        capture = sanitize_capture(content=response.text)
        self.audit.append({"url": str(request.url), "method": request.method, "status": response.status_code,
                           "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                           "capture_sha256": capture.content_digest})
        return response

    def close(self):
        self._client.close()


class CatalogCollector:
    def __init__(self, config, version, runner, broker):
        self.config = config
        self.version = version
        self.runner = runner
        self.broker = broker
        self.pages = 0

    def capture(self, url, mode):
        policy = self.config["policy"]
        if not allowed_url(url, policy, "product" if mode == "product" else "catalog"):
            raise CollectionBlocked("target is outside the collection contract")
        if self.pages >= policy["max_pages"]:
            raise CollectionError("page budget exhausted")
        self.pages += 1
        response = self.broker.request("GET", url)
        if response.status_code in {401, 403, 429}:
            raise CollectionBlocked(f"source returned HTTP {response.status_code}")
        if response.status_code != 200:
            raise CollectionError(f"source returned HTTP {response.status_code}")
        capture = sanitize_capture(content=response.text)
        if not capture.content.strip():
            raise CollectionError("source returned an empty page")
        if CaptchaDetector().detect_html(capture.content) is not None:
            raise CollectionBlocked("source returned a challenge")
        context = {"mode": mode, "source_id": policy["source_id"], "url": url,
                   "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                   "capture_sha256": capture.content_digest, "html": capture.content}
        return canonical_json(context).encode()

    def discover(self, job_id):
        pending = list(self.config["catalog_urls"])
        visited = set()
        products = []
        while pending and len(visited) < self.config["max_catalog_pages"]:
            url = pending.pop(0)
            if url in visited:
                continue
            visited.add(url)
            payload = self.capture(url, "catalog")
            execution = self.runner.run(ExecutionRequest(job_id=job_id, script=self.version["code"].encode(),
                input_data=payload, env={}, timeout_seconds=10, output_limit_bytes=262144))
            if (execution.status != "completed" or execution.exit_code != 0
                    or execution.error or not execution.cleanup_completed):
                raise CollectionError("catalog extractor execution failed")
            try:
                data = json.loads(execution.stdout)
                urls, pages = data["product_urls"], data["next_urls"]
                if not isinstance(urls, list) or not isinstance(pages, list) or len(urls) > 500 or len(pages) > 10:
                    raise ValueError("invalid catalog result")
                products.extend(u for u in urls if isinstance(u, str) and allowed_url(u, self.config["policy"], "product"))
                pending.extend(u for u in pages if isinstance(u, str) and allowed_url(u, self.config["policy"], "catalog"))
            except (ValueError, TypeError, KeyError) as exc:
                raise CollectionError("catalog extractor returned an invalid result") from exc
            products = list(dict.fromkeys(products))
            if len(products) >= self.config["max_products"]:
                break
        if not products:
            raise CollectionError("catalog contains no supported product links")
        return products[:self.config["max_products"]]

    def extract(self, url, job_id):
        payload = self.capture(url, "product")
        gate = run_offline_gate(self.version, self.runner, job_id=job_id, input_data=payload)
        if gate.status != "passed":
            raise CollectionError("product execution/data gate failed: " + str(gate.reason))
        context = json.loads(payload)
        if any(p["source"] != context["source_id"] or p.get("canonical_url") != url
               or p["observed_at"] != context["observed_at"]
               or p["evidence"].get("capture_sha256") != context["capture_sha256"] for p in gate.products):
            raise CollectionError("product provenance does not match the capture")
        return gate.products
