"""Orchestration: fan out to every competent source, then reconcile.

Sources are queried concurrently because they are almost entirely latency:
MusicBrainz is a throttled HTTP call, silence detection is an ffmpeg decode.
Run in sequence they add up; run together the slowest one sets the pace.

Threads rather than asyncio, deliberately. The work is blocking subprocesses
and blocking HTTP, so there is nothing for an event loop to interleave that a
small pool does not already handle, and threads keep the source modules
ordinary synchronous code that is straightforward to test.

Failures are contained per source. A source that errors, times out, or
returns nothing is recorded as absent and the rest proceed -- the whole point
of weighing many sources is that none of them is required.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .consensus import Claim, ConflictReport, reconcile_all, strip_noise
from .content import Classification, ContentClass
from .sources import REGISTRY, derivation_map, sources_for, unavailable_for
from .sources import musicbrainz as mb
from .sources import silence as silence_mod
from .sources import youtube as yt
from .tracklist import TracklistResult, merge

#: Sources that need credentials, mapped to the variable they look for.
KEY_VARIABLES = {
    spec.requires_key for spec in REGISTRY.values() if spec.requires_key
}

MAX_WORKERS = 6
SOURCE_TIMEOUT = 60


def available_keys(environ: dict[str, str] | None = None) -> frozenset[str]:
    env = environ if environ is not None else dict(os.environ)
    return frozenset(name for name in KEY_VARIABLES if env.get(name))


@dataclass
class Resolution:
    """Everything the sources collectively established."""

    metadata: ConflictReport
    tracklist: TracklistResult | None = None
    consulted: list[str] = field(default_factory=list)
    silent: list[str] = field(default_factory=list)
    inactive: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def needs_host(self) -> bool:
        return self.metadata.needs_host or bool(
            self.tracklist and self.tracklist.needs_host
        )

    def describe(self) -> str:
        lines = [
            f"Consulted {len(self.consulted)} source(s) in {self.elapsed:.1f}s: "
            f"{', '.join(self.consulted) or 'none'}",
        ]
        if self.silent:
            lines.append(f"  no data from: {', '.join(self.silent)}")
        if self.inactive:
            lines.append(
                f"  inactive for want of credentials: {', '.join(self.inactive)}"
            )
        for resolution in self.metadata.resolutions:
            lines.append(f"  {resolution.describe()}")
        if self.tracklist:
            lines.append(self.tracklist.describe())
        return "\n".join(lines)

    def brief(self) -> str:
        """The combined adjudication request for a host agent."""
        parts = []
        if self.metadata.needs_host:
            parts.append("== METADATA ==\n" + self.metadata.brief())
        if self.tracklist and self.tracklist.needs_host:
            parts.append("== TRACKLIST ==\n" + self.tracklist.brief())
        return "\n\n".join(parts) or "Nothing requires judgement."


def _seed_from_youtube(info: dict) -> tuple[str, str | None]:
    """The title and artist to search other sources with.

    YouTube's own `track`/`artist` fields are used when present -- rare, but
    reliable when they appear -- and otherwise the video title is split on a
    dash, which is how uploaders overwhelmingly format music titles.
    """
    if info.get("track"):
        return info["track"], info.get("artist") or info.get("uploader")
    artist, title = yt.split_artist_title(info.get("title") or "")
    # Upload furniture has to come off before this is used as a search term:
    # no release database holds a recording called "Song (Official Video)".
    return strip_noise(title), (strip_noise(artist) if artist else info.get("artist"))


def resolve(
    info: dict,
    classification: Classification,
    *,
    audio_path: Path | None = None,
    enabled: tuple[str, ...] | None = None,
    environ: dict[str, str] | None = None,
    min_sources: int = 2,
) -> Resolution:
    """Query every competent source and reconcile what comes back."""
    started = time.monotonic()
    keys = available_keys(environ)
    content_class = classification.content_class

    active = sources_for(content_class, available_keys=keys, enabled=enabled)
    active_names = {spec.name for spec in active}
    weights = {spec.name: spec.weight for spec in REGISTRY.values()}

    claims: list[Claim] = []
    contributions: dict[str, list[yt.TrackCandidate]] = {}
    consulted: list[str] = []

    # In-upload sources are already in hand from the probe; no request needed.
    for name, entries in yt.collect(info).items():
        if name in active_names:
            contributions[name] = entries
            consulted.append(name)

    jobs = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        if "musicbrainz" in active_names:
            title, artist = _seed_from_youtube(info)
            jobs[pool.submit(
                mb.claims_for, title, artist, weight=weights["musicbrainz"]
            )] = "musicbrainz"

        if "silence" in active_names and audio_path and audio_path.exists():
            jobs[pool.submit(silence_mod.detect_gaps, audio_path)] = "silence"

        for future in as_completed(jobs, timeout=SOURCE_TIMEOUT * 2):
            name = jobs[future]
            try:
                outcome = future.result(timeout=SOURCE_TIMEOUT)
            except Exception:
                # A source that fails is absent, never fatal.
                continue
            if not outcome:
                continue
            consulted.append(name)
            if name == "silence":
                duration = float(info.get("duration") or 0.0)
                starts = silence_mod.boundaries_from_gaps(outcome, duration)
                contributions[name] = [
                    yt.TrackCandidate(start, f"Track {index + 1}", None, name)
                    for index, start in enumerate(starts)
                ]
            else:
                claims.extend(outcome)

    # YouTube's own metadata votes at the description's weight: it is
    # uploader-written text and carries the same risks.
    #
    # What the video title *means* depends on the content class. For a single
    # recording it names the track, so splitting it on a dash yields artist
    # and title. For a mix or a concert it names the whole set -- splitting
    # that produces nonsense, as a real DJ set demonstrated by yielding an
    # artist of "Festival Madness! EDM, Trance & Techno DJ Mix 2026". There
    # the title belongs on the album field and nowhere else.
    if classification.is_multitrack:
        album = strip_noise(info.get("title") or "")
        if album:
            claims.append(Claim("album", album, "yt_description",
                                weights["yt_description"], "video title"))
    else:
        title, artist = _seed_from_youtube(info)
        if title:
            claims.append(Claim("title", title, "yt_description",
                                weights["yt_description"], "parsed from video title"))
        if artist:
            claims.append(Claim("artist", artist, "yt_description",
                                weights["yt_description"], "parsed from video title"))
    if "yt_description" not in consulted:
        consulted.append("yt_description")

    derived = derivation_map()
    tracklist = None
    if classification.is_multitrack and contributions:
        tracklist = merge(
            contributions, weights, content_class=content_class,
            min_sources=min_sources, independent_of=derived,
        )

    return Resolution(
        metadata=reconcile_all(claims, min_sources=min_sources, independent_of=derived),
        tracklist=tracklist,
        consulted=sorted(set(consulted)),
        silent=sorted(active_names - set(consulted)),
        inactive=[s.name for s in unavailable_for(content_class, available_keys=keys)],
        elapsed=round(time.monotonic() - started, 2),
    )
