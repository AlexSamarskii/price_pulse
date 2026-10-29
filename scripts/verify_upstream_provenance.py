#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_SEGMENTS = {".env", ".ssh", ".gnupg", "id_rsa", "id_ed25519"}
SENSITIVE_PREFIXES = (".env.",)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sensitive_path(name: str) -> bool:
    path = PurePosixPath(name)
    return path.is_absolute() or ".." in path.parts or any(
        part in SENSITIVE_SEGMENTS or part.startswith(SENSITIVE_PREFIXES)
        for part in path.parts
    )


def _archive_entries(archive: Path) -> tuple[dict[str, str], set[str]]:
    file_hashes: dict[str, str] = {}
    directory_names: set[str] = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.name or PurePosixPath(member.name).is_absolute():
                raise ValueError("archive contains an absolute path")
            if _is_sensitive_path(member.name):
                raise ValueError(f"archive contains a sensitive path: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"archive contains a link: {member.name}")
            if member.isdir():
                directory_names.add(member.name)
                continue
            if not member.isfile():
                raise ValueError(f"archive contains a foreign payload: {member.name}")
            if member.mode & 0o111:
                raise ValueError(f"archive entry is executable: {member.name}")
            stream = tar.extractfile(member)
            if stream is None:
                raise ValueError(f"archive file cannot be read: {member.name}")
            file_hashes[member.name] = hashlib.sha256(stream.read()).hexdigest()
    return file_hashes, directory_names


def verify(
    manifest_path: Path,
    archive_path: Path,
    integration_path: Path,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    integration = json.loads(integration_path.read_text(encoding="utf-8"))
    if manifest.get("repository") != "https://github.com/YunyiFlux/ScrapeClaw":
        raise ValueError("upstream repository changed")
    if manifest.get("commit") != "4e0ed78478db38c1173610b39dba573dcf46bd77":
        raise ValueError("upstream commit changed")
    if manifest.get("license") != "MIT":
        raise ValueError("upstream license changed")
    actual_archive_hash = sha256_file(archive_path)
    if integration.get("snapshot_sha256") != actual_archive_hash:
        raise ValueError("source snapshot digest does not match the integration manifest")

    expected_hashes = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    actual_hashes, _ = _archive_entries(archive_path)
    missing = sorted(set(expected_hashes) - set(actual_hashes))
    foreign = sorted(set(actual_hashes) - set(expected_hashes))
    mismatched = sorted(
        name
        for name in set(expected_hashes) & set(actual_hashes)
        if expected_hashes[name] != actual_hashes[name]
    )
    if missing or foreign or mismatched:
        raise ValueError(
            "source snapshot membership mismatch: "
            f"missing={missing}, foreign={foreign}, mismatched={mismatched}"
        )

    mappings = integration.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("integration manifest has no adapted-code mappings")
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ValueError("integration mapping must be an object")
        adapted_path = mapping.get("adapted_path")
        upstream_path = mapping.get("upstream_path")
        if not isinstance(adapted_path, str) or adapted_path.startswith("/") or ".." in PurePosixPath(adapted_path).parts:
            raise ValueError(f"integration mapping has an invalid adapted path: {adapted_path}")
        if upstream_path not in expected_hashes:
            raise ValueError(f"integration mapping points outside the snapshot: {upstream_path}")
        if expected_hashes[upstream_path] != actual_hashes.get(upstream_path):
            raise ValueError(f"integration mapping source hash changed: {upstream_path}")

    return {
        "status": "pass",
        "archive_sha256": actual_archive_hash,
        "verified_members": len(expected_hashes),
        "integration_mappings": len(mappings),
        "commit": manifest["commit"],
        "license": manifest["license"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify immutable upstream provenance")
    parser.add_argument("--manifest", type=Path, default=PROJECT_ROOT / "docs/upstream/source_manifest.json")
    parser.add_argument("--snapshot", type=Path, default=PROJECT_ROOT / "docs/upstream/source_snapshot.tar.gz")
    parser.add_argument("--integration", type=Path, default=PROJECT_ROOT / "docs/upstream/integration_manifest.json")
    arguments = parser.parse_args()
    try:
        result = verify(arguments.manifest, arguments.snapshot, arguments.integration)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, tarfile.TarError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
