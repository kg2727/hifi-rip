"""Layered configuration -- the steering mechanism.

Two users running the same skill must be able to get different behaviour
without editing it, and a user must be able to redirect it mid-conversation
without editing anything at all. So settings resolve through ordered layers,
lowest precedence first:

    1. built-in defaults
    2. detected environment      (macOS -> Apple profile, etc.)
    3. config file               (~/.config/hifi-rip/config.toml)
    4. environment variables     (HIFI_RIP_*)
    5. explicit overrides        (CLI flags, or an agent passing through
                                  what the user just asked for in chat)

Every resolved field records which layer produced it. That provenance is
not decoration: when an agent tells a user "I'm ripping this as AAC," the
user needs to know whether that came from their config file or from the
machine they happen to be sitting at, because those call for different
corrections.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import environment as env_mod

CONFIG_ENV_VAR = "HIFI_RIP_CONFIG"
ENV_PREFIX = "HIFI_RIP_"

#: Layer names, ordered by increasing precedence.
LAYERS = ("default", "environment", "file", "env", "override")


def config_path() -> Path:
    explicit = os.environ.get(CONFIG_ENV_VAR)
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "hifi-rip" / "config.toml"


@dataclass
class Config:
    """Resolved settings, plus where each one came from."""

    profile: str = "archive"
    cookies_from_browser: str | None = None
    cookies_file: str | None = None
    library_root: Path = field(default_factory=lambda: Path.home() / "Music" / "hifi-rip")
    handoff: str = "filesystem"
    #: Per-track path template, applied under `library_root`.
    naming: str = "{albumartist}/{album}/{track:02d} - {title}"
    #: Used when a recording has neither album nor track number. The album
    #: template would otherwise invent both, yielding "Unknown Album/00 - X".
    naming_single: str = "{albumartist}/{title}"
    #: Off by default and never enabled implicitly: transcoding stacks a
    #: second lossy generation, so it has to be an explicit human choice.
    allow_transcode: bool = False
    sponsorblock: bool = True
    sponsorblock_categories: tuple[str, ...] = ("music_offtopic", "sponsor", "intro", "outro")
    #: Metadata sources to consult; unavailable ones degrade gracefully.
    sources: tuple[str, ...] = (
        "yt_chapters", "yt_description", "yt_comments", "musicbrainz",
        "discogs", "lastfm", "sponsorblock", "acoustid", "silence",
    )
    #: How many independent sources must agree before a value is accepted
    #: without escalating to the host agent for judgement.
    consensus_threshold: int = 2

    provenance: dict[str, str] = field(default_factory=dict, repr=False)

    def source_of(self, name: str) -> str:
        return self.provenance.get(name, "default")

    def describe(self) -> str:
        lines = ["Resolved configuration (value <- layer):"]
        for f in fields(self):
            if f.name == "provenance":
                continue
            lines.append(
                f"  {f.name} = {getattr(self, f.name)!r}  <- {self.source_of(f.name)}"
            )
        return "\n".join(lines)


_COERCE = {
    "library_root": lambda v: Path(v).expanduser(),
    "sources": tuple,
    "sponsorblock_categories": tuple,
}


def _apply(config: Config, values: dict[str, Any], layer: str) -> None:
    """Overlay `values` onto `config`, recording provenance.

    Unknown keys are ignored rather than fatal: a config file written for a
    newer version of the skill should not break an older install.
    """
    known = {f.name for f in fields(Config)} - {"provenance"}
    for key, value in values.items():
        if key not in known or value is None:
            continue
        setattr(config, key, _COERCE.get(key, lambda v: v)(value))
        config.provenance[key] = layer


def _from_environment(env: env_mod.Environment) -> dict[str, Any]:
    return {
        "profile": env.profile,
        "library_root": env.library_root,
        "handoff": env.handoff.value,
    }


def _from_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _from_env_vars(environ: dict[str, str] | None = None) -> dict[str, Any]:
    environ = environ if environ is not None else dict(os.environ)
    known = {f.name for f in fields(Config)} - {"provenance"}
    values: dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX):].lower()
        if name not in known:
            continue
        values[name] = _parse_scalar(raw)
    return values


def _parse_scalar(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "1"}:
        return True
    if lowered in {"false", "no", "0"}:
        return False
    if raw.isdigit():
        return int(raw)
    if "," in raw:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return raw


def load(
    *,
    overrides: dict[str, Any] | None = None,
    environment: env_mod.Environment | None = None,
    path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Config:
    """Resolve all layers into one `Config`."""
    config = Config()
    environment = environment or env_mod.detect()

    _apply(config, _from_environment(environment), "environment")
    _apply(config, _from_file(path or config_path()), "file")
    _apply(config, _from_env_vars(environ), "env")
    _apply(config, overrides or {}, "override")

    return config


EXAMPLE_CONFIG = """\
# ~/.config/hifi-rip/config.toml
#
# Every key here is optional. Anything omitted falls back to what the skill
# detects about this machine, so a shared config stays portable.

# Force a destination profile regardless of host:
#   apple | windows | sonos | archive | opus-native
profile = "archive"

# Unlocks 256k streams on eligible YouTube Music tracks.
# Accepts: chrome, firefox, safari, brave, edge, chromium, vivaldi, opera
cookies_from_browser = "chrome"

library_root = "~/Music/hifi-rip"
naming = "{albumartist}/{album}/{track:02d} - {title}"
# Used when a recording has neither album nor track number, so standalone
# singles do not land under an invented "Unknown Album/00 - ..." path.
naming_single = "{albumartist}/{title}"

# Transcoding stacks a second lossy generation. Leave this off unless you
# have a device that genuinely cannot play AAC or Opus.
allow_transcode = false

sponsorblock = true
sponsorblock_categories = ["music_offtopic", "sponsor", "intro", "outro"]

# Sources needing API keys activate only when their key is present; the
# rest keep working without any setup.
sources = [
    "yt_chapters", "yt_description", "yt_comments", "musicbrainz",
    "discogs", "lastfm", "sponsorblock", "acoustid", "silence",
]

# Independent sources that must agree before a value is accepted without
# escalating to the agent for judgement. Raise it for stricter rips.
consensus_threshold = 2
"""
