"""Cutting one upload into tracks without re-encoding or inserting silence.

Every cut is `-c copy`, so the audio in each track is the same compressed
data the source held. Nothing is padded: the usual reason split albums gain
gaps is that a tool re-encoded each piece, and each encode contributed its
own priming silence.

Two honest caveats, both measured rather than assumed -- see
`verify_concatenation`, which is what establishes them for a given file
rather than taking this docstring's word for it:

*Cuts are packet-aligned, not sample-aligned.* Audio packets carry roughly
20ms (Opus) or 23ms (AAC at 44.1kHz), and a copy cannot subdivide one. A
requested boundary therefore lands within one packet of where it was asked
for -- inaudible for music, but it means boundaries are quantised.

Boundaries are snapped onto real packet timestamps before cutting, and each
snapped time is used as both the end of one track and the start of the next.
Without that, ffmpeg rounds each segment independently and outward, so
neighbours disagree about the seam and a few milliseconds are duplicated at
every cut. Measured on a real file, snapping took AAC from 19ms of drift to
byte-exact.

*Ogg Opus still reports a small duration drift.* Every Ogg stream carries
its own pre-skip header, so three tracks carry three of them. The audio
packets still rejoin byte for byte; only the container's duration
bookkeeping moves, by around 13ms across a handful of cuts. Matroska, by
contrast, drifts by seconds and should not be used for split output.

*Continuous mixes have no correct cut point anyway.* Across a crossfade both
tracks are genuinely playing, so any boundary is a choice about which track
keeps the transition, not an approximation of a true one.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .download import DownloadError, packet_md5, probe_media
from .tag import safe_component

#: Below this, a "track" is an artefact of bad boundary data rather than
#: music. Splitting on it would produce files nobody wants.
MIN_TRACK_SECONDS = 5.0


class SplitError(RuntimeError):
    pass


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise SplitError("ffmpeg not found on PATH")
    return path


@dataclass(frozen=True)
class Boundary:
    """One track's extent within the source."""

    start: float
    end: float
    title: str
    track: int | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start

    def __str__(self) -> str:
        return f"{self.track or '-'}. {self.title} [{self.start:.1f}-{self.end:.1f}s]"


@dataclass
class SplitResult:
    parts: list[Path] = field(default_factory=list)
    boundaries: list[Boundary] = field(default_factory=list)
    #: Populated by `verify_concatenation`; None when it was not run.
    reassembles_exactly: bool | None = None
    drift_seconds: float | None = None

    def report(self) -> str:
        """Report the two dimensions separately, because they differ.

        Whether the *audio* survives a round trip and whether the *container*
        agrees about duration are independent questions. Ogg Opus writes a
        fresh pre-skip header into every stream it creates, so a split whose
        audio is byte-perfect can still report a few milliseconds of duration
        drift. Collapsing the two into one verdict makes a bookkeeping detail
        look like lost audio.
        """
        lines = [f"Split into {len(self.parts)} track(s), copied without re-encoding."]
        drift_ms = (self.drift_seconds or 0.0) * 1000

        if self.reassembles_exactly is True:
            lines.append(
                "  audio: rejoining the tracks reproduces the source byte for "
                "byte -- no samples were lost, added, or padded at the cuts."
            )
            if drift_ms > 1.0:
                lines.append(
                    f"  duration: the rejoined container reports {drift_ms:.0f}ms "
                    f"more than the source. This is pre-skip bookkeeping written "
                    f"into each new stream, not audio; the samples are identical."
                )
        elif self.reassembles_exactly is False:
            lines.append(
                f"  audio: rejoining the tracks does NOT reproduce the source "
                f"(duration differs by {drift_ms:.0f}ms). This container is a "
                f"poor choice for splitting; prefer .m4a or .opus."
            )
        return "\n".join(lines)


def derive_boundaries(
    points: list[float],
    titles: list[str],
    duration: float,
    *,
    start: float = 0.0,
) -> list[Boundary]:
    """Turn a list of start times into contiguous, gapless extents.

    Each track runs to the start of the next, with no space between them --
    a gap here would discard audio, and an overlap would duplicate it.
    """
    if len(points) != len(titles):
        raise SplitError(
            f"got {len(points)} boundary times but {len(titles)} titles"
        )
    if not points:
        return []

    ordered = sorted(zip(points, titles), key=lambda pair: pair[0])

    # Reject implausibly close cut *points* rather than the short segments
    # they produce. Dropping a segment would leave the span it covered in no
    # track at all -- a silent hole in the middle of the album, which is a
    # worse outcome than a slightly long track.
    accepted: list[tuple[float, str]] = []
    for point, title in ordered:
        point = max(point, start)
        if accepted and point - accepted[-1][0] < MIN_TRACK_SECONDS:
            continue
        accepted.append((point, title))

    # The same rule at the end: a stub final track means the last cut was
    # spurious, so fold it back into its predecessor.
    while len(accepted) > 1 and duration - accepted[-1][0] < MIN_TRACK_SECONDS:
        accepted.pop()

    boundaries: list[Boundary] = []
    for index, (point, title) in enumerate(accepted):
        end = accepted[index + 1][0] if index + 1 < len(accepted) else duration
        boundaries.append(Boundary(
            start=point, end=end, title=title, track=index + 1
        ))
    return boundaries


