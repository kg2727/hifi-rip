"""Detection of which YouTube audio tier a probe actually reached.

Free accounts top out at Opus ~160k (itag 251) and AAC 128k (itag 140).
YouTube Music / Premium credentials additionally surface AAC 256k (141)
and Opus ~256k (774) *on eligible content*.

That last qualifier is the whole reason this module exists. 256k streams
are gated on both the account and the content: an official YouTube Music
track carries them, an ordinary uploaded music video usually does not,
even for an entitled account. So "Premium configured but no 256k stream"
is ambiguous between two very different situations:

  1. the content simply isn't eligible -- entirely normal, nothing wrong;
  2. the cookies expired and the probe silently fell back to free tier.

Case 2 is the dangerous one. Browser cookies expire constantly, and a
silent downgrade means the user keeps ripping at 128k while believing
they are getting 256k -- a library that is quietly worse than they think,
with no error anywhere. Guessing between the two cases would be worse
than saying nothing, so we never guess: cookie acceptance is established
from yt-dlp's own diagnostics, independently of which streams appeared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from .formats import PREMIUM_ABR_THRESHOLD, AudioStream


class Tier(Enum):
    FREE = "free"
    PREMIUM = "premium"


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.value.upper()}] {self.message}"


#: yt-dlp's own wording when supplied cookies are missing, unreadable, or
#: rejected. Matched case-insensitively against captured stderr.
COOKIE_FAILURE_PATTERNS: tuple[str, ...] = (
    r"could not find .* cookies",
    r"could not copy .* cookies",
    r"no cookies found",
    r"cookies are no longer valid",
    r"failed to load cookies",
    r"unable to load cookies",
    r"sign in to confirm",
    r"please sign in",
    r"account cookies are invalid",
)

_COOKIE_FAILURE_RE = re.compile("|".join(COOKIE_FAILURE_PATTERNS), re.IGNORECASE)


@dataclass
class Entitlement:
    """What tier this probe reached, and how confident we are about why."""

    tier: Tier
    cookies_configured: bool
    #: True/False once established from yt-dlp diagnostics; None when no
    #: cookies were configured, so the question does not apply.
    cookies_accepted: bool | None
    premium_streams: tuple[AudioStream, ...] = ()
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def silently_downgraded(self) -> bool:
        """Configured for Premium, but the probe ran as an anonymous one."""
        return self.cookies_configured and self.cookies_accepted is False

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity is Severity.WARNING]

    def summary(self) -> str:
        lines = [f"Tier reached: {self.tier.value}"]
        if self.cookies_configured:
            state = {True: "accepted", False: "REJECTED", None: "unknown"}[
                self.cookies_accepted
            ]
            lines.append(f"Credentials: configured, {state}")
        else:
            lines.append("Credentials: none configured (anonymous probe)")
        if self.premium_streams:
            found = ", ".join(str(s) for s in self.premium_streams)
            lines.append(f"Premium-tier streams offered: {found}")
        lines.extend(str(d) for d in self.diagnostics)
        return "\n".join(lines)


def cookies_were_accepted(stderr: str) -> bool:
    """Whether a cookie-bearing probe authenticated successfully.

    Deliberately independent of which streams came back -- that is exactly
    the conflation this module exists to avoid.
    """
    return not _COOKIE_FAILURE_RE.search(stderr or "")


def assess(
    streams: Sequence[AudioStream],
    *,
    cookies_configured: bool,
    stderr: str = "",
) -> Entitlement:
    """Classify the tier a probe reached and explain any shortfall."""
    premium = tuple(s for s in streams if s.is_premium_tier)
    accepted = cookies_were_accepted(stderr) if cookies_configured else None
    tier = Tier.PREMIUM if premium else Tier.FREE
    diagnostics: list[Diagnostic] = []

    if cookies_configured and accepted is False:
        diagnostics.append(
            Diagnostic(
                Severity.WARNING,
                "Credentials were configured but YouTube rejected them, so this "
                "probe ran anonymously and saw only free-tier streams. Any rip "
                "made now is capped at Opus ~160k / AAC 128k despite your "
                "Premium account. Refresh your browser login (or re-export "
                "cookies) and run again -- this is a silent quality downgrade, "
                "not a hard failure, so nothing else would have told you.",
            )
        )
    elif cookies_configured and not premium:
        diagnostics.append(
            Diagnostic(
                Severity.INFO,
                "Credentials were accepted, but this video offers no 256k "
                "stream. That is normal: 256k is gated on the content as well "
                "as the account, and only official YouTube Music tracks carry "
                "it. Ordinary music-video uploads top out at the free ceiling "
                "for everyone. Run `hifi-rip doctor <known-music-url>` to "
                "confirm your credentials independently of this video.",
            )
        )
    elif not cookies_configured:
        diagnostics.append(
            Diagnostic(
                Severity.INFO,
                "No credentials configured, so 256k streams were never on "
                "offer. If you have YouTube Premium or YouTube Music, set "
                "`cookies_from_browser` to roughly double the bitrate on "
                "eligible tracks.",
            )
        )

    return Entitlement(
        tier=tier,
        cookies_configured=cookies_configured,
        cookies_accepted=accepted,
        premium_streams=premium,
        diagnostics=diagnostics,
    )


# Deliberately absent: a "distance below the nominal ceiling" metric.
#
# Opus is VBR, so itag 251's advertised ~160k is a target, not a floor --
# a well-encoded track routinely measures 125-140k, and itag 140's "128k"
# regularly reports as 130k. Comparing measured bitrate against nominal
# figures would therefore fire a quality warning on almost every rip, and
# a warning that fires constantly is one users learn to ignore. The only
# shortfall worth reporting is the one this module actually establishes:
# credentials that were configured and then rejected.
