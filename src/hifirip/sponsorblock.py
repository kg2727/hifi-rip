"""SponsorBlock segments: crowd-sourced markers for non-music material.

Used for two things: trimming the lead-in before music actually starts, and
supplying `music_offtopic` boundaries as one input among many to the track
resolver.

Queried through the hash-prefix endpoint rather than by video ID. The API
accepts the first four characters of the SHA-256 of the ID and returns every
video sharing that prefix, so the server never learns which one was asked
about. It costs a slightly larger response and is the endpoint SponsorBlock
itself recommends.

Everything here is crowd-sourced, and trimming discards audio permanently.
So submissions are filtered on votes, and implausible trims are refused
rather than applied: a wrong marker that removes the first thirty seconds of
a track is far more damaging than a lead-in that survives.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

API_ROOT = "https://sponsor.ajay.app/api/skipSegments"
HASH_PREFIX_LENGTH = 4

#: Categories worth requesting. `music_offtopic` marks non-music passages in
#: music uploads, which is the one most useful to this project.
DEFAULT_CATEGORIES = ("intro", "outro", "sponsor", "selfpromo", "music_offtopic")

#: Submissions below this net vote count are ignored. Zero-vote entries are
#: unreviewed, and a single bad one would silently remove real audio.
MIN_VOTES = 0

#: A lead-in longer than this is not a lead-in. Real intros run seconds; a
#: marker claiming minutes is either mis-categorised or vandalism, and acting
#: on it would delete music.
MAX_INTRO_SECONDS = 90.0

#: A trim is only a lead-in if it starts essentially at the beginning.
INTRO_START_TOLERANCE = 2.0


class SponsorBlockError(RuntimeError):
    pass


@dataclass(frozen=True)
class Segment:
    category: str
    start: float
    end: float
    votes: int = 0
    locked: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start

    def __str__(self) -> str:
        return f"{self.category} {self.start:.1f}-{self.end:.1f}s ({self.votes:+d} votes)"


def hash_prefix(video_id: str) -> str:
    return hashlib.sha256(video_id.encode()).hexdigest()[:HASH_PREFIX_LENGTH]


def fetch_segments(
    video_id: str,
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
    *,
    client: httpx.Client | None = None,
    timeout: float = 15.0,
) -> list[Segment]:
    """Fetch segments for one video, returning [] when there are none.

    Absent data is normal -- most uploads have no submissions at all -- so a
    miss is never an error. Network failures are also swallowed: SponsorBlock
    is one input among many, and a rip should not fail because a
    crowd-sourced service is down.
    """
    url = f"{API_ROOT}/{hash_prefix(video_id)}"
    params = {"categories": '["' + '","'.join(categories) + '"]'}

    try:
        owned = client is None
        http = client or httpx.Client(timeout=timeout)
        try:
            response = http.get(url, params=params)
        finally:
            if owned:
                http.close()
    except httpx.HTTPError:
        return []

    if response.status_code == 404:
        return []
    if response.status_code != 200:
        return []

    try:
        payload = response.json()
    except ValueError:
        return []

    segments: list[Segment] = []
    for entry in payload if isinstance(payload, list) else []:
        # The hash-prefix endpoint returns every video sharing the prefix.
        if entry.get("videoID") != video_id:
            continue
        for item in entry.get("segments") or []:
            bounds = item.get("segment") or []
            if len(bounds) != 2:
                continue
            try:
                start, end = float(bounds[0]), float(bounds[1])
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            segments.append(Segment(
                category=item.get("category", "unknown"),
                start=start,
                end=end,
                votes=int(item.get("votes", 0)),
                locked=bool(item.get("locked", 0)),
            ))

    return sorted(segments, key=lambda s: s.start)


def trustworthy(segments: list[Segment], min_votes: int = MIN_VOTES) -> list[Segment]:
    """Drop submissions nobody has upvoted, keeping locked ones regardless.

    Locked segments were set by SponsorBlock moderators and outrank votes.
    """
    return [s for s in segments if s.locked or s.votes >= min_votes]


def lead_in(
    segments: list[Segment],
    *,
    max_seconds: float = MAX_INTRO_SECONDS,
) -> Segment | None:
    """The segment covering silence or non-music before the music starts.

    Returns None unless a marker genuinely looks like a lead-in: it must
    begin at the very start, be categorised as intro or off-topic, and be
    short enough to be plausible. Every other case leaves the audio alone,
    because a false positive here deletes the opening of a track and the
    user has no way to notice beyond it sounding wrong.
    """
    for segment in segments:
        if segment.start > INTRO_START_TOLERANCE:
            continue
        if segment.category not in ("intro", "music_offtopic"):
            continue
        if segment.duration > max_seconds:
            continue
        return segment
    return None


def non_music(segments: list[Segment]) -> list[Segment]:
    """Passages marked as not being music, for the track resolver."""
    return [s for s in segments if s.category in ("music_offtopic", "sponsor", "selfpromo")]


def describe(segments: list[Segment]) -> str:
    if not segments:
        return "No SponsorBlock segments submitted for this video."
    lines = [f"{len(segments)} SponsorBlock segment(s):"]
    lines += [f"  {segment}" for segment in segments]
    return "\n".join(lines)
