"""What's running: package version, git commit, and key library versions."""

from __future__ import annotations

import os
import platform
import subprocess
from importlib import metadata
from pathlib import Path


def _package(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def commit() -> str:
    """The deployed commit: baked into the image by update.sh, else read from git."""
    if sha := os.environ.get("WORDLEBOT_COMMIT", "").strip():
        return sha
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def version_info() -> str:
    return "\n".join(
        [
            f"**wordlebot** {_package('wordlebot')} (commit `{commit()}`)",
            f"discord.py {_package('discord.py')} · Python {platform.python_version()}",
        ]
    )
