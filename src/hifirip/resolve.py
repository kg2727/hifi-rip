"""The single explicit decision: environment x entitlement -> stream.

Both axes are load-bearing and they interact, so the resolution is written
out as one table rather than left to fall out of ranking logic:

                     free tier                 Premium / YT Music
    Apple            AAC 128k  -> .m4a         AAC 256k  -> .m4a
    Windows          AAC 128k  -> .m4a         AAC 256k  -> .m4a
    Archive/Linux    Opus 160k -> .opus        Opus 256k -> .opus

The environment axis picks the codec *family* (Music.app cannot decode
Opus, so an Apple destination pins AAC no matter what bitrate Opus is
offering). The entitlement axis picks the *bitrate* within that family.
Neither ever triggers a transcode: both cells of a row are separate source
streams that YouTube encoded independently from the same master.

`Decision.trace()` renders the whole chain so a user can see not just what
was chosen but which alternatives were rejected and why -- without that,
"highest quality available" is an unverifiable claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import environment as env_mod
from .entitlement import Entitlement, Tier, assess
from .formats import PROFILES, NoUsableStream, Profile, Selection, select
from .probe import ProbeResult


@dataclass
class Decision:
    selection: Selection
    entitlement: Entitlement
    environment: env_mod.Environment
    profile_source: str  # "user" | "environment"

    @property
    def stream(self):
        return self.selection.stream

    @property
    def left_on_table(self) -> float:
        """kbps available on this video that the profile could not use.

        Measured against what YouTube actually offered, never against a
        nominal itag figure -- see the note at the foot of `entitlement`.
        Non-zero only when compatibility genuinely cost bitrate.
        """
        usable = [
            s.abr or 0.0
            for s in self.selection.considered
            if s.family in self.selection.profile.families
        ]
        return max(usable, default=0.0) - (self.stream.abr or 0.0)

    def trace(self) -> str:
        parts = [
            "=== environment ===",
            self.environment.describe(),
            f"Profile '{self.selection.profile.name}' chosen by: {self.profile_source}",
            "",
            "=== entitlement ===",
            self.entitlement.summary(),
            "",
            "=== stream ===",
            self.selection.explain(),
        ]
        if self.entitlement.silently_downgraded:
            parts += [
                "",
                "!! This rip is NOT the best your account can reach. Fix the "
                "credentials above and re-run before keeping these files.",
            ]
        return "\n".join(parts)


def resolve(
    result: ProbeResult,
    *,
    profile: str | Profile | None = None,
    environment: env_mod.Environment | None = None,
    language: str | None = None,
) -> Decision:
    """Combine host environment and account tier into one stream choice.

    An explicit `profile` always wins over the detected environment -- that
    is the primary steering knob, and a user who asks for Opus on a Mac
    gets Opus even though Music.app cannot play it.
    """
    environment = environment or env_mod.detect()

    if profile is None:
        chosen, source = environment.profile, "environment"
    else:
        chosen = profile if isinstance(profile, str) else profile.name
        source = "user"

    entitlement = assess(
        result.streams,
        cookies_configured=result.cookies_configured,
        stderr=result.stderr,
    )

    selection = select(result.streams, chosen, language=language)

    return Decision(
        selection=selection,
        entitlement=entitlement,
        environment=environment,
        profile_source=source,
    )


def resolution_matrix() -> str:
    """The decision table, rendered for docs and `hifi-rip explain`."""
    rows = [
        ("apple", "AAC 128k -> .m4a", "AAC 256k -> .m4a"),
        ("windows", "AAC 128k -> .m4a", "AAC 256k -> .m4a"),
        ("archive", "Opus ~160k -> .opus", "Opus ~256k -> .opus"),
        ("sonos", "AAC 128k -> .m4a", "AAC 256k -> .m4a"),
        ("opus-native", "Opus ~160k -> .opus", "Opus ~256k -> .opus"),
    ]
    width = max(len(name) for name, _, _ in rows)
    lines = [f"{'profile'.ljust(width)}  {'free':<22}{'premium'}"]
    lines += [f"{name.ljust(width)}  {free:<22}{prem}" for name, free, prem in rows]
    lines.append("")
    lines.append(
        "Every cell is a distinct source stream, remuxed with `-c copy`. "
        "No cell is produced by converting another."
    )
    return "\n".join(lines)


__all__ = [
    "Decision",
    "Entitlement",
    "NoUsableStream",
    "PROFILES",
    "Tier",
    "resolution_matrix",
    "resolve",
]
