"""Content classification against real observed metadata.

Every fixture below is the metadata a real yt-dlp probe returned during
development, recorded verbatim. They are the six videos that drove the
taxonomy rebuild, including the two the first classifier got wrong.

The taxonomy itself is derived from a 720-item survey; see
`tools/survey_content.py` and the module docstring in `hifirip.content`.
"""

from __future__ import annotations

import pytest

from hifirip.content import (
    AMBIGUOUS_MINUTES,
    HANDLING,
    UNCERTAIN_BELOW,
    ContentClass,
    apply_host_decision,
    classify,
    count_timestamps,
)

# --- real probe results, recorded verbatim --------------------------------

MUSIC_VIDEO = {
    "title": "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)",
    "duration": 213.0, "chapters": [], "description": "",
    "track": "Never Gonna Give You Up", "album": "Whenever You Need Somebody",
}
DJ_SET = {
    "title": "Tiësto - Live from Atomium Brussels",
    "duration": 10776.0, "chapters": [], "description": "",
}
SHORT = {
    "title": "When the lyrics hit just as hard as the drop ✨❤️",
    "duration": 33.0, "chapters": [], "description": "",
}
ARMIN_SET = {
    "title": "Armin van Buuren WE2 | Tomorrowland 2026",
    "duration": 3741.0, "chapters": [], "description": "",
}
AFTERMOVIE = {
    "title": "Tomorrowland Belgium 2026 | Official Aftermovie",
    "duration": 1303.0, "chapters": [], "description": "",
}
DOCUMENTARY = {
    "title": "We Are Tomorrow 2025 l Documentary",
    "duration": 1498.0, "chapters": [], "description": "",
}
ALBUM_UPLOAD = {
    "title": "Some Band - Some Record (Full Album)",
    "duration": 2580.0,
    "chapters": [
        {"start_time": i * 258.0, "end_time": (i + 1) * 258.0, "title": f"T{i+1}"}
        for i in range(10)
    ],
    "description": "",
}


# --- the cases the first classifier got wrong -----------------------------

def test_documentary_is_not_a_single_track():
    """Previously mislabelled single-track, missing the 25-minute cutoff by
    two seconds. The title says 'Documentary'; runtime should not get a vote.
    """
    result = classify(DOCUMENTARY)
    assert result.content_class is ContentClass.SPEECH_DOMINANT
    assert result.handling.splits is False


def test_aftermovie_is_a_montage_not_a_mix():
    """Previously routed to DJ-set handling, which would have tried to split
    a montage of snippets into hundreds of fragments.
    """
    result = classify(AFTERMOVIE)
    assert result.content_class is ContentClass.MONTAGE
    assert result.handling.splits is False


def test_short_is_a_fragment_not_a_whole_track():
    """No lexical cue in the title at all -- runtime alone must carry this,
    and it is the one range where the survey showed runtime is decisive.
    """
    result = classify(SHORT)
    assert result.content_class is ContentClass.FRAGMENT
    assert "source recording" in result.handling.splitting_note


# --- correct classifications ----------------------------------------------

def test_official_music_video_is_a_single_recording():
    result = classify(MUSIC_VIDEO)
    assert result.content_class is ContentClass.SINGLE_RECORDING
    assert result.confidence > 0.8


def test_album_upload_with_track_length_chapters():
    result = classify(ALBUM_UPLOAD)
    assert result.content_class is ContentClass.DISCRETE_MULTITRACK
    assert result.has_deterministic_boundaries


# --- deferral rather than guessing ----------------------------------------

@pytest.mark.parametrize("fixture", [DJ_SET, ARMIN_SET], ids=["tiesto", "armin"])
def test_dj_sets_whose_titles_do_not_say_so_escalate(fixture):
    """Both are DJ sets, and neither title establishes it.

    'Live from Atomium Brussels' reads identically to a concert billing, and
    the survey put concerts and DJ sets at 95% duration overlap, so nothing
    in the metadata breaks the tie. Deferring is the correct outcome; naming
    a class here would pick a pipeline by coin flip.
    """
    result = classify(fixture)
    assert result.content_class is ContentClass.UNKNOWN
    assert result.needs_escalation
    assert result.confidence < UNCERTAIN_BELOW


