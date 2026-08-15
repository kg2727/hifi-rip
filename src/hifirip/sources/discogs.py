"""Discogs lookups.

Weighted just below MusicBrainz and, crucially, **independent of it**. That
independence is what makes this key worth having: under the consensus rule a
value needs two independent sources, and a keyless install has only
MusicBrainz and the uploader's own title. Discogs is the third opinion that
lets a field settle when those two disagree, and the second when MusicBrainz
has nothing.

Discogs is release-scoped rather than recording-scoped, which is its strength
and its weakness. Pressing-level detail is unusually good; the same song
appears under many entries, and duplicates are common. So the search is
narrowed by artist wherever one is known, and the earliest non-compilation
result wins for the same reason it does in `musicbrainz` -- a hit song is on
dozens of compilations and exactly one original.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

import httpx

from ..consensus import Claim, normalise
from ..resilience import DiskCache

API_ROOT = "https://api.discogs.com"
USER_AGENT = "hifi-rip/0.1.0 +https://github.com/kg2727/hifi-rip"

#: Discogs allows 60 authenticated requests a minute. Staying just inside
#: that avoids the 429s that would otherwise arrive mid-album.
MIN_INTERVAL = 1.05
REQUEST_TIMEOUT = 8.0
MAX_ATTEMPTS = 2

#: Release formats that are not the recording's own release.
COMPILATION_HINTS = frozenset({"compilation", "mixed", "dj mix", "sampler"})

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
class Release:
    discogs_id: int
    title: str            # Discogs formats these as "Artist - Title"
    artist: str | None
    year: str | None
    formats: tuple[str, ...] = ()

    @property
    def is_compilation(self) -> bool:
        return any(
            hint in fmt.lower() for fmt in self.formats for hint in COMPILATION_HINTS
        )

    def __str__(self) -> str:
        return f"{self.title} ({self.year or '?'})"


_cache = DiskCache("discogs")


def _get(
    path: str, params: dict, token: str, *, client: httpx.Client | None = None
) -> dict:
    # The token is deliberately excluded from the cache key: it is a secret,
    # and the response does not vary by which valid token asked.
    key = f"{path}?{sorted(params.items())}"
    return _cache.fetch(
        key, lambda: _get_uncached(path, params, token, client=client)
    )


def _get_uncached(
    path: str, params: dict, token: str, *, client: httpx.Client | None = None
) -> dict:
    """One request, retried once on explicit rate limiting.

    The token travels in a header rather than the query string so it cannot
    end up in a redirect target or a proxy log.
    """
    owned = client is None
    http = client or httpx.Client(timeout=REQUEST_TIMEOUT)
    headers = {"User-Agent": USER_AGENT, "Authorization": f"Discogs token={token}"}
    try:
        for attempt in range(MAX_ATTEMPTS):
            _wait_turn()
            try:
                response = http.get(f"{API_ROOT}/{path}", params=params, headers=headers)
            except (httpx.HTTPError, ValueError):
                return {}
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    return {}
            if response.status_code == 429 and attempt + 1 < MAX_ATTEMPTS:
                time.sleep(2.0)
                continue
            return {}
        return {}
    finally:
        if owned:
            http.close()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s&'-]", " ", text or "")).strip()


def _split_title(combined: str) -> tuple[str | None, str]:
    """Discogs returns "Artist - Title"; separate them without losing suffixes."""
    if " - " in combined:
        artist, _, title = combined.partition(" - ")
        return artist.strip() or None, title.strip()
    return None, combined.strip()


def search_release(
    title: str,
    artist: str | None,
    token: str,
    *,
    limit: int = 5,
    client: httpx.Client | None = None,
) -> list[Release]:
    """Find releases carrying a track with this title."""
    title = _clean(title)
    if not title or not token:
        return []

    params: dict[str, object] = {"track": title, "type": "release", "per_page": limit}
    if artist:
        cleaned = _clean(artist)
        if cleaned:
            params["artist"] = cleaned

    payload = _get("database/search", params, token, client=client)

    releases: list[Release] = []
    for item in payload.get("results") or []:
        combined = item.get("title") or ""
        parsed_artist, parsed_title = _split_title(combined)
        releases.append(Release(
            discogs_id=int(item.get("id") or 0),
            title=parsed_title,
            artist=parsed_artist,
            year=str(item.get("year")) if item.get("year") else None,
            formats=tuple(item.get("format") or ()),
        ))
    return releases


def _best(releases: list[Release]) -> Release | None:
    """Prefer an original release over the compilations a hit accumulates."""
    if not releases:
        return None
    return min(
        releases,
        key=lambda release: (
            1 if release.is_compilation else 0,
            release.year or "9999",
        ),
    )


def claims_for(
    title: str,
    artist: str | None = None,
    *,
    token: str,
    weight: float = 0.95,
    client: httpx.Client | None = None,
) -> list[Claim]:
    """Turn the best release into weighted claims, or return nothing.

    Only one release contributes. Emitting several would let this single
    source vote repeatedly for near-identical spellings and fabricate the
    appearance of agreement that the consensus rule exists to require.
    """
    if not token:
        return []

    best = _best(search_release(title, artist, token, client=client))
    if not best:
        return []

    detail = f"discogs release {best.discogs_id}"
    claims: list[Claim] = []
    if best.artist:
        claims.append(Claim("artist", best.artist, "discogs", weight, detail))
    if best.title:
        claims.append(Claim("album", best.title, "discogs", weight, detail))
    if best.year:
        claims.append(Claim("date", best.year, "discogs", weight, detail))
    return claims


def confirms(release: Release, artist: str | None) -> bool:
    """Whether a result plausibly matches what was asked for."""
    if not artist or not release.artist:
        return True
    return normalise(release.artist) == normalise(artist)
