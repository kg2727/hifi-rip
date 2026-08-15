"""Acoustic fingerprinting via AcoustID.

The only source that observes the recording instead of reading what somebody
typed about it. That makes it uniquely useful in the cases text sources fail
completely: a Short whose caption says nothing about the music, an untitled
upload, a mix where a track is identified by no one.

It is marked as deriving from MusicBrainz in the registry, because its
metadata resolves against MusicBrainz IDs. So it does **not** corroborate
MusicBrainz's values under the independence rule -- it extends *reach*, not
agreement, and pretending otherwise would manufacture consensus between a
database and a lookup into that same database.

Two practical constraints shape the interface. Fingerprinting needs the audio
on disk, so this can only run after a download, unlike every text source. And
it needs the `fpcalc` binary from chromaprint, which is a separate install
from the API key -- a key with no fpcalc silently does nothing, which is
exactly the sort of quiet failure worth reporting explicitly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..consensus import Claim

API_URL = "https://api.acoustid.org/v2/lookup"
USER_AGENT = "hifi-rip/0.1.0 +https://github.com/kg2727/hifi-rip"

#: AcoustID asks for no more than three requests per second.
MIN_INTERVAL = 0.4
REQUEST_TIMEOUT = 10.0

#: Seconds of audio to fingerprint. The service matches on roughly the first
#: two minutes, and hashing a three-hour set in full would cost minutes of
#: CPU for no additional accuracy.
FINGERPRINT_SECONDS = 120

#: Below this the match is too speculative to spend a weighted vote on.
#: Remixes fingerprint close to their originals, so a low score here often
#: means "a different version of this song" rather than "no idea".
MIN_SCORE = 0.85

_throttle = threading.Lock()
_last_request = 0.0


class FingerprinterMissing(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "fpcalc not found on PATH. AcoustID needs the chromaprint "
            "fingerprinter as well as an API key: `brew install chromaprint`."
        )


def _wait_turn() -> None:
    global _last_request
    with _throttle:
        elapsed = time.monotonic() - _last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        _last_request = time.monotonic()


def fingerprinter_available() -> bool:
    return bool(shutil.which("fpcalc"))


@dataclass(frozen=True)
class Fingerprint:
    duration: int
    value: str


@dataclass(frozen=True)
class Identification:
    score: float
    recording_id: str
    title: str | None
    artist: str | None
    album: str | None
    date: str | None

    def __str__(self) -> str:
        return f"{self.artist or '?'} - {self.title or '?'} ({self.score:.2f})"


def fingerprint(
    path: Path, *, seconds: int = FINGERPRINT_SECONDS, offset: float = 0.0
) -> Fingerprint | None:
    """Compute a chromaprint fingerprint for a stretch of audio.

    `offset` exists for mixes: fingerprinting from zero identifies only the
    first track, so a caller walking a set passes the start of each one.
    """
    if not fingerprinter_available() or not path.exists():
        return None

    command = ["fpcalc", "-json", "-length", str(seconds)]
    if offset > 0:
        command += ["-ts", f"{offset:.3f}"]
    command.append(str(path))

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return None

    value = payload.get("fingerprint")
    duration = payload.get("duration")
    if not value or not duration:
        return None
    return Fingerprint(duration=int(float(duration)), value=value)


def lookup(
    print_: Fingerprint,
    api_key: str,
    *,
    client: httpx.Client | None = None,
) -> list[Identification]:
    """Ask AcoustID what a fingerprint matches."""
    if not api_key or not print_:
        return []

    _wait_turn()
    owned = client is None
    http = client or httpx.Client(timeout=REQUEST_TIMEOUT)
    try:
        # POST, because a fingerprint is long enough to exceed URL limits on
        # some proxies, and it keeps the key out of request logs.
        response = http.post(
            API_URL,
            data={
                "client": api_key,
                "duration": print_.duration,
                "fingerprint": print_.value,
                # `recordings` alone, deliberately. Measured against a track
                # certainly in the database, "recordings+releases" and
                # "recordings+releasegroups" both return a strong 0.98 score
                # with *zero* recordings attached, while "recordings" returns
                # four correct ones. The richer requests fail silently rather
                # than erroring, which reads as "not in the database".
                #
                # No loss: this source exists to identify what is playing.
                # Album and date come from the release-authoritative sources,
                # and AcoustID could not corroborate them anyway, since it
                # resolves against MusicBrainz.
                "meta": "recordings",
                "format": "json",
            },
            headers={"User-Agent": USER_AGENT},
        )
    except (httpx.HTTPError, ValueError):
        return []
    finally:
        if owned:
            http.close()

    if response.status_code != 200:
        return []
    try:
        payload = response.json()
    except ValueError:
        return []
    if payload.get("status") != "ok":
        return []

    found: list[Identification] = []
    for result in payload.get("results") or []:
        score = float(result.get("score") or 0.0)
        if score < MIN_SCORE:
            continue
        for recording in result.get("recordings") or []:
            artists = recording.get("artists") or []
            releases = recording.get("releases") or []
            release = min(
                releases,
                key=lambda r: str(r.get("date", {}).get("year") or 9999),
            ) if releases else {}
            found.append(Identification(
                score=score,
                recording_id=recording.get("id", ""),
                title=recording.get("title"),
                artist=artists[0].get("name") if artists else None,
                album=release.get("title"),
                date=str(release.get("date", {}).get("year") or "") or None,
            ))
    return sorted(found, key=lambda i: -i.score)


def claims_for(
    path: Path,
    api_key: str,
    *,
    weight: float = 0.85,
    offset: float = 0.0,
    client: httpx.Client | None = None,
) -> list[Claim]:
    """Identify audio on disk and turn the best match into claims."""
    print_ = fingerprint(path, offset=offset)
    if not print_:
        return []

    matches = lookup(print_, api_key, client=client)
    if not matches:
        return []

    best = matches[0]
    detail = f"acoustid score {best.score:.2f}, recording {best.recording_id}"

    claims: list[Claim] = []
    if best.title:
        claims.append(Claim("title", best.title, "acoustid", weight, detail))
    if best.artist:
        claims.append(Claim("artist", best.artist, "acoustid", weight, detail))
    if best.album:
        claims.append(Claim("album", best.album, "acoustid", weight, detail))
    if best.date:
        claims.append(Claim("date", best.date, "acoustid", weight, detail))
    return claims


def readiness() -> str:
    """Why AcoustID may be inactive, for `doctor`."""
    if not fingerprinter_available():
        return (
            "fpcalc not installed -- an AcoustID key alone does nothing. "
            "Install chromaprint: brew install chromaprint"
        )
    return "fpcalc present"
