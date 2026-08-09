"""MusicBrainz lookups.

Needs no credentials, which is why it anchors a keyless install, but it does
require a descriptive User-Agent and tolerates only about one request per
second. Both are conditions of use rather than suggestions, and a client that
ignores them gets blocked -- so the rate limit is enforced here rather than
left to callers to remember.

Search results carry a server-side score. It is used only to *rank*
candidates, never as a confidence: a 100-scored match to the wrong recording
is still wrong, and the weight this source carries in a vote is fixed by the
registry, not by how sure MusicBrainz sounds.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

import httpx

from ..consensus import Claim, normalise

API_ROOT = "https://musicbrainz.org/ws/2"
USER_AGENT = "hifi-rip/0.1.0 (https://github.com/kg2727/hifi-rip)"

#: MusicBrainz asks for no more than one request per second.
MIN_INTERVAL = 1.1

#: Below this search score a result is too speculative to be worth a vote.
MIN_SCORE = 70

_throttle = threading.Lock()
_last_request = 0.0


def _wait_turn() -> None:
    global _last_request
    with _throttle:
        elapsed = time.monotonic() - _last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        _last_request = time.monotonic()


@dataclass(frozen=True)
class Recording:
    mbid: str
    title: str
    artist: str | None
    release: str | None
    date: str | None
    length: float | None      # seconds
    score: int = 0

    def __str__(self) -> str:
        return f"{self.artist or '?'} - {self.title} ({self.score})"


#: Kept short. A slow MusicBrainz must not hold up a rip whose other sources
#: have already answered; it is one weighted vote, not a prerequisite.
REQUEST_TIMEOUT = 8.0
MAX_ATTEMPTS = 2


def _get(path: str, params: dict, *, client: httpx.Client | None = None) -> dict:
    """One request, with a single polite retry on explicit rate limiting.

    MusicBrainz answers a client that exceeds its limit with 503 and a
    Retry-After header. Retrying more than once would make the throttling
    worse rather than better, so a second failure is simply accepted as no
    data -- degrading to the other sources is the designed behaviour.
    """
    owned = client is None
    http = client or httpx.Client(timeout=REQUEST_TIMEOUT)
    try:
        for attempt in range(MAX_ATTEMPTS):
            _wait_turn()
            try:
                response = http.get(
                    f"{API_ROOT}/{path}",
                    params={**params, "fmt": "json"},
                    headers={"User-Agent": USER_AGENT},
                )
            except (httpx.HTTPError, ValueError):
                return {}

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    return {}
            if response.status_code == 503 and attempt + 1 < MAX_ATTEMPTS:
                delay = _retry_after(response) or MIN_INTERVAL
                time.sleep(min(delay, 5.0))
                continue
            return {}
        return {}
    finally:
        if owned:
            http.close()


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


#: Release groups that are not the recording's own album. A hit song appears
#: on dozens of these, and they crowd out the original.
COMPILATION_TYPES = {"compilation", "live", "soundtrack", "dj-mix", "mixtape/street"}


def _original_release(releases: list[dict]) -> dict:
    """Pick the release a recording actually belongs to.

    MusicBrainz returns releases in no useful order, so taking the first
    yields whichever compilation happens to be listed -- a well-known single
    resolves to a themed party album rather than the record it came from.
    Studio albums are preferred over compilations, and the earliest date
    wins, which is nearly always the original issue.
    """
    if not releases:
        return {}

    def rank(release: dict) -> tuple[int, str]:
        group = release.get("release-group") or {}
        primary = (group.get("primary-type") or "").lower()
        secondary = {t.lower() for t in group.get("secondary-types") or []}
        is_compilation = bool(secondary & COMPILATION_TYPES) or primary in COMPILATION_TYPES
        # Undated releases sort last: a missing date is not evidence of being
        # early, and treating it as "0000" would beat every real album.
        date = release.get("date") or "9999"
        return (1 if is_compilation else 0, date)

    return min(releases, key=rank)


def _clean_query(text: str) -> str:
    """Strip Lucene syntax so a title containing punctuation is not a query.

    A title with a colon or brackets would otherwise be parsed as a field
    specifier and either error or silently match nothing.
    """
    return re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', " ", text or "").strip()


def search_recording(
    title: str,
    artist: str | None = None,
    *,
    limit: int = 5,
    client: httpx.Client | None = None,
) -> list[Recording]:
    """Find recordings matching a title, optionally narrowed by artist."""
    title = _clean_query(title)
    if not title:
        return []

    query = f'recording:"{title}"'
    if artist:
        cleaned = _clean_query(artist)
        if cleaned:
            query += f' AND artist:"{cleaned}"'

    payload = _get("recording", {"query": query, "limit": limit}, client=client)

    recordings: list[Recording] = []
    for item in payload.get("recordings") or []:
        score = int(item.get("score") or 0)
        if score < MIN_SCORE:
            continue
        credits = item.get("artist-credit") or []
        artist_name = credits[0].get("name") if credits else None
        release = _original_release(item.get("releases") or [])
        length = item.get("length")
        recordings.append(Recording(
            mbid=item.get("id", ""),
            title=item.get("title", ""),
            artist=artist_name,
            release=release.get("title"),
            date=(release.get("date") or "")[:4] or None,
            length=length / 1000 if length else None,
            score=score,
        ))
    return recordings


def lookup_release(mbid: str, *, client: httpx.Client | None = None) -> dict:
    """Fetch a recording's releases with their release-group types.

    The search endpoint returns releases without group metadata, so there is
    nothing to distinguish an original album from the dozens of compilations
    a well-known song appears on -- and picking by date alone reliably lands
    on a themed mix from the wrong decade. One extra throttled request buys
    the type information that makes the choice correct.
    """
    if not mbid:
        return {}
    payload = _get(
        f"recording/{mbid}", {"inc": "releases+release-groups"}, client=client
    )
    return _original_release(payload.get("releases") or [])


def claims_for(
    title: str,
    artist: str | None = None,
    *,
    weight: float = 1.0,
    client: httpx.Client | None = None,
) -> list[Claim]:
    """Turn the best match into claims the consensus engine can weigh.

    Only the top-ranked recording contributes. Emitting several candidates
    would let one source vote repeatedly for near-identical spellings and
    manufacture the appearance of agreement.
    """
    results = search_recording(title, artist, client=client)
    if not results:
        return []

    best = max(results, key=lambda r: r.score)
    detail = f"MBID {best.mbid}, search score {best.score}"

    claims = [Claim("title", best.title, "musicbrainz", weight, detail)]
    if best.artist:
        claims.append(Claim("artist", best.artist, "musicbrainz", weight, detail))

    # Resolve the album properly rather than trusting the search payload.
    release = lookup_release(best.mbid, client=client) or {}
    album = release.get("title") or best.release
    date = (release.get("date") or "")[:4] or best.date
    if album:
        claims.append(Claim("album", album, "musicbrainz", weight, detail))
    if date:
        claims.append(Claim("date", date, "musicbrainz", weight, detail))
    return claims


def confirms(recording: Recording, title: str, artist: str | None) -> bool:
    """Whether a result actually matches what was asked for.

    Search is fuzzy and will happily return a different song by the same
    artist. Comparing normalised forms catches the cases where the score is
    high but the result is not the recording in hand.
    """
    if normalise(recording.title) != normalise(title):
        return False
    if artist and recording.artist:
        return normalise(recording.artist) == normalise(artist)
    return True
