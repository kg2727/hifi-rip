"""The 1001Tracklists source: politeness rules and parsing.

The site renders tracklists client-side, so this source yields nothing over
HTTP in practice. What is tested here is that it fails *safely* -- honouring
robots.txt, never fetching without permission, and degrading to an empty
result rather than raising -- and that the parser is correct on
server-rendered markup, which is what would be needed if that ever changes or
if a caller supplies a saved page.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from hifirip.sources import tracklists1001 as tl

SERVER_RENDERED = """
<html><body>
  <h1>Some DJ - Live at Some Festival 2026</h1>
  <div class="tlrow"><span>1</span> 0:00 Artist One - First Track</div>
  <div class="tlrow"><span>2</span> 4:35 Artist Two - Second Track (Remix)</div>
  <div class="tlrow"><span>3</span> 1:02:10 Artist Three - Third Track</div>
  <div class="tlrow"><span>4</span> 1:08:45 Fourth Track No Artist</div>
</body></html>
"""


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Never touch the real cache, the real network, or the real clock."""
    monkeypatch.setattr(tl, "CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(tl, "_robots", None)
    monkeypatch.setattr(tl, "_last_request", 0.0)
    monkeypatch.setattr(tl, "MIN_INTERVAL", 0.0)


def allow_all():
    respx.get(f"{tl.BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )


def disallow_all():
    respx.get(f"{tl.BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )


# --------------------------------------------------------------------------
# Politeness
# --------------------------------------------------------------------------

@respx.mock
def test_disallowed_paths_are_never_fetched():
    disallow_all()
    page = respx.get(f"{tl.BASE}/tracklist/x/y").mock(
        return_value=httpx.Response(200, text=SERVER_RENDERED)
    )
    assert tl.fetch(f"{tl.BASE}/tracklist/x/y") is None
    assert not page.called


@respx.mock
def test_unreadable_robots_means_do_not_crawl():
    """'Cannot tell' must not be treated as permission."""
    respx.get(f"{tl.BASE}/robots.txt").mock(side_effect=httpx.ConnectError("down"))
    assert tl.permitted(f"{tl.BASE}/tracklist/x/y") is False


@respx.mock
def test_allowed_paths_are_fetched():
    allow_all()
    respx.get(f"{tl.BASE}/tracklist/x/y").mock(
        return_value=httpx.Response(200, text=SERVER_RENDERED)
    )
    assert tl.fetch(f"{tl.BASE}/tracklist/x/y") is not None


@respx.mock
def test_blocking_status_codes_yield_nothing_rather_than_retrying():
    """403 and 429 mean stop, not try differently."""
    allow_all()
    for status in (403, 429, 503):
        respx.get(f"{tl.BASE}/tracklist/s{status}").mock(
            return_value=httpx.Response(status)
        )
        assert tl.fetch(f"{tl.BASE}/tracklist/s{status}") is None


@respx.mock
def test_responses_are_cached_so_repeats_do_not_refetch():
    allow_all()
    route = respx.get(f"{tl.BASE}/tracklist/x/y").mock(
        return_value=httpx.Response(200, text=SERVER_RENDERED)
    )
    tl.fetch(f"{tl.BASE}/tracklist/x/y")
    tl.fetch(f"{tl.BASE}/tracklist/x/y")
    assert route.call_count == 1


@respx.mock
def test_rate_limit_is_enforced_between_requests(monkeypatch):
    monkeypatch.setattr(tl, "MIN_INTERVAL", 0.2)
    allow_all()
    respx.get(url__regex=rf"{tl.BASE}/tracklist/.*").mock(
        return_value=httpx.Response(200, text=SERVER_RENDERED)
    )
    started = time.monotonic()
    tl.fetch(f"{tl.BASE}/tracklist/a/1")
    tl.fetch(f"{tl.BASE}/tracklist/b/2")
    assert time.monotonic() - started >= 0.2


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

@pytest.mark.skipif(not tl.parser_available(), reason="beautifulsoup4 not installed")
def test_server_rendered_tracklist_parses():
    result = tl.parse(SERVER_RENDERED, "https://example.invalid/x")
    assert result is not None
    assert len(result.entries) == 4
    assert [round(e.start) for e in result.entries] == [0, 275, 3730, 4125]
    assert result.entries[0].artist == "Artist One"
    assert result.entries[0].title == "First Track"


@pytest.mark.skipif(not tl.parser_available(), reason="beautifulsoup4 not installed")
def test_entries_are_sorted_and_deduplicated():
    result = tl.parse(SERVER_RENDERED)
    starts = [e.start for e in result.entries]
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts))


@pytest.mark.skipif(not tl.parser_available(), reason="beautifulsoup4 not installed")
def test_a_row_without_an_artist_separator_keeps_the_whole_name():
    result = tl.parse(SERVER_RENDERED)
    last = result.entries[-1]
    assert last.artist is None
    assert last.title == "Fourth Track No Artist"


@pytest.mark.skipif(not tl.parser_available(), reason="beautifulsoup4 not installed")
def test_a_page_with_too_few_rows_is_not_a_tracklist():
    """Two stray timestamps in prose must not become a tracklist."""
    assert tl.parse("<html><body><div>0:00 One</div></body></html>") is None


@pytest.mark.skipif(not tl.parser_available(), reason="beautifulsoup4 not installed")
def test_client_rendered_shell_yields_nothing():
    """What the live site actually returns: rows present, data absent."""
    shell = (
        "<html><body><h1>Set</h1>"
        + "".join(f'<div class="tlrow" id="tlr{i}"></div>' for i in range(30))
        + "</body></html>"
    )
    assert tl.parse(shell) is None


# --------------------------------------------------------------------------
# Failing safely
# --------------------------------------------------------------------------

@respx.mock
def test_candidates_never_raise_when_the_site_is_unreachable():
    respx.get(f"{tl.BASE}/robots.txt").mock(side_effect=httpx.ConnectError("down"))
    assert tl.candidates_for("some dj set") == []


def test_readiness_states_the_client_side_limitation():
    text = tl.readiness()
    assert "client-side" in text or "not installed" in text


def test_source_is_optional_by_design():
    """A rip must never depend on a third-party site being reachable."""
    from hifirip.sources import REGISTRY

    spec = REGISTRY["1001tracklists"]
    assert spec.requires_key is None
    assert spec.weight < 1.0
