"""Host environment detection and the destination policy that follows.

The destination is not a cosmetic choice -- it constrains the codec. Music.app
cannot decode Opus, so choosing to land files in an Apple library forces the
AAC source stream. Detecting the environment therefore has to happen *before*
stream selection, not after it.

Every value here is a default. `config.Config` overrides all of it; see
`resolve()` for how a user's explicit choice takes precedence.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Host(Enum):
    MACOS = "macos"
    WINDOWS = "windows"
    LINUX = "linux"
    UNKNOWN = "unknown"


class Handoff(Enum):
    """What happens to a finished file."""

    #: Hand to Music.app, which copies it into its own media folder; our
    #: staging copy is then redundant and gets removed.
    APPLE_MUSIC = "apple-music"
    #: Write into the OS music folder that the system player already indexes.
    SYSTEM_LIBRARY = "system-library"
    #: Leave it where it landed and print the path.
    FILESYSTEM = "filesystem"


MUSIC_APP = Path("/System/Applications/Music.app")
#: Music.app's "copy files to media folder on add" preference. When true, an
#: import duplicates the file and our staging copy must be cleaned up; when
#: false, Music.app references our file in place and deleting it would break
#: the library entry.
MUSIC_COPY_PREF = ("com.apple.Music", "copy-files-to-library")


@dataclass(frozen=True)
class Environment:
    host: Host
    profile: str
    handoff: Handoff
    library_root: Path
    #: Populated on macOS only; None when the preference can't be read.
    apple_music_copies_on_import: bool | None = None

    def describe(self) -> str:
        lines = [
            f"Host: {self.host.value}",
            f"Default profile: {self.profile}",
            f"Library root: {self.library_root}",
            f"Handoff: {self.handoff.value}",
        ]
        if self.host is Host.MACOS:
            lines.append(
                "Music.app cannot decode Opus, so the Apple profile pins the "
                "AAC source stream -- 256k with valid YouTube Music "
                "credentials, 128k without."
            )
        return "\n".join(lines)


def detect_host() -> Host:
    system = platform.system().lower()
    if system == "darwin":
        return Host.MACOS
    if system == "windows":
        return Host.WINDOWS
    if system == "linux":
        return Host.LINUX
    return Host.UNKNOWN


def has_music_app() -> bool:
    return sys.platform == "darwin" and MUSIC_APP.exists()


def apple_music_copies_on_import() -> bool | None:
    """Read Music.app's import-copy preference.

    Returns None when the preference has never been written, which is the
    common case on a fresh account -- Apple's own default is to copy, so
    callers should treat None as "probably copies, verify before deleting".
    """
    if sys.platform != "darwin" or not shutil.which("defaults"):
        return None
    import subprocess

    domain, key = MUSIC_COPY_PREF
    try:
        result = subprocess.run(
            ["defaults", "read", domain, key],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() in {"1", "true", "YES"}


def default_library_root(host: Host) -> Path:
    """The folder the platform's own music player already watches."""
    if host is Host.MACOS:
        return Path.home() / "Music" / "hifi-rip"
    if host is Host.WINDOWS:
        # Media Player indexes %USERPROFILE%\Music automatically, so writing
        # here is all that "adding to the library" requires.
        profile_dir = os.environ.get("USERPROFILE")
        base = Path(profile_dir) if profile_dir else Path.home()
        return base / "Music" / "hifi-rip"
    xdg = os.environ.get("XDG_MUSIC_DIR")
    return (Path(xdg) if xdg else Path.home() / "Music") / "hifi-rip"


def detect() -> Environment:
    """Best-guess environment before any user configuration is applied."""
    host = detect_host()

    if host is Host.MACOS and has_music_app():
        return Environment(
            host=host,
            profile="apple",
            handoff=Handoff.APPLE_MUSIC,
            library_root=default_library_root(host),
            apple_music_copies_on_import=apple_music_copies_on_import(),
        )
    if host is Host.WINDOWS:
        return Environment(
            host=host,
            profile="windows",
            handoff=Handoff.SYSTEM_LIBRARY,
            library_root=default_library_root(host),
        )
    return Environment(
        host=host,
        profile="archive",
        handoff=Handoff.FILESYSTEM if host is Host.UNKNOWN else Handoff.SYSTEM_LIBRARY,
        library_root=default_library_root(host),
    )
