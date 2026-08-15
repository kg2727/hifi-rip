"""1001Tracklists: the community record of what a DJ actually played.

There is no public API, so this scrapes. That deserves its constraints spelled
out, because a scraper that ignores them is a scraper that gets the project
blocked and the site burdened:

* **robots.txt is fetched and honoured** before any page request. If the site
  disallows a path, this returns nothing rather than deciding it knows better.
* **One request per five seconds**, process-wide. Tracklist lookups are not
  latency-critical -- a rip already takes minutes -- so there is no reason to
  be aggressive.
* **Responses are cached on disk.** Re-running a rip, or re-scoring a corpus,
  must not re-fetch. Most repeat traffic from a tool like this is avoidable.
* **Blocking is accepted, never worked around.** Bot protection, a 403, or a
  challenge page all resolve to "no data". This source is one weighted vote
  among many and the pipeline is designed to run without it.

Weighted 0.90 in the registry -- the joint-highest tracklist source -- but
deliberately not high enough to settle a boundary alone. Entries are
community-submitted, and an unreviewed submission should not by itself decide
what a user's library says.

The parser needs BeautifulSoup, which is an optional extra:

    pip install "hifi-rip[scrape]"

Without it this source reports itself unavailable rather than failing a rip.

## Measured limitation: the data is not in the HTML

robots.txt permits both the search and tracklist paths, and pages return 200
to a plain client. The content, however, is not there. Measured on a real
tracklist page: 299KB of HTML containing 42 row containers, an "enable
JavaScript" notice, and **nine** cue-time strings where a DJ set carries
twenty to forty. The search endpoint likewise returns 82KB with no tracklist
links at all. Rows are server-rendered as empty shells and populated client
side.

So this source yields nothing over HTTP, and `candidates_for` returns an
empty list. Everything below still works on server-rendered tracklist HTML,
so it remains useful if the site changes or if a caller supplies a saved
page, and the pipeline is unaffected either way: this is one optional vote
among many.

Extracting the data would require driving a headless browser. That is
deliberately not done here. robots.txt grants permission to *crawl paths*; it
says nothing about executing a site's JavaScript to obtain content it does
not serve to HTTP clients, which is a materially different act. It would also
add a browser-sized dependency and seconds of latency per lookup. If that
trade is ever wanted it should be an explicit, separately-reasoned decision,
not a silent escalation inside a metadata source.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import httpx

from .youtube import TrackCandidate

BASE = "https://www.1001tracklists.com"
USER_AGENT = "hifi-rip/0.1.0 (+https://github.com/kg2727/hifi-rip)"

#: Deliberately slow. Nothing here is latency-critical.
MIN_INTERVAL = 5.0
REQUEST_TIMEOUT = 15.0

CACHE_ROOT = Path.home() / ".cache" / "hifi-rip" / "1001tracklists"
#: Tracklists are edited for a while after an event, then settle.
CACHE_TTL = 7 * 24 * 3600

#: Cue times look like "1:02:33" or "12:05" in a tracklist row.
CUE = re.compile(r"\b((?:\d{1,2}:)?\d{1,2}:\d{2})\b")

_throttle = threading.Lock()
_last_request = 0.0
_robots: urllib.robotparser.RobotFileParser | None = None
_robots_lock = threading.Lock()


class Unavailable(RuntimeError):
    """The parser dependency is absent."""


def parser_available() -> bool:
    try:
        import bs4  # noqa: F401
    except ImportError:
        return False
    return True


def readiness() -> str:
    """Why this source is or is not going to produce anything."""
    if not parser_available():
        return (
            "BeautifulSoup not installed -- 1001Tracklists cannot be parsed. "
            'Install with: pip install "hifi-rip[scrape]"'
        )
    return (
        "parser present, but 1001Tracklists renders tracklists client-side: "
        "pages return 200 with empty row shells, so HTTP scraping yields "
        "nothing. See the module docstring for the measurement."
    )


# ---------------------------------------------------------------------------
# Politeness
# ---------------------------------------------------------------------------

def _wait_turn() -> None:
    global _last_request
    with _throttle:
        elapsed = time.monotonic() - _last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        _last_request = time.monotonic()


def robots() -> urllib.robotparser.RobotFileParser | None:
    """Fetch and cache robots.txt. A failure to read it means we do not crawl."""
    global _robots
    with _robots_lock:
        if _robots is not None:
            return _robots
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(urljoin(BASE, "/robots.txt"))
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                response = client.get(
                    urljoin(BASE, "/robots.txt"),
                    headers={"User-Agent": USER_AGENT},
                )
            if response.status_code != 200:
                return None
            parser.parse(response.text.splitlines())
        except httpx.HTTPError:
            return None
        _robots = parser
        return _robots


def permitted(url: str) -> bool:
    """Whether robots.txt allows this path.

    An unreadable robots.txt returns False. Treating "cannot tell" as
    permission is how well-meaning crawlers end up unwelcome.
    """
    rules = robots()
    if rules is None:
        return False
    return rules.can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path(url: str) -> Path:
    import hashlib

    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return CACHE_ROOT / f"{digest}.html"


def _cached(url: str) -> str | None:
    path = _cache_path(url)
    if not path.is_file():
        return None
    if time.time() - path.stat().st_mtime > CACHE_TTL:
        return None
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _store(url: str, body: str) -> None:
    try:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        _cache_path(url).write_text(body)
    except OSError:
        pass


def fetch(url: str, *, client: httpx.Client | None = None) -> str | None:
    """Fetch one page, honouring cache, robots.txt and the rate limit."""
    body = _cached(url)
    if body is not None:
        return body
    if not permitted(url):
        return None

    _wait_turn()
    owned = client is None
    http = client or httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True)
    try:
        response = http.get(url, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError:
        return None
    finally:
        if owned:
            http.close()

    # 403 and 429 mean "stop", not "try differently".
    if response.status_code != 200:
        return None
    _store(url, response.text)
    return response.text


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Tracklist:
    url: str
    title: str
    entries: list[TrackCandidate]


def _seconds(stamp: str) -> float | None:
    parts = [int(p) for p in stamp.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def parse(html: str, url: str = "") -> Tracklist | None:
    """Extract cue times and track names from a tracklist page.

    Selectors are intentionally loose. A scraper pinned to one site's exact
    class names breaks on the next redesign, and a tracklist that silently
    becomes empty is worse than one that fails loudly -- so parsing looks for
    the *shape* of the data (a row containing a cue time and a track name)
    rather than a specific DOM path.
    """
    if not parser_available():
        raise Unavailable(readiness())
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(["h1", "title"])
    title = heading.get_text(strip=True) if heading else ""

    entries: list[TrackCandidate] = []
    seen: set[float] = set()

    for row in soup.find_all(["div", "tr", "li"]):
        text = row.get_text(" ", strip=True)
        if not text or len(text) > 300:
            continue
        cue = CUE.search(text)
        if not cue:
            continue
        start = _seconds(cue.group(1))
        if start is None or start in seen:
            continue

        # Everything after the cue time is the track name.
        remainder = text[cue.end():].strip(" -–—|")
        remainder = re.sub(r"\s+", " ", remainder)
        if len(remainder) < 3:
            continue

        artist, _, track = remainder.partition(" - ")
        if not track:
            artist, track = "", remainder
        entries.append(TrackCandidate(
            start=start,
            title=track.strip()[:200],
            artist=(artist.strip() or None),
            source="1001tracklists",
        ))
        seen.add(start)

    if len(entries) < 3:
        return None
    entries.sort(key=lambda entry: entry.start)
    return Tracklist(url=url, title=title, entries=entries)


def search(query: str, *, client: httpx.Client | None = None) -> list[str]:
    """Find candidate tracklist URLs for a set."""
    url = f"{BASE}/search/result.php?search_selection=9&main_search={quote_plus(query)}"
    html = fetch(url, client=client)
    if not html or not parser_available():
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/tracklist/" in href:
            absolute = urljoin(BASE, href)
            if absolute not in found:
                found.append(absolute)
    return found[:5]


def candidates_for(
    query: str, *, client: httpx.Client | None = None
) -> list[TrackCandidate]:
    """Best-effort tracklist for a set description.

    Returns [] for every failure mode -- blocked, missing parser, no match,
    unparseable page -- because this source is optional by design and a rip
    must never depend on a third-party site being reachable.
    """
    if not parser_available():
        return []
    try:
        for url in search(query, client=client):
            html = fetch(url, client=client)
            if not html:
                continue
            tracklist = parse(html, url)
            if tracklist:
                return tracklist.entries
    except (Unavailable, httpx.HTTPError):
        return []
    return []
