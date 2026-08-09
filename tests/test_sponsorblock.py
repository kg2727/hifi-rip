"""SponsorBlock segment handling.

Trimming discards audio permanently on the strength of crowd-sourced data,
so most of these tests are about the guards: a mis-categorised or malicious
marker must not be allowed to delete the opening of a track, and the user has
no way to notice beyond it sounding wrong.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from hifirip.sponsorblock import (
    API_ROOT,
    MAX_INTRO_SECONDS,
    Segment,
    describe,
    fetch_segments,
    hash_prefix,
    lead_in,
    non_music,
    trustworthy,
)

VIDEO_ID = "qN2KclPxWE8"


def payload(video_id: str, segments: list[dict]) -> list[dict]:
    return [{"videoID": video_id, "segments": segments}]


def seg(category: str, start: float, end: float, votes: int = 5, locked: int = 0) -> dict:
    return {"category": category, "segment": [start, end], "votes": votes,
            "locked": locked, "UUID": "x"}


# --------------------------------------------------------------------------
# Privacy-preserving lookup
# --------------------------------------------------------------------------

def test_hash_prefix_is_short_and_stable():
    prefix = hash_prefix(VIDEO_ID)
    assert len(prefix) == 4
    assert prefix == hash_prefix(VIDEO_ID)


def test_different_videos_generally_differ():
    assert hash_prefix("aaaaaaaaaaa") != hash_prefix("bbbbbbbbbbb")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

@respx.mock
def test_segments_are_parsed_and_sorted():
    respx.get(f"{API_ROOT}/{hash_prefix(VIDEO_ID)}").mock(
        return_value=httpx.Response(200, json=payload(VIDEO_ID, [
            seg("sponsor", 100.0, 130.0),
            seg("intro", 0.0, 14.5),
        ]))
    )
    segments = fetch_segments(VIDEO_ID)
    assert [s.category for s in segments] == ["intro", "sponsor"]
    assert segments[0].end == 14.5


@respx.mock
def test_other_videos_sharing_the_hash_prefix_are_discarded():
    """The prefix endpoint returns every video that shares those four chars."""
    respx.get(f"{API_ROOT}/{hash_prefix(VIDEO_ID)}").mock(
        return_value=httpx.Response(200, json=[
            {"videoID": "somethingelse", "segments": [seg("intro", 0.0, 60.0)]},
            {"videoID": VIDEO_ID, "segments": [seg("intro", 0.0, 12.0)]},
        ])
    )
    segments = fetch_segments(VIDEO_ID)
    assert len(segments) == 1
    assert segments[0].end == 12.0


@respx.mock
def test_no_submissions_is_not_an_error():
    respx.get(f"{API_ROOT}/{hash_prefix(VIDEO_ID)}").mock(
        return_value=httpx.Response(404)
    )
    assert fetch_segments(VIDEO_ID) == []


@respx.mock
def test_network_failure_never_fails_the_rip():
    """SponsorBlock is one input among many; an outage must not block a rip."""
    respx.get(f"{API_ROOT}/{hash_prefix(VIDEO_ID)}").mock(
        side_effect=httpx.ConnectError("down")
    )
    assert fetch_segments(VIDEO_ID) == []


@respx.mock
def test_malformed_entries_are_skipped_not_fatal():
    respx.get(f"{API_ROOT}/{hash_prefix(VIDEO_ID)}").mock(
        return_value=httpx.Response(200, json=payload(VIDEO_ID, [
            {"category": "intro", "segment": [5.0]},          # too few bounds
            {"category": "intro", "segment": ["a", "b"]},      # not numbers
            seg("intro", 20.0, 10.0),                          # inverted
            seg("intro", 0.0, 12.0),                           # the good one
        ]))
    )
    segments = fetch_segments(VIDEO_ID)
    assert len(segments) == 1
    assert segments[0].end == 12.0


# --------------------------------------------------------------------------
# Trust
# --------------------------------------------------------------------------

def test_downvoted_submissions_are_dropped():
    segments = [Segment("intro", 0, 10, votes=-3), Segment("intro", 0, 10, votes=2)]
    assert len(trustworthy(segments, min_votes=0)) == 1


def test_moderator_locked_segments_outrank_votes():
    locked = Segment("intro", 0, 10, votes=-5, locked=True)
    assert trustworthy([locked], min_votes=0) == [locked]


# --------------------------------------------------------------------------
# Lead-in guards -- the destructive path
# --------------------------------------------------------------------------

def test_lead_in_is_found_at_the_start():
    found = lead_in([Segment("intro", 0.0, 14.0)])
    assert found and found.end == 14.0


def test_segment_in_the_middle_is_not_a_lead_in():
    """An intro marker at 8 minutes is not a lead-in; trimming from zero to
    there would delete eight minutes of music."""
    assert lead_in([Segment("intro", 480.0, 500.0)]) is None


def test_implausibly_long_intro_is_refused():
    """A marker claiming minutes of intro is mis-categorised or vandalism."""
    assert lead_in([Segment("intro", 0.0, MAX_INTRO_SECONDS + 60)]) is None


def test_wrong_category_at_the_start_is_not_trimmed():
    assert lead_in([Segment("sponsor", 0.0, 20.0)]) is None


def test_music_offtopic_at_the_start_counts():
    found = lead_in([Segment("music_offtopic", 0.0, 13.0)])
    assert found and found.end == 13.0


def test_small_start_offset_is_tolerated():
    found = lead_in([Segment("intro", 1.2, 15.0)])
    assert found is not None


def test_no_segments_means_no_trim():
    assert lead_in([]) is None


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_non_music_selects_the_right_categories():
    segments = [
        Segment("music_offtopic", 0, 10), Segment("intro", 10, 20),
        Segment("sponsor", 20, 30),
    ]
    assert {s.category for s in non_music(segments)} == {"music_offtopic", "sponsor"}


def test_describe_handles_the_empty_case():
    assert "No SponsorBlock segments" in describe([])


def test_describe_lists_segments_with_votes():
    text = describe([Segment("intro", 0.0, 14.0, votes=7)])
    assert "intro" in text and "+7 votes" in text
