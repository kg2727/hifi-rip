"""Fetching and remuxing, with the no-transcode rule enforced rather than promised.

The README claims output audio is bit-identical to the stream YouTube served.
That is exactly the kind of claim that rots silently: a container change that
quietly re-encodes still produces a playable file of roughly the right size,
and nobody notices until they compare spectrograms months later.

So it is checked. `ffmpeg -f md5` hashes the audio packets themselves, before
and after the remux. Identical hashes prove the packets were copied rather
than decoded and re-encoded. A mismatch aborts instead of shipping a file
that merely looks fine.

Downloading and remuxing are kept as separate steps -- rather than using
yt-dlp's own post-processing -- so the source file survives long enough to be
hashed against the result.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .formats import AudioStream

DEFAULT_TIMEOUT = 3600


class DownloadError(RuntimeError):
    pass


class TranscodeDetected(DownloadError):
    """The remux altered the audio. Never expected; always a bug or a bad container."""


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise DownloadError(
            f"{name} not found on PATH. Install it with `brew install {name}`."
        )
    return path


@dataclass
class Media:
    """What ffprobe reports about an audio file."""

    codec: str
    sample_rate: int | None
    channels: int | None
    duration: float | None
    bitrate: float | None   # kbps

    def __str__(self) -> str:
        rate = f"{self.bitrate:.0f}k" if self.bitrate else "?k"
        return f"{self.codec} {rate} @ {self.sample_rate or '?'}Hz"


def probe_media(path: Path) -> Media:
    """Read the audio stream's parameters from a file on disk.

    Both the stream and the format sections are read, because WebM stores
    duration and bitrate on the container rather than the stream: for Opus in
    WebM -- the format selected most often -- the stream section reports N/A
    for both, and reading it alone yields a file we can say nothing about.
    """
    result = subprocess.run(
        [
            _tool("ffprobe"), "-v", "error", "-select_streams", "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,duration,bit_rate"
            ":format=duration,bit_rate",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise DownloadError(f"ffprobe failed on {path}: {result.stderr.strip()}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise DownloadError(f"could not parse ffprobe output for {path}: {exc}") from None

    streams = payload.get("streams") or []
    if not streams:
        raise DownloadError(f"{path} contains no audio stream")
    stream = streams[0]
    container = payload.get("format") or {}

    def number(source: dict, key: str) -> float | None:
        raw = source.get(key)
        if raw in (None, "", "N/A"):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    duration = number(stream, "duration") or number(container, "duration")
    bitrate = number(stream, "bit_rate") or number(container, "bit_rate")
    if bitrate:
        bitrate = bitrate / 1000
    elif duration:
        # Derived from file size: slightly high, since it counts container
        # overhead. Still far more useful than reporting "?k".
        try:
            bitrate = path.stat().st_size * 8 / duration / 1000
        except OSError:
            bitrate = None

    return Media(
        codec=stream.get("codec_name", "unknown"),
        sample_rate=int(number(stream, "sample_rate") or 0) or None,
        channels=int(number(stream, "channels") or 0) or None,
        duration=duration,
        bitrate=bitrate,
    )


def packet_md5(path: Path) -> str:
    """Hash the audio packets, ignoring container framing.

    Decoding is deliberately avoided: `-c copy` hashes the compressed packets
    as stored, so the result changes if and only if the audio data itself
    changed. Container metadata, chapter atoms and tags do not affect it,
    which is what makes it a usable before/after comparison across a
    container change.
    """
    result = subprocess.run(
        [
            _tool("ffmpeg"), "-v", "error", "-i", str(path),
            "-map", "0:a:0", "-c", "copy", "-f", "md5", "-",
        ],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise DownloadError(f"ffmpeg hash failed on {path}: {result.stderr.strip()}")
    match = re.search(r"MD5=([0-9a-f]+)", result.stdout)
    if not match:
        raise DownloadError(f"could not parse ffmpeg md5 output: {result.stdout!r}")
    return match.group(1)


@dataclass
class RipResult:
    path: Path
    source: Media
    output: Media
    verified_identical: bool

    def report(self) -> str:
        lines = [
            f"Wrote {self.path.name}",
            f"  source: {self.source}",
            f"  output: {self.output}",
        ]
        if self.verified_identical:
            lines.append(
                "  verified: audio packets are bit-identical to the source "
                "stream (md5 match)"
            )
        else:
            lines.append("  verified: NOT CHECKED")
        return "\n".join(lines)


def fetch(
    url: str,
    stream: AudioStream,
    workdir: Path,
    *,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    progress: bool = True,
) -> Path:
    """Download exactly one pre-selected stream, with no post-processing."""
    workdir.mkdir(parents=True, exist_ok=True)
    template = str(workdir / "source.%(ext)s")

    cmd = [
        _tool("yt-dlp"),
        "-f", stream.itag,
        # No --extract-audio and no --remux-video: post-processing here would
        # consume the source file we need to hash against.
        "--no-playlist", "--no-part", "--no-warnings",
        "-o", template,
    ]
    if not progress:
        cmd.append("--quiet")
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    elif cookies_file:
        cmd += ["--cookies", cookies_file]
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise DownloadError(
            f"yt-dlp exited {result.returncode} downloading {url}:\n"
            f"{result.stderr.strip()}"
        )

    downloaded = sorted(workdir.glob("source.*"))
    if not downloaded:
        raise DownloadError(f"yt-dlp reported success but wrote nothing to {workdir}")
    return downloaded[0]


def remux(source: Path, destination: Path, *, verify: bool = True) -> RipResult:
    """Change container without touching the audio, and prove it.

    `-vn` drops any attached cover-art video stream, which several containers
    carry and which would otherwise block the copy. Tags are stripped here and
    written deliberately later, so a rip never inherits whatever the uploader
    happened to leave in the source.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = probe_media(source)
    source_hash = packet_md5(source) if verify else None

    result = subprocess.run(
        [
            _tool("ffmpeg"), "-v", "error", "-y",
            "-i", str(source),
            "-vn", "-map", "0:a:0", "-c:a", "copy",
            "-map_metadata", "-1",
            str(destination),
        ],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise DownloadError(
            f"ffmpeg could not remux {source.name} to {destination.suffix}: "
            f"{result.stderr.strip()}"
        )

    after = probe_media(destination)

    if before.codec != after.codec:
        raise TranscodeDetected(
            f"Remuxing {source.suffix} -> {destination.suffix} changed the codec "
            f"({before.codec} -> {after.codec}). This would stack a second lossy "
            f"generation, so the output has been rejected."
        )

    verified = False
    if verify:
        if packet_md5(destination) != source_hash:
            raise TranscodeDetected(
                f"Audio packets changed while remuxing to {destination.suffix}. "
                f"The codec is still {after.codec}, so this is a silent "
                f"re-encode rather than a copy, and the output has been "
                f"rejected rather than shipped as lossless."
            )
        verified = True

    return RipResult(
        path=destination, source=before, output=after, verified_identical=verified
    )
