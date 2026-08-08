"""Content classification.

Fixtures are trimmed from real yt-dlp output. The continuous-mix case is a
three-hour DJ set with no chapters and no description timestamps -- the case
that showed the front half of the source chain can come back entirely empty.
"""

from __future__ import annotations

import pytest

from hifirip.content import (
    CONFIDENT_MASS,
    UNCERTAIN_BELOW,
    ContentClass,
    classify,
    count_timestamps,
)

DJ_SET = {
    "title": "Tiësto - Live from Atomium Brussels",
    "duration": 10776.0,
    "chapters": [],
    "description": "Tiësto live from the Atomium in Brussels.",
    "categories": ["Music"],
}

MUSIC_VIDEO = {
    "title": "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)",
    "duration": 213.0,
    "chapters": [],
    "description": "The official video.",
    "track": "Never Gonna Give You Up",
    "album": "Whenever You Need Somebody",
}

ALBUM_UPLOAD = {
    "title": "Some Band - Some Record (Full Album)",
    "duration": 2580.0,
    "chapters": [
        {"start_time": i * 258.0, "end_time": (i + 1) * 258.0, "title": f"Track {i+1}"}
        for i in range(10)
    ],
    "description": "Full album stream.",
}

BARE_LONG_UPLOAD = {
    "title": "untitled upload",
    "duration": 3000.0,
    "chapters": [],
    "description": "",
}


def test_dj_set_is_a_continuous_mix():
    result = classify(DJ_SET)
    assert result.content_class is ContentClass.CONTINUOUS_MIX
    assert result.is_multitrack
    assert not result.has_deterministic_boundaries


def test_music_video_is_a_single_track():
    result = classify(MUSIC_VIDEO)
    assert result.content_class is ContentClass.SINGLE_TRACK
    assert not result.is_multitrack


def test_album_upload_with_chapters_is_discrete():
    result = classify(ALBUM_UPLOAD)
    assert result.content_class is ContentClass.DISCRETE_ALBUM
    assert result.is_multitrack
    assert result.has_deterministic_boundaries


# --------------------------------------------------------------------------
# Confidence calibration
# --------------------------------------------------------------------------

def test_single_weak_signal_does_not_yield_full_confidence():
    """Unopposed is not the same as well-supported.

    Low confidence is what makes the tool ask rather than assume, so an
    overconfident score suppresses the very questions the user asked for.
    """
    lone_signal = {"title": "x", "duration": 200.0, "chapters": [], "description": ""}
    result = classify(lone_signal)
    assert result.content_class is ContentClass.SINGLE_TRACK
    assert 0.0 < result.confidence < 0.8


def test_corroborated_classification_is_confident():
    result = classify(DJ_SET)
    # Title plus runtime, independently pointing the same way.
    assert result.confidence >= 0.9
    assert len(result.supporting()) >= 2


def test_confidence_never_exceeds_one():
    for fixture in (DJ_SET, MUSIC_VIDEO, ALBUM_UPLOAD):
        assert 0.0 <= classify(fixture).confidence <= 1.0


def test_weak_lone_signal_is_unknown_not_a_guess():
    """Runtime alone must not name a class.

    A 50-minute upload with no title cue, no chapters, and no timestamps
    tells us almost nothing; calling it an album would route sources and a
    splitting policy off a coin flip.
    """
    result = classify(BARE_LONG_UPLOAD)
    assert result.content_class is ContentClass.UNKNOWN
    assert result.confidence < UNCERTAIN_BELOW
    # The evidence is kept so the briefing can still explain itself.
    assert result.signals
    assert "confirmed rather than assumed" in result.briefing()


def test_empty_metadata_does_not_raise():
    assert classify({}).content_class is ContentClass.UNKNOWN


# --------------------------------------------------------------------------
# Briefings must be specific to the rip in hand
# --------------------------------------------------------------------------

def test_mix_briefing_names_what_was_missing_here():
    briefing = classify(DJ_SET).briefing()
    assert "180-minute" in briefing
    assert "no chapter markers" in briefing
    assert "no timestamps in the description" in briefing
    # The tradeoff, stated in terms of consequences rather than jargon.
    assert "both tracks are genuinely playing at once" in briefing
    assert "reproduces the original mix exactly" in briefing


def test_album_briefing_reports_stated_boundaries():
    briefing = classify(ALBUM_UPLOAD).briefing()
    assert "10 chapter markers" in briefing
    assert "stated rather than guessed" in briefing


def test_single_track_briefing_does_not_discuss_splitting_tradeoffs():
    briefing = classify(MUSIC_VIDEO).briefing()
    assert "No splitting applies" in briefing
    assert "crossfade" not in briefing.lower()


def test_briefings_cite_their_evidence():
    for fixture in (DJ_SET, ALBUM_UPLOAD, MUSIC_VIDEO):
        assert "Classified from:" in classify(fixture).briefing()


# --------------------------------------------------------------------------
# Timestamp counting
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("0:00 One\n3:12 Two\n7:45 Three", 3),
        ("00:00 One\n1:03:12 Two", 2),
        ("(0:00) One\n(4:20) Two", 2),
        ("no timestamps at all", 0),
        ("", 0),
        # Mid-line times are references, not a tracklist.
        ("see 3:12 for the drop", 0),
    ],
)
def test_count_timestamps(description, expected):
    assert count_timestamps(description) == expected


def test_confident_mass_is_documented_not_magic():
    assert CONFIDENT_MASS > 0
