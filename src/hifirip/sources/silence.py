"""Boundary detection from the audio itself.

The only source here that needs neither network nor credentials, and the only
one that observes the recording rather than reporting what someone wrote
about it. That makes it a useful independent check on tracklists that are
otherwise entirely testimonial.

It applies to discrete uploads only. A beatmatched mix contains no silence at
all, so running this on one yields nothing and, worse, an empty result from a
source that "applies" can look like evidence of no boundaries rather than
evidence of nothing. The registry therefore routes it to
DISCRETE_MULTITRACK alone.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Level below which audio counts as silence. Digital black is rare on real
#: recordings -- dither, room tone and encoder noise all sit above it -- so
#: the threshold is set where quiet actually lands.
DEFAULT_THRESHOLD_DB = -50.0

#: Shorter gaps are musical rests, breaths, or the tail of a fade, not track
#: boundaries. Album gaps are conventionally around two seconds.
DEFAULT_MIN_SILENCE = 1.2

SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass(frozen=True)
class Gap:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        """Where a cut should land inside this gap.

        The middle, so each neighbouring track keeps a symmetric tail of
        room tone rather than starting abruptly on its first sample.
        """
        return (self.start + self.end) / 2


def detect_gaps(
    path: Path,
    *,
    threshold_db: float = DEFAULT_THRESHOLD_DB,
    min_silence: float = DEFAULT_MIN_SILENCE,
    timeout: int = 3600,
) -> list[Gap]:
    """Find silent passages, decoding once and writing nothing."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not path.exists():
        return []

    result = subprocess.run(
        [
            ffmpeg, "-v", "info", "-nostats", "-i", str(path),
            "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    # silencedetect reports on stderr even when it succeeds.
    text = result.stderr or ""
    starts = [float(m) for m in SILENCE_START.findall(text)]
    ends = [float(m) for m in SILENCE_END.findall(text)]

    gaps: list[Gap] = []
    for index, start in enumerate(starts):
        if index < len(ends):
            end = ends[index]
            if end > start:
                gaps.append(Gap(start, end))
    return gaps


def boundaries_from_gaps(
    gaps: list[Gap], duration: float, *, lead_in_limit: float = 30.0
) -> list[float]:
    """Convert gaps into track start times.

    A gap right at the beginning is lead-in, not a boundary -- treating it as
    one would make an empty first track. Track one always starts at zero.
    """
    starts = [0.0]
    for gap in gaps:
        if gap.midpoint <= lead_in_limit:
            continue
        if gap.midpoint >= duration - lead_in_limit:
            continue
        starts.append(round(gap.midpoint, 3))
    return starts


def describe(gaps: list[Gap]) -> str:
    if not gaps:
        return (
            "No silence found. Expected for continuously mixed material; for a "
            "discrete album it suggests the tracks are crossfaded or the "
            "threshold is too strict."
        )
    return "\n".join(
        [f"{len(gaps)} silent gap(s):"]
        + [f"  {g.start:8.1f}-{g.end:8.1f}s ({g.duration:.1f}s)" for g in gaps]
    )
