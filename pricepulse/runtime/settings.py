from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class RuntimeSettings:
    docker_binary: str
    runtime_image: str
    name_prefix: str
    command_path: str

    @classmethod
    def from_mapping(cls, values: Mapping[str, str] | None = None) -> "RuntimeSettings":
        source = dict(values or {})
        return cls(
            docker_binary=source.get("docker_binary", "docker"),
            runtime_image=source.get("runtime_image", "pricepulse-runtime:local"),
            name_prefix=source.get("name_prefix", "pricepulse-sandbox"),
            command_path=source.get("command_path", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
        )
