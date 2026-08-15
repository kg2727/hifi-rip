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

from . import keychain
from .consensus import Claim, ConflictReport, reconcile_all, strip_noise
from .content import Classification, ContentClass
from .resilience import breaker_for
from .sources import REGISTRY, derivation_map, sources_for, unavailable_for
from .sources import acoustid, discogs, tracklists1001
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


def credential_origins(
    environ: dict[str, str] | None = None,
) -> dict[str, keychain.Resolved]:
    """Where each credential comes from, without its value."""
    env = environ if environ is not None else dict(os.environ)
    return keychain.resolve_all(sorted(KEY_VARIABLES), env)


def available_keys(environ: dict[str, str] | None = None) -> frozenset[str]:
    """Names of the credential variables that resolve to a value.

    Returns variable *names*, never their values. Every caller -- routing,
    reporting, `doctor` -- needs only presence, so the value never leaves the
    environment and cannot be printed, logged, or serialised by accident.

    The Keychain is consulted when the environment is silent, because shell
    profiles reach some processes and not others: `.zshrc` only interactive
    shells, `.zprofile` only login shells, and neither an editor-launched
    tool nor a scheduled job. A key that is plainly configured and invisible
    to the tool is the most confusing failure this project can produce.
    """
    return frozenset(
        variable for variable, resolved in credential_origins(environ).items()
        if resolved.present
    )


def credential_value(
    variable: str, environ: dict[str, str] | None = None
) -> str | None:
    """Fetch one credential for actual use. Never log the result."""
    env = environ if environ is not None else dict(os.environ)
    value, _ = keychain.resolve(variable, env)
    return value


def describe_credentials(environ: dict[str, str] | None = None) -> str:
    """Report which credentials are configured, and where each came from.

    Prints presence and origin, never a value. A diagnostic that echoes a
    secret is one screen-share or pasted bug report away from leaking it, and
    `doctor` output gets pasted into issues constantly. The origin is worth
    showing because "set, from keychain" and "set, from environment" fail in
    completely different ways.
    """
    origins = credential_origins(environ)
    keyed = sorted(
        (spec for spec in REGISTRY.values() if spec.requires_key),
        key=lambda spec: -spec.weight,
    )

    lines = []
    for spec in keyed:
        resolved = origins.get(spec.requires_key)
        if resolved and resolved.present:
            state = f"set ({resolved.origin})"
        else:
            state = "not set"
        lines.append(f"  {spec.requires_key:16s} {state:20s} -> {spec.name}")

    if not any(r.present for r in origins.values()):
        lines.append(
            "  No API credentials found in the environment or the Keychain.\n"
            "  Every keyless source still works; see docs/credentials.md."
        )
    return "\n".join(lines)


@dataclass
class Resolution:
    """Everything the sources collectively established."""

    metadata: ConflictReport
    tracklist: TracklistResult | None = None
    consulted: list[str] = field(default_factory=list)
    silent: list[str] = field(default_factory=list)
    inactive: list[str] = field(default_factory=list)
    #: Sources skipped because their breaker is open.
    circuit_open: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def needs_host(self) -> bool:
        return self.metadata.needs_host or bool(
            self.tracklist and self.tracklist.needs_host
        )

    @property
    def corroboration(self) -> float:
        """Share of resolved fields that two independent sources agreed on.

        The health signal worth watching over time. When a source silently
        starts returning junk -- as AcoustID did while returning a confident
        0.98 score and no metadata -- nothing errors and nothing looks wrong;
        this number just falls. Tracked across a batch it is the difference
        between noticing in an hour and noticing in a month.
        """
        resolutions = self.metadata.resolutions
        if not resolutions:
            return 0.0
        return round(len(self.metadata.settled) / len(resolutions), 3)

    def metrics(self) -> dict[str, object]:
        """Machine-readable summary, for batch reports and health checks."""
        return {
            "elapsed_seconds": self.elapsed,
            "sources_consulted": sorted(self.consulted),
            "sources_silent": sorted(self.silent),
            "sources_inactive": sorted(self.inactive),
            "sources_circuit_open": sorted(self.circuit_open),
            "fields_total": len(self.metadata.resolutions),
            "fields_settled": len(self.metadata.settled),
            "corroboration": self.corroboration,
            "needs_host": self.needs_host,
            "tracks_found": len(self.tracklist.tracks) if self.tracklist else 0,
            "tracks_corroborated": (
                len(self.tracklist.tracks) - len(self.tracklist.unresolved)
                if self.tracklist else 0
            ),
        }

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
        if self.circuit_open:
            lines.append(
                f"  skipped, recently failing: {', '.join(self.circuit_open)}"
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

    circuit_open: list[str] = []

    def gated(name: str) -> bool:
        """Whether to attempt a source, honouring its breaker.

        A source that has failed repeatedly is skipped for a cooldown rather
        than retried per item. During development a rate-limited MusicBrainz
        cost a full timeout on every lookup; across a batch that is one
        timeout per upload for a service already known to be refusing us.
        """
        if breaker_for(name).allows():
            return True
        circuit_open.append(name)
        return False

    jobs = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        if "musicbrainz" in active_names and gated("musicbrainz"):
            title, artist = _seed_from_youtube(info)
            jobs[pool.submit(
                mb.claims_for, title, artist, weight=weights["musicbrainz"]
            )] = "musicbrainz"

        # Community DJ-set tracklists. Scoped to mixes by the registry, and
        # slow by design (one request per five seconds, cached on disk), so
        # it is only worth asking when there is a set to identify.
        if ("1001tracklists" in active_names
                and tracklists1001.parser_available()
                and gated("1001tracklists")):
            query = strip_noise(info.get("title") or "")
            if query:
                jobs[pool.submit(tracklists1001.candidates_for, query)] = "1001tracklists"

        if "discogs" in active_names and gated("discogs"):
            title, artist = _seed_from_youtube(info)
            token = credential_value("DISCOGS_TOKEN", environ)
            if token:
                jobs[pool.submit(
                    discogs.claims_for, title, artist,
                    token=token, weight=weights["discogs"],
                )] = "discogs"

        # Fingerprinting needs the audio itself, so unlike every text source
        # it can only run once something has been downloaded.
        if ("acoustid" in active_names and audio_path
                and audio_path.exists() and gated("acoustid")):
            key = credential_value("ACOUSTID_KEY", environ)
            if key and acoustid.fingerprinter_available():
                jobs[pool.submit(
                    acoustid.claims_for, audio_path, key,
                    weight=weights["acoustid"],
                )] = "acoustid"

        if "silence" in active_names and audio_path and audio_path.exists():
            jobs[pool.submit(silence_mod.detect_gaps, audio_path)] = "silence"

        for future in as_completed(jobs, timeout=SOURCE_TIMEOUT * 2):
            name = jobs[future]
            try:
                outcome = future.result(timeout=SOURCE_TIMEOUT)
            except Exception as exc:
                # A source that fails is absent, never fatal -- but the
                # failure is remembered, so a service that is down stops
                # being asked once per item.
                breaker_for(name).record_failure(str(exc))
                continue
            breaker_for(name).record_success()
            if not outcome:
                continue
            consulted.append(name)
            if name == "1001tracklists":
                contributions[name] = outcome
            elif name == "silence":
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
        circuit_open=sorted(set(circuit_open)),
        elapsed=round(time.monotonic() - started, 2),
    )
