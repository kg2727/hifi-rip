"""Content classification -- what kind of thing are we actually ripping?

A three-hour DJ set, a full-album upload, and a four-minute music video are
three different problems, and treating them alike produces confidently wrong
results. The class determines which metadata sources are worth consulting,
whether splitting makes sense at all, and how the result can be evaluated.

Concretely, on a continuously-mixed set:

  * silence detection finds nothing, because a beatmatched mix has none;
  * MusicBrainz and Discogs release durations are irrelevant, because tracks
    are mixed in late, cut out early, looped, or extended, so their studio
    lengths bear no relation to how long they appear;
  * boundaries are *intervals*, not points -- during an 8-to-32-bar crossfade
    both tracks are genuinely audible, so there is no single correct split.

None of that is true of a discrete album upload, where silence between tracks
is real and canonical release durations line up almost exactly.

`Classification.briefing()` exists because the user asked to be consulted on
splitting with an explanation grounded in their actual rip rather than a
generic warning. It reports what was found in *this* video and what follows
from it, so the choice is an informed one.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContentClass(Enum):
    SINGLE_TRACK = "single-track"
    DISCRETE_ALBUM = "discrete-album"
    CONTINUOUS_MIX = "continuous-mix"
    UNKNOWN = "unknown"


#: Titles of continuously-mixed material. DJ sets, radio shows, live sets.
MIX_PATTERNS = re.compile(
    r"\b(dj[\s-]?set|live\s?set|liveset|b2b|boiler\s?room|essential\s?mix"
    r"|mixtape|mixed\s+by|continuous\s+mix|live\s+(?:from|at|in)\b"
    r"|set\s*@|@\s*\w+\s+festival|tomorrowland|ultra\s+music)\b",
    re.IGNORECASE,
)

#: Titles of discrete multi-track uploads, where tracks are separated.
ALBUM_PATTERNS = re.compile(
    r"\b(full\s+album|complete\s+album|album\s+stream|\(\s*album\s*\)"
    r"|greatest\s+hits|anthology|discography)\b",
    re.IGNORECASE,
)

#: Above this, a music upload is almost certainly multi-track.
MULTITRACK_MINUTES = 25.0
#: Typical song length band, used to judge whether chapters look like tracks.
TRACK_SECONDS = (90.0, 480.0)


@dataclass
class Signal:
    """One piece of evidence, and what it argues for."""

    name: str
    detail: str                       # human phrasing, reused in briefings
    supports: ContentClass | None
    weight: float

    def __str__(self) -> str:
        return self.detail


@dataclass
class Classification:
    content_class: ContentClass
    confidence: float
    signals: list[Signal] = field(default_factory=list)
    duration: float = 0.0
    chapter_count: int = 0
    description_timestamps: int = 0

    @property
    def is_multitrack(self) -> bool:
        return self.content_class in (
            ContentClass.DISCRETE_ALBUM,
            ContentClass.CONTINUOUS_MIX,
        )

    @property
    def has_deterministic_boundaries(self) -> bool:
        """Whether boundaries are already stated rather than inferred."""
        return self.chapter_count > 1 or self.description_timestamps > 1

    def supporting(self) -> list[Signal]:
        return [s for s in self.signals if s.supports is self.content_class]

    def briefing(self) -> str:
        """A splitting explanation grounded in this specific video.

        Deliberately not generic: it names what was and was not found here,
        so the user is choosing between outcomes rather than agreeing to a
        warning they have read a hundred times.
        """
        minutes = self.duration / 60
        lines: list[str] = []

        if self.content_class is ContentClass.CONTINUOUS_MIX:
            lines.append(
                f"This is a {minutes:.0f}-minute continuously-mixed set, not a "
                f"collection of separate tracks."
            )
            found = []
            if not self.chapter_count:
                found.append("no chapter markers")
            if not self.description_timestamps:
                found.append("no timestamps in the description")
            if found:
                lines.append(
                    f"It has {' and '.join(found)}, so any track boundaries "
                    f"would come from community tracklists and acoustic "
                    f"fingerprinting rather than from anything the uploader "
                    f"stated."
                )
            lines.append(
                "Because the tracks are beatmatched into each other, the "
                "boundaries are ranges rather than points -- across a "
                "transition both tracks are genuinely playing at once. "
                "Splitting means each file starts and ends with its neighbour "
                "audible; that is not an error, it is what the audio contains."
            )
            lines.append(
                "Keeping it as one file with embedded chapter markers is "
                "skippable in any modern player, preserves the transitions the "
                "artist built, and cannot cut in the wrong place. Splitting "
                "gives you real per-track files for shuffle and library use, "
                "and concatenating them reproduces the original mix exactly, "
                "since nothing is re-encoded and no silence is inserted."
            )

        elif self.content_class is ContentClass.DISCRETE_ALBUM:
            lines.append(
                f"This is a {minutes:.0f}-minute upload of what looks like a "
                f"discrete multi-track release."
            )
            if self.chapter_count > 1:
                lines.append(
                    f"The uploader supplied {self.chapter_count} chapter "
                    f"markers, so boundaries are stated rather than guessed."
                )
            elif self.description_timestamps > 1:
                lines.append(
                    f"The description carries {self.description_timestamps} "
                    f"timestamps, which give boundaries directly."
                )
            else:
                lines.append(
                    "Neither chapters nor description timestamps are present, "
                    "so boundaries will be reconciled across release databases "
                    "and silence detection."
                )
            lines.append(
                "Tracks here are separated rather than mixed, so splitting is "
                "clean: cuts land in the gaps and each file contains one track "
                "and nothing else."
            )

        elif self.content_class is ContentClass.SINGLE_TRACK:
            lines.append(
                f"This is a single {minutes:.1f}-minute track. No splitting "
                f"applies; only metadata and cover art need resolving."
            )
        else:
            lines.append(
                f"Could not confidently classify this {minutes:.0f}-minute "
                f"upload. Evidence was mixed or absent, so splitting decisions "
                f"should be confirmed rather than assumed."
            )

        if self.supporting():
            reasons = "; ".join(s.detail for s in self.supporting())
            lines.append(f"(Classified from: {reasons}.)")
        return "\n".join(lines)


def classify(info: dict[str, Any]) -> Classification:
    """Classify a video from its yt-dlp metadata alone.

    Audio is never fetched here. Classification has to run *before* the
    download so it can inform which sources to consult and what to ask the
    user, and a three-hour download is far too expensive to spend on a
    question that title, duration, and chapters usually answer.
    """
    title = info.get("title") or ""
    duration = float(info.get("duration") or 0.0)
    chapters = info.get("chapters") or []
    description = info.get("description") or ""
    timestamps = count_timestamps(description)
    minutes = duration / 60

    signals: list[Signal] = []
    scores = {c: 0.0 for c in ContentClass if c is not ContentClass.UNKNOWN}

    def add(name: str, detail: str, supports: ContentClass | None, weight: float):
        signals.append(Signal(name, detail, supports, weight))
        if supports is not None:
            scores[supports] += weight

    # -- Title ------------------------------------------------------------
    if MIX_PATTERNS.search(title):
        add("title", "the title describes a live or mixed set",
            ContentClass.CONTINUOUS_MIX, 3.0)
    if ALBUM_PATTERNS.search(title):
        add("title", "the title describes a full-album upload",
            ContentClass.DISCRETE_ALBUM, 3.0)

    # -- Duration ---------------------------------------------------------
    if duration and minutes < MULTITRACK_MINUTES:
        add("duration", f"it runs {minutes:.1f} minutes, a single-track length",
            ContentClass.SINGLE_TRACK, 2.5)
    elif minutes >= 90:
        # Very long uploads are far more often sets than album reissues.
        add("duration", f"it runs {minutes:.0f} minutes, far beyond album length",
            ContentClass.CONTINUOUS_MIX, 1.5)
    elif duration:
        add("duration", f"it runs {minutes:.0f} minutes, album length",
            ContentClass.DISCRETE_ALBUM, 1.0)

    # -- Chapters ---------------------------------------------------------
    if len(chapters) > 1:
        lengths = [
            float(c.get("end_time", 0)) - float(c.get("start_time", 0))
            for c in chapters
        ]
        lengths = [x for x in lengths if x > 0]
        median = statistics.median(lengths) if lengths else 0.0
        if TRACK_SECONDS[0] <= median <= TRACK_SECONDS[1]:
            add(
                "chapters",
                f"{len(chapters)} chapters averaging {median / 60:.1f} minutes, "
                f"consistent with individual tracks",
                ContentClass.DISCRETE_ALBUM,
                2.5,
            )
        else:
            add("chapters", f"{len(chapters)} chapters of atypical length",
                None, 0.0)
    elif duration and minutes >= MULTITRACK_MINUTES:
        add(
            "chapters",
            "no chapter markers despite a multi-track runtime, which is "
            "typical of sets and untended uploads alike",
            None,
            0.0,
        )

    # -- Description timestamps -------------------------------------------
    if timestamps > 1:
        add(
            "description",
            f"{timestamps} timestamps in the description",
            ContentClass.DISCRETE_ALBUM if timestamps < 40 else ContentClass.CONTINUOUS_MIX,
            1.5,
        )

    # -- Music metadata ---------------------------------------------------
    if info.get("track") or info.get("album"):
        add("metadata", "YouTube supplied track/album metadata, which it "
            "attaches to identified single recordings",
            ContentClass.SINGLE_TRACK, 2.0)

    best = max(scores, key=lambda c: scores[c])
    total = sum(scores.values())
    if total <= 0 or scores[best] <= 0:
        return Classification(
            ContentClass.UNKNOWN, 0.0, signals, duration, len(chapters), timestamps
        )

    confidence = _confidence(scores[best], total)
    if confidence < UNCERTAIN_BELOW:
        # Reporting a weakly-evidenced guess as a finding is worse than
        # admitting we don't know: downstream routing would silently pick
        # sources and a splitting policy on the strength of, say, runtime
        # alone. Signals are retained so the briefing can still say why.
        best = ContentClass.UNKNOWN

    return Classification(
        content_class=best,
        confidence=confidence,
        signals=signals,
        duration=duration,
        chapter_count=len(chapters),
        description_timestamps=timestamps,
    )


#: Evidence weight at which a classification is considered well-supported.
#: Roughly: one strong signal plus one corroborating one.
CONFIDENT_MASS = 4.5

#: Below this, we decline to name a class at all. A single weak signal --
#: typically runtime with nothing to corroborate it -- lands here.
UNCERTAIN_BELOW = 0.35


def _confidence(best: float, total: float) -> float:
    """Combine share of evidence with how much evidence there was.

    Share alone is badly calibrated: a video whose only signal is "runs 3.5
    minutes" scores an unopposed 1.0, indistinguishable from one backed by
    a matching title, chapter structure, and release metadata. Since a low
    confidence is what prompts us to ask the user rather than assume, that
    overconfidence would silently suppress exactly the questions they asked
    to be asked.
    """
    share = best / total
    saturation = min(1.0, best / CONFIDENT_MASS)
    return round(share * saturation, 3)


_TIMESTAMP_LINE = re.compile(r"(?m)^\s*\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?")


def count_timestamps(description: str) -> int:
    """Count line-leading timestamps, the shape a tracklist actually takes."""
    return len(_TIMESTAMP_LINE.findall(description or ""))
