"""Thin wrapper around `yt-dlp -J`.

We shell out to the yt-dlp *binary* rather than importing it as a library,
deliberately. YouTube breaks extractors on a roughly weekly cadence, and a
user who runs `brew upgrade yt-dlp` or `pipx upgrade yt-dlp` should get the
fix immediately without waiting on a release of this package.

stderr is captured separately from stdout and handed back intact, because
`entitlement.assess` establishes credential validity from yt-dlp's own
diagnostics rather than by inferring it from which streams came back.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Sequence

from .formats import AudioStream, parse_streams

DEFAULT_TIMEOUT = 120


class ProbeError(RuntimeError):
    pass


class YtDlpMissing(ProbeError):
    def __init__(self) -> None:
        super().__init__(
            "yt-dlp not found on PATH. Install it with `brew install yt-dlp`, "
            "`pipx install yt-dlp`, or your package manager of choice."
        )


@dataclass
class ProbeResult:
    """Everything one metadata probe yielded."""

    info: dict
    streams: list[AudioStream]
    stderr: str
    cookies_configured: bool

    @property
    def video_id(self) -> str | None:
        return self.info.get("id")

    @property
    def title(self) -> str | None:
        return self.info.get("title")

    @property
    def duration(self) -> float | None:
        return self.info.get("duration")

    @property
    def chapters(self) -> list[dict]:
        return self.info.get("chapters") or []

    @property
    def is_playlist(self) -> bool:
        return self.info.get("_type") == "playlist"


def yt_dlp_path() -> str:
    path = shutil.which("yt-dlp")
    if not path:
        raise YtDlpMissing()
    return path


def build_command(
    url: str,
    *,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    cmd = [yt_dlp_path(), "-J", "--no-warnings", "--no-playlist"]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    elif cookies_file:
        cmd += ["--cookies", cookies_file]
    cmd += list(extra_args)
    cmd.append(url)
    return cmd


def probe(
    url: str,
    *,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    extra_args: Sequence[str] = (),
) -> ProbeResult:
    """Fetch metadata and the audio-only stream list for `url`."""
    cmd = build_command(
        url,
        cookies_from_browser=cookies_from_browser,
        cookies_file=cookies_file,
        extra_args=extra_args,
    )
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"yt-dlp timed out after {timeout}s probing {url}") from exc

    stderr = completed.stderr or ""

    # A non-zero exit with cookie complaints still tells us something useful,
    # so surface stderr verbatim rather than a generic failure message.
    if completed.returncode != 0:
        raise ProbeError(
            f"yt-dlp exited {completed.returncode} probing {url}:\n{stderr.strip()}"
        )

    try:
        info = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"Could not parse yt-dlp JSON for {url}: {exc}") from exc

    return ProbeResult(
        info=info,
        streams=parse_streams(info.get("formats") or []),
        stderr=stderr,
        cookies_configured=bool(cookies_from_browser or cookies_file),
    )
