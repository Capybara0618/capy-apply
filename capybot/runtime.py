"""Small runtime helpers shared by the standalone Capybot services."""

from __future__ import annotations

import os
from pathlib import Path


def ensure_dir(path: str | Path) -> Path:
    target = Path(path).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target


def data_dir() -> Path:
    configured = os.getenv("CAPYBOT_HOME")
    return ensure_dir(configured or Path.home() / ".capybot")


def runtime_dir(name: str) -> Path:
    return ensure_dir(data_dir() / name)