def packet_times(source: Path, around: float, window: float = 2.0) -> list[float]:
    """Presentation timestamps of audio packets near `around`.

    Only a small interval is probed. A three-hour Opus file holds well over
    half a million packets, and enumerating all of them to place one cut
    would cost more than the entire rip.
    """
    start = max(0.0, around - window)
    result = subprocess.run(
        [
            shutil.which("ffprobe") or "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-read_intervals", f"{start:.3f}%{around + window:.3f}",
            "-show_entries", "packet=pts_time",
            "-of", "csv=p=0", str(source),
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        return []
    times = []
    for line in result.stdout.splitlines():
        value = line.strip().rstrip(",")
        try:
            times.append(float(value))
        except ValueError:
            continue
    return sorted(times)


def snap_to_packet(source: Path, time: float) -> float:
    """Move a requested boundary onto a real packet start.

    A copy cannot subdivide a packet, so ffmpeg rounds every cut to a packet
    edge anyway. Left to itself it rounds each segment *independently* and
    outward, so neighbouring segments disagree about where the boundary was
    and a few milliseconds get duplicated at every cut. Snapping first means
    both sides of a boundary are given the identical timestamp, and the
    tracks rejoin exactly.
    """
    if time <= 0:
        return 0.0
    candidates = packet_times(source, time)
    if not candidates:
        return time
    return min(candidates, key=lambda t: abs(t - time))


def snap_boundaries(source: Path, boundaries: list[Boundary]) -> list[Boundary]:
    """Align every boundary to a packet edge, sharing edges between neighbours.

    Each cut point is snapped once and then used as *both* the end of the
    preceding track and the start of the following one, so the two sides
    cannot disagree. This is what makes the pieces rejoin exactly rather
    than overlapping by a packet at every seam.
    """
    if not boundaries:
        return []

    snapped_starts = [snap_to_packet(source, b.start) for b in boundaries]
    aligned: list[Boundary] = []
    for index, boundary in enumerate(boundaries):
        start = snapped_starts[index]
        end = snapped_starts[index + 1] if index + 1 < len(boundaries) else boundary.end
        aligned.append(Boundary(
            start=start, end=end, title=boundary.title, track=boundary.track
        ))
    return aligned


def cut(source: Path, boundary: Boundary, destination: Path) -> Path:
    """Extract one extent with `-c copy`.

    `-ss` is placed before `-i` so ffmpeg seeks rather than decodes-and-
    discards; for audio-only input every packet is independently decodable,
    so this stays accurate to the packet while remaining fast on a
    three-hour file.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            _ffmpeg(), "-v", "error", "-y",
            "-ss", f"{boundary.start:.6f}",
            "-i", str(source),
            "-t", f"{boundary.duration:.6f}",
            "-map", "0:a:0", "-c:a", "copy",
            "-map_metadata", "-1",
            str(destination),
        ],
        capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0:
        raise SplitError(
            f"could not cut {boundary} from {source.name}: {result.stderr.strip()}"
        )
    return destination


def split(
    source: Path,
    boundaries: list[Boundary],
    outdir: Path,
    *,
    container: str | None = None,
    verify: bool = True,
) -> SplitResult:
    """Cut a source into tracks, optionally proving the cuts lose nothing."""
    if not boundaries:
        raise SplitError("no boundaries supplied; nothing to split")

    suffix = container or source.suffix.lstrip(".")
    outdir.mkdir(parents=True, exist_ok=True)

    boundaries = snap_boundaries(source, boundaries)

    parts: list[Path] = []
    for boundary in boundaries:
        name = f"{boundary.track:02d} - {safe_component(boundary.title)}.{suffix}"
        parts.append(cut(source, boundary, outdir / name))

    result = SplitResult(parts=parts, boundaries=boundaries)
    if verify:
        exact, drift = verify_concatenation(parts, source, outdir)
        result.reassembles_exactly = exact
        result.drift_seconds = drift
    return result


def concatenate(parts: list[Path], destination: Path) -> Path:
    """Rejoin tracks with `-c copy`, for verification."""
    listing = destination.parent / "concat.txt"
    listing.write_text(
        "".join(f"file '{part.resolve()}'\n" for part in parts)
    )
    result = subprocess.run(
        [
            _ffmpeg(), "-v", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", str(listing), "-c", "copy", str(destination),
        ],
        capture_output=True, text=True, timeout=1800,
    )
    listing.unlink(missing_ok=True)
    if result.returncode != 0:
        raise SplitError(f"could not rejoin tracks: {result.stderr.strip()}")
    return destination


def verify_concatenation(
    parts: list[Path], source: Path, workdir: Path
) -> tuple[bool, float | None]:
    """Measure what rejoining the tracks actually reproduces.

    Returns (byte-identical, duration drift in seconds). This is the check
    that keeps "splitting loses nothing" honest: if a cut ever silently
    dropped or padded audio, the rejoined file would differ here, and the
    difference is reported rather than glossed.
    """
    rejoined = workdir / f"_verify.{source.suffix.lstrip('.')}"
    try:
        concatenate(parts, rejoined)
        identical = packet_md5(rejoined) == packet_md5(source)
        drift = None
        original = probe_media(source).duration
        rebuilt = probe_media(rejoined).duration
        if original and rebuilt:
            drift = abs(original - rebuilt)
        return identical, drift
    except (SplitError, DownloadError):
        return False, None
    finally:
        rejoined.unlink(missing_ok=True)
