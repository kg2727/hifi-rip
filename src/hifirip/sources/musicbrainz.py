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
    is_compilation: bool = False

    def __str__(self) -> str:
        kind = " [compilation]" if self.is_compilation else ""
        return f"{self.artist or '?'} - {self.title} ({self.score}){kind}"


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
COMPILATION_TYPES = {"compilation", "live", "soundtrack", "dj-mix", "mixtape/street",
                     "remix", "interview"}

#: Deliberately large. MusicBrainz stores a separate recording entity per
#: master, so a well-known song has hundreds, and the search ranks them by
#: text score -- which ties at 100 across all of them. A five-result page
#: therefore returns an arbitrary handful: measured on a famous single, the
#: top five were a millennium megamix, two other megamixes, a fitness
#: compilation and a live album, with the original studio release absent
#: entirely. Ranking releases cannot fix a page that never contains the
#: right recording.
SEARCH_LIMIT = 25


def _is_compilation(release: dict) -> bool:
    group = release.get("release-group") or {}
    primary = (group.get("primary-type") or "").lower()
    secondary = {t.lower() for t in group.get("secondary-types") or []}
    return bool(secondary & COMPILATION_TYPES) or primary in COMPILATION_TYPES


def _release_rank(release: dict) -> tuple[int, str]:
    # Undated releases sort last: a missing date is not evidence of being
    # early, and treating it as "0000" would beat every real album.
    return (1 if _is_compilation(release) else 0, release.get("date") or "9999")


def _original_release(releases: list[dict]) -> dict:
    """Pick the release a recording actually belongs to."""
    return min(releases, key=_release_rank) if releases else {}


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
    limit: int = SEARCH_LIMIT,
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
        releases = item.get("releases") or []
        release = _original_release(releases)
        length = item.get("length")
        recordings.append(Recording(
            mbid=item.get("id", ""),
            title=item.get("title", ""),
            artist=artist_name,
            release=release.get("title"),
            date=(release.get("date") or "")[:4] or None,
            length=length / 1000 if length else None,
            score=score,
            is_compilation=_is_compilation(release) if release else False,
        ))
    return recordings


def best_candidate(recordings: list[Recording]) -> Recording | None:
    """Choose across candidates, not within one.

    Text score is useless for ranking here: every plausible match ties at
    100, so `max(score)` returns whichever entity the server listed first.
    What distinguishes them is what they appear *on* -- a recording whose
    only release is a themed megamix is the same song, but it is not the
    canonical one, and filing a library under it is wrong in a way the user
    will notice.
    """
    if not recordings:
        return None
    return min(
        recordings,
        key=lambda r: (
            1 if r.is_compilation else 0,
            r.date or "9999",
            -r.score,
        ),
    )


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

    best = best_candidate(results)
    if not best:
        return []
    detail = f"MBID {best.mbid}, search score {best.score}"

    claims = [Claim("title", best.title, "musicbrainz", weight, detail)]
    if best.artist:
        claims.append(Claim("artist", best.artist, "musicbrainz", weight, detail))

    # The album is only claimed when it is a real one. A compilation title is
    # worse than no album at all: it is confidently wrong, and it becomes a
    # folder name in the user's library.
    if best.release and not best.is_compilation:
        claims.append(Claim("album", best.release, "musicbrainz", weight, detail))
        if best.date:
            claims.append(Claim("date", best.date, "musicbrainz", weight, detail))
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
