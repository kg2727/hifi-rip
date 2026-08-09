"""Source routing, weighting, and credential gating.

Routing matters as much as weighting: consulting a source outside its
competence does not merely waste a request, it injects confident noise into
a weighted vote. Release durations are authoritative for an album and
meaningless for a DJ set, where tracks are mixed in late or looped.
"""

from __future__ import annotations

import pytest

from hifirip.content import ContentClass
from hifirip.sources import (
    REGISTRY,
    Kind,
    derivation_map,
    describe_routing,
    sources_for,
    unavailable_for,
)

ALL_KEYS = frozenset({"DISCOGS_TOKEN", "SETLISTFM_KEY", "ACOUSTID_KEY", "LASTFM_KEY"})


def names(specs) -> set[str]:
    return {spec.name for spec in specs}


# --------------------------------------------------------------------------
# Competence routing
# --------------------------------------------------------------------------

def test_setlistfm_is_only_consulted_for_live_performances():
    """It knows what a band played, and nothing about any other format."""
    assert "setlistfm" in names(
        sources_for(ContentClass.LIVE_PERFORMANCE, available_keys=ALL_KEYS))
    for other in (ContentClass.CONTINUOUS_MIX, ContentClass.DISCRETE_MULTITRACK,
                  ContentClass.SINGLE_RECORDING):
        assert "setlistfm" not in names(sources_for(other, available_keys=ALL_KEYS))


def test_1001tracklists_is_only_consulted_for_mixes():
    assert "1001tracklists" in names(sources_for(ContentClass.CONTINUOUS_MIX))
    assert "1001tracklists" not in names(sources_for(ContentClass.LIVE_PERFORMANCE))


def test_silence_detection_is_not_offered_for_mixed_sets():
    """A beatmatched mix has no silence anywhere; the source would return
    nothing and its weight would be spent on an empty answer."""
    assert "silence" in names(sources_for(ContentClass.DISCRETE_MULTITRACK))
    assert "silence" not in names(sources_for(ContentClass.CONTINUOUS_MIX))


def test_release_databases_are_not_consulted_for_mixes():
    """Studio durations bear no relation to how long a track plays in a set."""
    mix = names(sources_for(ContentClass.CONTINUOUS_MIX, available_keys=ALL_KEYS))
    assert "musicbrainz" not in mix
    assert "discogs" not in mix


def test_release_databases_are_consulted_for_albums():
    album = names(sources_for(ContentClass.DISCRETE_MULTITRACK, available_keys=ALL_KEYS))
    assert {"musicbrainz", "discogs"} <= album


def test_tracklist_sources_are_not_offered_for_single_recordings():
    single = names(sources_for(ContentClass.SINGLE_RECORDING, available_keys=ALL_KEYS))
    assert "yt_chapters" not in single
    assert "1001tracklists" not in single


def test_fingerprinting_applies_to_every_music_bearing_class():
    """It identifies what is actually playing, whatever the musical format."""
    for klass in (ContentClass.SINGLE_RECORDING, ContentClass.FRAGMENT,
                  ContentClass.DISCRETE_MULTITRACK, ContentClass.CONTINUOUS_MIX,
                  ContentClass.LIVE_PERFORMANCE, ContentClass.AMBIENT_LONGFORM):
        assert "acoustid" in names(sources_for(klass, available_keys=ALL_KEYS))


def test_speech_and_montage_get_no_music_identification():
    """Fingerprinting a documentary matches narration-covered cues at best,
    and a montage's snippets are too short and too overlaid to identify.
    Spending a weighted vote on either injects noise rather than evidence."""
    for klass in (ContentClass.SPEECH_DOMINANT, ContentClass.MONTAGE):
        assert "acoustid" not in names(sources_for(klass, available_keys=ALL_KEYS))


def test_unknown_class_routes_to_nothing():
    assert sources_for(ContentClass.UNKNOWN, available_keys=ALL_KEYS) == []


# --------------------------------------------------------------------------
# Graceful degradation
# --------------------------------------------------------------------------

def test_keyless_install_still_has_working_sources():
    keyless = sources_for(ContentClass.DISCRETE_MULTITRACK)
    assert names(keyless) >= {"musicbrainz", "yt_chapters", "yt_description"}
    assert all(not spec.needs_credentials for spec in keyless)


def test_keyed_sources_activate_when_their_key_is_present():
    without = names(sources_for(ContentClass.SINGLE_RECORDING))
    with_key = names(sources_for(ContentClass.SINGLE_RECORDING,
                                 available_keys=frozenset({"DISCOGS_TOKEN"})))
    assert "discogs" not in without
    assert "discogs" in with_key


def test_missing_keys_are_reported_rather_than_hidden():
    """A user should see what a key would buy, not silently get less."""
    missing = unavailable_for(ContentClass.SINGLE_RECORDING)
    assert "discogs" in names(missing)
    text = describe_routing(ContentClass.SINGLE_RECORDING)
    assert "DISCOGS_TOKEN" in text
    assert "Inactive" in text


def test_explicit_enable_list_narrows_the_set():
    chosen = sources_for(ContentClass.DISCRETE_MULTITRACK, enabled=("musicbrainz",))
    assert names(chosen) == {"musicbrainz"}


# --------------------------------------------------------------------------
# Weighting
# --------------------------------------------------------------------------

def test_curated_databases_outrank_free_text():
    assert REGISTRY["musicbrainz"].weight > REGISTRY["yt_description"].weight
    assert REGISTRY["yt_description"].weight > REGISTRY["yt_comments"].weight


def test_no_source_is_weighted_high_enough_to_settle_alone():
    """Weight is persuasion, not authority. Even the top source must be
    corroborated, which the consensus threshold enforces separately."""
    assert max(spec.weight for spec in REGISTRY.values()) <= 1.0


def test_results_are_ordered_most_authoritative_first():
    weights = [s.weight for s in sources_for(ContentClass.DISCRETE_MULTITRACK,
                                             available_keys=ALL_KEYS)]
    assert weights == sorted(weights, reverse=True)


def test_every_source_documents_why_it_is_weighted_as_it_is():
    for spec in REGISTRY.values():
        assert len(spec.rationale) > 40, spec.name


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------

def test_derived_sources_are_declared():
    derived = derivation_map()
    assert derived["lastfm"] == {"musicbrainz"}
    assert derived["acoustid"] == {"musicbrainz"}


def test_independent_sources_declare_no_upstream():
    assert "musicbrainz" not in derivation_map()
    assert "1001tracklists" not in derivation_map()


# --------------------------------------------------------------------------
# Registry integrity
# --------------------------------------------------------------------------

def test_registry_keys_match_source_names():
    for key, spec in REGISTRY.items():
        assert key == spec.name


@pytest.mark.parametrize("spec", list(REGISTRY.values()), ids=lambda s: s.name)
def test_every_source_is_well_formed(spec):
    assert 0.0 < spec.weight <= 1.0
    assert spec.applies_to
    assert isinstance(spec.kind, Kind)
    assert spec.derives_from <= set(REGISTRY)
