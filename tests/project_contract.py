"""Helpers for tests that must survive project initialization."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
_HOST_PLACEHOLDER = re.compile(r"\$\{(?P<prefix>[A-Z][A-Z0-9_]*)_HOST(?::-[^}]*)?}")
_PORT_PLACEHOLDER = re.compile(
    r"\$\{(?P<prefix>[A-Z][A-Z0-9_]*)_PORT:-(?P<port>[0-9]+)}"
)


@dataclass(frozen=True, slots=True)
class ProjectContract:
    distribution: str
    display_name: str
    env_prefix: str
    port: int

    def environment_name(self, suffix: str) -> str:
        return f"{self.env_prefix}_{suffix}"


def load_project_contract(root: Path = ROOT) -> ProjectContract:
    with (root / "pyproject.toml").open("rb") as stream:
        distribution = tomllib.load(stream)["project"]["name"]
    application_text = (root / "config/application.yaml").read_text(encoding="utf-8")
    application = yaml.safe_load(application_text)
    host_match = _HOST_PLACEHOLDER.search(application_text)
    port_match = _PORT_PLACEHOLDER.search(application_text)
    assert isinstance(distribution, str) and distribution
    assert isinstance(application, dict)
    assert isinstance(application.get("service"), dict)
    display_name = application["service"].get("name")
    assert isinstance(display_name, str) and display_name
    assert host_match is not None
    assert port_match is not None
    assert port_match.group("prefix") == host_match.group("prefix")
    return ProjectContract(
        distribution=distribution,
        display_name=display_name,
        env_prefix=host_match.group("prefix"),
        port=int(port_match.group("port")),
    )