def test_escalation_brief_carries_the_evidence_and_the_options():
    brief = classify(ARMIN_SET).escalation_brief()
    assert "Armin van Buuren" in brief
    assert "duration_minutes: 62.4" in brief
    for klass in (ContentClass.CONTINUOUS_MIX, ContentClass.LIVE_PERFORMANCE):
        assert klass.value in brief
    # It must warn the host off the axis that produced the original bugs.
    assert "95%" in brief
    assert "poor discriminator" in brief


def test_escalation_brief_invites_further_deferral():
    """A host that is also unsure must be told to say so, not to guess."""
    assert "rather than guessing" in classify(ARMIN_SET).escalation_brief()


def test_host_decision_is_recorded_as_such():
    result = apply_host_decision(
        classify(ARMIN_SET), ContentClass.CONTINUOUS_MIX, "title names a festival stage"
    )
    assert result.content_class is ContentClass.CONTINUOUS_MIX
    assert not result.needs_escalation
    assert "host agent" in result.briefing()


def test_audio_probe_plan_skips_the_unrepresentative_opening():
    plan = classify(DJ_SET).audio_probe_plan(windows=6)
    assert len(plan) == 6
    assert plan[0] >= 60.0            # past intros and channel idents
    assert plan[-1] < 10776.0         # a full window remains
    assert plan == sorted(plan)


def test_audio_probe_plan_handles_very_short_input():
    assert classify(SHORT).audio_probe_plan() in ([0.0], [])


# --- duration must not drive decisions in the ambiguous band --------------

def test_runtime_alone_never_names_a_class_in_the_ambiguous_band():
    """The survey found 84-95% overlap here between classes needing
    different pipelines, so runtime must abstain rather than vote.
    """
    for minutes in (10, 25, 45, 60, 90, 120):
        bare = {"title": "untitled upload", "duration": minutes * 60.0,
                "chapters": [], "description": ""}
        result = classify(bare)
        assert result.content_class is ContentClass.UNKNOWN, minutes


def test_ambiguous_band_signal_is_recorded_as_uninformative():
    bare = {"title": "untitled", "duration": 3600.0, "chapters": [], "description": ""}
    signals = classify(bare).signals
    duration_signals = [s for s in signals if s.name == "duration"]
    assert duration_signals and all(s.weight == 0.0 for s in duration_signals)
    assert all(s.supports is None for s in duration_signals)


def test_ambiguous_band_matches_the_surveyed_range():
    assert AMBIGUOUS_MINUTES == (4.0, 130.0)


def test_empty_metadata_does_not_raise():
    assert classify({}).content_class is ContentClass.UNKNOWN


# --- briefings ------------------------------------------------------------

def test_every_class_has_handling_defined():
    for klass in ContentClass:
        assert klass in HANDLING
        assert HANDLING[klass].summary


def test_mix_briefing_explains_the_tradeoff_in_consequences():
    result = apply_host_decision(classify(DJ_SET), ContentClass.CONTINUOUS_MIX)
    briefing = result.briefing()
    assert "both tracks are genuinely playing at once" in briefing
    assert "reproduces the mix exactly" in briefing
    assert "no chapter markers" in briefing


def test_live_performance_briefing_names_applause_not_silence():
    fixture = {"title": "Band - Full Concert Live", "duration": 6000.0,
               "chapters": [], "description": ""}
    result = classify(fixture)
    assert result.content_class is ContentClass.LIVE_PERFORMANCE
    assert "applause" in result.briefing()
    assert "setlistfm" in result.handling.sources


def test_speech_dominant_briefing_declines_to_extract_tracks():
    briefing = classify(DOCUMENTARY).briefing()
    assert "mostly talking" in briefing
    assert "Chapter markers are offered instead" in briefing


def test_single_recording_briefing_does_not_discuss_splitting():
    briefing = classify(MUSIC_VIDEO).briefing()
    assert "No splitting applies" in briefing
    assert "crossfade" not in briefing.lower()


# --- timestamps -----------------------------------------------------------

@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("0:00 One\n3:12 Two\n7:45 Three", 3),
        ("00:00 One\n1:03:12 Two", 2),
        ("(0:00) One\n(4:20) Two", 2),
        ("no timestamps at all", 0),
        ("", 0),
        ("see 3:12 for the drop", 0),
    ],
)
def test_count_timestamps(description, expected):
    assert count_timestamps(description) == expected
