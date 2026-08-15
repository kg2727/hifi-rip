"""Tracklists carried in the upload itself: chapters, description, comments.

These need no credentials and no network beyond the probe already performed,
which makes them the backbone of a keyless install.

Their reliability differs sharply and the weights in the registry reflect it.
Chapters are machine-readable and stated by the uploader. Descriptions are
free text written by the same person, so they are authoritative in content
and chaotic in format. Comments are written by strangers -- and for popular
sets are frequently the only timings that exist anywhere, often strikingly
accurate, which is why they are included at all despite being unverified.

The parsing problem is that a timestamp-shaped string is not a tracklist
entry. Descriptions are full of "check out my mix at 2:30" and social links,
and a parser that accepts every `\\d+:\\d+` produces confident nonsense that
then carries weight in a vote. So entries must look like a *list*: several of
them, at line starts, in ascending order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A timestamp at the start of a line, optionally bracketed or numbered.
#: Anchoring to line start is what separates a tracklist from prose that
#: happens to mention a time.
ENTRY = re.compile(
    r"""^[\s\-\*•]*                 # bullets or dashes
        (?:\d{1,3}[\.\)]\s*)?            # optional "12." track number
        [\[\(]?                          # optional bracket
        (?P<h>\d{1,2}:)?(?P<m>\d{1,2}):(?P<s>\d{2})
        [\]\)]?
        \s*[\-–—\.\)\]]?\s*     # separator before the title
        (?P<title>.+?)\s*$
    """,
    re.VERBOSE | re.MULTILINE,
)

#: A tracklist needs to look like a list. One or two matches in a long
#: description are far more likely to be prose than a tracklist.
MIN_ENTRIES = 3

#: Artist/title separators, in order of how unambiguous they are.
SPLITTERS = (" – ", " — ", " - ", " -- ", " | ")

#: Lines that are structurally timestamps but semantically noise.
NOT_A_TRACK = re.compile(
    r"^(intro|outro|tracklist|track ?list|subscribe|follow|download|"
    r"buy|stream|listen|support|links?|social|instagram|twitter|facebook|"
    r"spotify|soundcloud|merch|patreon)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TrackCandidate:
    """One proposed track start."""

    start: float
    title: str
    artist: str | None = None
    source: str = ""

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title

    def __str__(self) -> str:
        return f"{self.start:8.1f}s  {self.display}"


def parse_timestamp(hours: str | None, minutes: str, seconds: str) -> float:
    total = int(minutes) * 60 + int(seconds)
    if hours:
        total += int(hours.rstrip(":")) * 3600
    return float(total)


def split_artist_title(text: str) -> tuple[str | None, str]:
    """Separate "Artist - Title" where the separator is unambiguous.

    Only the first separator is used, so remixes keep their suffix intact:
    "A - B (C Remix)" yields artist "A" and title "B (C Remix)", which is
    what release databases will be matched against.
    """
    for splitter in SPLITTERS:
        if splitter in text:
            artist, _, title = text.partition(splitter)
            artist, title = artist.strip(), title.strip()
            if artist and title:
                return artist, title
    return None, text.strip()


def parse_entries(text: str, source: str, *, duration: float | None = None) -> list[TrackCandidate]:
    """Extract a tracklist from free text, or return [] if it is not one."""
    candidates: list[TrackCandidate] = []
    for match in ENTRY.finditer(text or ""):
        title = match.group("title").strip(" -–—.")
        if not title or NOT_A_TRACK.match(title):
            continue
        start = parse_timestamp(match.group("h"), match.group("m"), match.group("s"))
        if duration and start > duration:
            continue
        artist, title = split_artist_title(title)
        candidates.append(TrackCandidate(start, title, artist, source))

    if len(candidates) < MIN_ENTRIES:
        return []

    # A real tracklist ascends. Text that merely contains several times --
    # a comment thread quoting different moments, say -- generally does not.
    ascending = sorted(candidates, key=lambda c: c.start)
    if [c.start for c in candidates] != [c.start for c in ascending]:
        return []

    deduped: list[TrackCandidate] = []
    for candidate in ascending:
        if deduped and candidate.start == deduped[-1].start:
            continue
        deduped.append(candidate)
    return deduped


def from_chapters(info: dict) -> list[TrackCandidate]:
    """YouTube's own chapter markers: unambiguous when present."""
    candidates = []
    for chapter in info.get("chapters") or []:
        title = (chapter.get("title") or "").strip()
        if not title:
            continue
        artist, title = split_artist_title(title)
        candidates.append(TrackCandidate(
            start=float(chapter.get("start_time") or 0.0),
            title=title, artist=artist, source="yt_chapters",
        ))
    return candidates


def from_description(info: dict) -> list[TrackCandidate]:
    return parse_entries(
        info.get("description") or "", "yt_description",
        duration=info.get("duration"),
    )


def from_comments(info: dict, *, limit: int = 200) -> list[TrackCandidate]:
    """The best-supported tracklist among the comments, if any.

    Comments are scanned rather than merged: two commenters posting rival
    tracklists are rival claims, not one combined claim, and blending them
    would invent a tracklist neither person wrote. The most-upvoted comment
    that parses as a list wins, since votes are the only review signal here.
    """
    comments = info.get("comments") or []
    duration = info.get("duration")

    best: list[TrackCandidate] = []
    best_votes = -1
    for comment in comments[:limit]:
        text = comment.get("text") or ""
        parsed = parse_entries(text, "yt_comments", duration=duration)
        if not parsed:
            continue
        votes = int(comment.get("like_count") or 0)
        # Prefer upvotes; break ties on the more detailed tracklist.
        if (votes, len(parsed)) > (best_votes, len(best)):
            best, best_votes = parsed, votes
    return best


def collect(info: dict) -> dict[str, list[TrackCandidate]]:
    """Every in-upload tracklist source, keyed by source name."""
    found = {
        "yt_chapters": from_chapters(info),
        "yt_description": from_description(info),
        "yt_comments": from_comments(info),
    }
    return {name: entries for name, entries in found.items() if entries}
