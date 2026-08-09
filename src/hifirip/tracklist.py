"""Merging rival tracklists into one.

Field consensus asks "what is this album called"; tracklist consensus asks
"where does track four begin, and what is it", which is harder in two ways.

The values are continuous, so agreement is a matter of proximity rather than
equality. Two sources that place a boundary at 14:02 and 14:05 agree; the
question is how far apart is still agreement, and that depends on the
material. A discrete album has real silence between tracks, so sources
should converge within a couple of seconds. A DJ set has an 8-to-32 bar
crossfade during which both tracks are genuinely playing, so sources
routinely differ by fifteen seconds and are still describing the same
transition.

And the entries have to be aligned before they can be compared at all. One
source may list an intro the others omit, which shifts every subsequent
index. So boundaries are clustered by time rather than zipped by position.

The single-source rule from `consensus` carries over per track: a boundary
proposed by one source alone is a lead, not a conclusion, however good that
source is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .consensus import Claim, Resolution, Status, reconcile
from .content import ContentClass
from .sources.youtube import TrackCandidate

#: How far apart two sources may place the same boundary and still be taken
#: to agree, per content class. A crossfade is genuinely tens of seconds
#: wide; a gap between album tracks is not.
TOLERANCE: dict[ContentClass, float] = {
    ContentClass.DISCRETE_MULTITRACK: 3.0,
    ContentClass.CONTINUOUS_MIX: 15.0,
    ContentClass.LIVE_PERFORMANCE: 12.0,
    ContentClass.AMBIENT_LONGFORM: 20.0,
}
DEFAULT_TOLERANCE = 10.0


@dataclass
class TrackConsensus:
    """One agreed track: where it starts, what it is, and how sure we are."""

    start: float
    title: Resolution
    artist: Resolution | None
    supporting_sources: set[str] = field(default_factory=set)
    spread: float = 0.0

    @property
    def needs_judgement(self) -> bool:
        return (
            self.title.needs_judgement
            or len(self.supporting_sources) < 2
            or (self.artist is not None and self.artist.needs_judgement)
        )

    @property
    def display(self) -> str:
        title = self.title.value or "Unknown"
        artist = self.artist.value if self.artist else None
        return f"{artist} - {title}" if artist else title

    def describe(self) -> str:
        backing = ", ".join(sorted(self.supporting_sources))
        line = f"{self.start:8.1f}s  {self.display}  [{backing}]"
        if self.spread > 1.0:
            line += f"  (sources differ by {self.spread:.1f}s)"
        return line


@dataclass
class TracklistResult:
    tracks: list[TrackConsensus] = field(default_factory=list)
    tolerance: float = DEFAULT_TOLERANCE
    #: Sources that offered a tracklist at all.
    contributors: set[str] = field(default_factory=set)

    @property
    def unresolved(self) -> list[TrackConsensus]:
        return [t for t in self.tracks if t.needs_judgement]

    @property
    def needs_host(self) -> bool:
        return bool(self.unresolved)

    def describe(self) -> str:
        if not self.tracks:
            return "No tracklist could be assembled from any source."
        lines = [
            f"{len(self.tracks)} track(s) from {len(self.contributors)} source(s): "
            f"{', '.join(sorted(self.contributors))}",
            f"Boundary agreement tolerance: {self.tolerance:.0f}s",
        ]
        lines += [f"  {track.describe()}" for track in self.tracks]
        if self.unresolved:
            lines.append(
                f"{len(self.unresolved)} track(s) need judgement -- see brief."
            )
        return "\n".join(lines)

    def brief(self) -> str:
        """What a host agent must settle, and nothing it need not."""
        if not self.unresolved:
            return "Tracklist fully corroborated; no judgement needed."

        lines = [
            f"{len(self.unresolved)} of {len(self.tracks)} tracks could not be "
            f"settled by source agreement. Corroborated tracks are omitted; do "
            f"not revisit them.",
            "",
        ]
        for track in self.unresolved:
            lines.append(f"AT {track.start:.1f}s")
            if len(track.supporting_sources) < 2:
                only = ", ".join(track.supporting_sources) or "none"
                lines.append(
                    f"  Proposed by one source only ({only}). That is a lead, "
                    f"not a consensus, however reliable the source."
                )
            for claim in track.title.supporting + track.title.dissenting:
                lines.append(f"    title: {claim}")
            if track.artist:
                for claim in track.artist.supporting + track.artist.dissenting:
                    lines.append(f"    artist: {claim}")
            lines.append("")

        lines.append(
            "Prefer the reading with the most independent support. If a track "
            "cannot be identified, leave its title unset rather than guessing "
            "-- an untitled track is honest, and a wrong one is filed into the "
            "user's library under a name that is not theirs."
        )
        return "\n".join(lines)


def cluster(
    candidates: list[tuple[TrackCandidate, float]], tolerance: float
) -> list[list[tuple[TrackCandidate, float]]]:
    """Group boundaries that different sources place at the same transition.

    Single-linkage on time: a candidate joins the open cluster while it sits
    within `tolerance` of the previous one. Clustering rather than zipping by
    index is what tolerates a source that lists an intro the others skip,
    which would otherwise shift every later track by one.
    """
    if not candidates:
        return []

    ordered = sorted(candidates, key=lambda pair: pair[0].start)
    clusters: list[list[tuple[TrackCandidate, float]]] = [[ordered[0]]]
    for candidate, weight in ordered[1:]:
        if candidate.start - clusters[-1][-1][0].start <= tolerance:
            clusters[-1].append((candidate, weight))
        else:
            clusters.append([(candidate, weight)])
    return clusters


def merge(
    contributions: dict[str, list[TrackCandidate]],
    weights: dict[str, float],
    *,
    content_class: ContentClass = ContentClass.CONTINUOUS_MIX,
    min_sources: int = 2,
    independent_of: dict[str, set[str]] | None = None,
) -> TracklistResult:
    """Reconcile several sources' tracklists into one."""
    tolerance = TOLERANCE.get(content_class, DEFAULT_TOLERANCE)

    flattened: list[tuple[TrackCandidate, float]] = []
    for source, entries in contributions.items():
        weight = weights.get(source, 0.5)
        flattened.extend((entry, weight) for entry in entries)

    tracks: list[TrackConsensus] = []
    for group in cluster(flattened, tolerance):
        sources = {candidate.source for candidate, _ in group}

        title_claims = [
            Claim("title", candidate.title, candidate.source, weight)
            for candidate, weight in group if candidate.title
        ]
        artist_claims = [
            Claim("artist", candidate.artist, candidate.source, weight)
            for candidate, weight in group if candidate.artist
        ]

        # Weight the agreed start by source reliability: a chapter marker
        # should pull the boundary harder than an unreviewed comment.
        total = sum(weight for _, weight in group) or 1.0
        start = sum(c.start * w for c, w in group) / total
        spread = max(c.start for c, _ in group) - min(c.start for c, _ in group)

        tracks.append(TrackConsensus(
            start=round(start, 3),
            title=reconcile(title_claims, min_sources=min_sources,
                            independent_of=independent_of),
            artist=(reconcile(artist_claims, min_sources=min_sources,
                              independent_of=independent_of)
                    if artist_claims else None),
            supporting_sources=sources,
            spread=round(spread, 3),
        ))

    return TracklistResult(
        tracks=tracks,
        tolerance=tolerance,
        contributors={s for s, e in contributions.items() if e},
    )


def to_boundaries(result: TracklistResult, duration: float):
    """Convert an agreed tracklist into split boundaries.

    Imported lazily so that `split` -- which needs ffmpeg -- is not a hard
    dependency of merely resolving a tracklist.
    """
    from .split import derive_boundaries

    points = [track.start for track in result.tracks]
    titles = [track.display for track in result.tracks]
    return derive_boundaries(points, titles, duration)


def apply_track_judgement(
    result: TracklistResult, decisions: dict[int, str | None]
) -> TracklistResult:
    """Fold host rulings back in, keyed by track index."""
    for index, title in decisions.items():
        if 0 <= index < len(result.tracks):
            resolution = result.tracks[index].title
            resolution.value = title
            resolution.status = Status.AGREED if title else Status.ABSENT
            result.tracks[index].supporting_sources |= {"host"}
    return result
