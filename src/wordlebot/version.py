"""What's running: package version, git commit, and key library versions."""

from __future__ import annotations

import functools
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
    return os.environ.get("WORDLEBOT_COMMIT", "").strip() or _git_commit()


@functools.cache
def _git_commit() -> str:
    """Read once: it describes the code this process loaded, even if the checkout
    moves on, and the panel's dashboard asks every 30 seconds."""
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


def version_info(markdown: bool = True) -> str:
    """For `/version` (Discord markdown), or as plain text for the admin panel."""
    name, sha = ("**wordlebot**", f"`{commit()}`") if markdown else ("wordlebot", commit())
    return "\n".join(
        [
            f"{name} {_package('wordlebot')} (commit {sha})",
            f"discord.py {_package('discord.py')} · Python {platform.python_version()}",
        ]
    )
