"""Capybot Apply: an evidence-first job-search decision agent."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path


def _source_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.exists():
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip().strip('"')
    return "0.3.0"


try:
    __version__ = package_version("capybot-ai")
except PackageNotFoundError:
    __version__ = _source_version()


__all__ = ["__version__"]
