"""Repository-wide test environment normalization."""

from __future__ import annotations

import os


def pytest_configure() -> None:
    """Keep loopback integration tests away from OS-level HTTP proxies."""

    hosts = {"127.0.0.1", "localhost", "::1"}
    for key in ("NO_PROXY", "no_proxy"):
        existing = {item.strip() for item in os.environ.get(key, "").split(",") if item.strip()}
        os.environ[key] = ",".join(sorted(existing | hosts))
