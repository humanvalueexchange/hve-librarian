"""Client boundary for the independently deployed HVE knowledge layer."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


KNOWLEDGE_INSTALL_ROOT = Path(
    os.environ.get("HVE_KNOWLEDGE_INSTALL_ROOT", "/opt/hve-knowledge-layer/current")
)
KNOWLEDGE_PYTHON = Path(
    os.environ.get(
        "HVE_KNOWLEDGE_PYTHON",
        str(Path.home() / ".hve-knowledge" / "venv" / "bin" / "python3"),
    )
)
KNOWLEDGE_ROOT = Path(os.environ.get("HVE_LIBRARY_ROOT", "/hve-library"))
EMBEDDING_CONTRACT_MODEL = os.environ.get(
    "HVE_EMBEDDING_CONTRACT_MODEL", "nomic-embed-text-v1.5"
)


def cli_command(*arguments: str, root: Path | None = None) -> list[str]:
    return [
        str(KNOWLEDGE_PYTHON),
        "-m",
        "hve_knowledge_layer.cli",
        "--root",
        str(root or KNOWLEDGE_ROOT),
        *arguments,
    ]


def cli_environment() -> dict[str, str]:
    environment = os.environ.copy()
    package_path = str(KNOWLEDGE_INSTALL_ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{package_path}:{existing}" if existing else package_path
    )
    environment["HVE_KNOWLEDGE_CONFIG"] = str(
        KNOWLEDGE_INSTALL_ROOT / "config" / "knowledge-layer" / "knowledge-layer.yaml"
    )
    return environment


def run_cli(arguments: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cli_command(*arguments),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=cli_environment(),
    )
