"""Environment lookup for Craftarr configuration."""

import os


def getenv(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)
