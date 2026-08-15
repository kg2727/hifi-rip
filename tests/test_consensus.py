"""Consensus reconciliation.

The load-bearing rule here is that authority never substitutes for
corroboration: a value backed by exactly one source is a lead, not a
conclusion, no matter how reliable that source is. Without it, the single
highest-weighted database silently decides every field, which is precisely
the failure the weighting was meant to avoid.
"""

from __future__ import annotations

import pytest

from hifirip.consensus import (
    Claim,
    Status,
    apply_judgement,
    equivalent,
    normalise,
    reconcile,
    reconcile_all,
)


def claim(source: str, value: str, weight: float, fieldname: str = "artist") -> Claim:
    return Claim(field=fieldname, value=value, source=source, weight=weight)


# --------------------------------------------------------------------------
# Normalisation: compare loosely, report faithfully
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("The Beatles", "the beatles"),
        ("Beyoncé", "Beyonce"),
        ("Song (Official Video)", "Song"),
        ("Song [HD]", "Song"),
        ("Song (Remastered 2011)", "Song"),
        ("A feat. B", "A ft B"),
        ("A  spaced   out", "A spaced out"),
        ("Don't Stop", "Dont Stop"),
        ("Track — Name", "Track Name"),
    ],
)
def test_trivial_differences_count_as_agreement(left, right):
    assert equivalent(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Song One", "Song Two"),
        ("The Beatles", "The Beach Boys"),
        ("Live Version", "Studio Version"),
    ],
)
def test_real_differences_are_preserved(left, right):
    assert not equivalent(left, right)


def test_normalisation_never_decides_what_the_user_sees():
    """The original spelling must survive into the resolved value."""
    resolution = reconcile([
        claim("musicbrainz", "Beyoncé", 1.0),
        claim("discogs", "Beyonce", 0.95),
    ])
    assert resolution.value == "Beyoncé"      # the higher-weighted spelling
    assert normalise(resolution.value) == "beyonce"


# --------------------------------------------------------------------------
# The no-single-source rule
# --------------------------------------------------------------------------

def test_one_source_is_a_lead_not_a_consensus():
    resolution = reconcile([claim("musicbrainz", "A Band", 1.0)])
    assert resolution.status is Status.SINGLE_SOURCE
    assert resolution.needs_judgement
    assert not resolution.settled


def test_the_most_authoritative_source_still_cannot_decide_alone():
    """1001Tracklists is weighted 0.90 precisely so it cannot settle a field
    by itself; this pins that it does not."""
    resolution = reconcile([claim("1001tracklists", "Some Track", 0.90)])
    assert resolution.status is Status.SINGLE_SOURCE


def test_two_independent_sources_settle_it():
    resolution = reconcile([
        claim("1001tracklists", "Some Track", 0.90),
        claim("yt_comments", "Some Track", 0.40),
    ])
    assert resolution.status is Status.AGREED
    assert resolution.independent_sources == 2


def test_threshold_is_configurable_for_stricter_rips():
    claims = [
        claim("musicbrainz", "X", 1.0),
        claim("discogs", "X", 0.95),
    ]
    assert reconcile(claims, min_sources=3).status is Status.SINGLE_SOURCE
    assert reconcile(claims, min_sources=2).status is Status.AGREED


# --------------------------------------------------------------------------
# Independence
# --------------------------------------------------------------------------

def test_a_mirror_does_not_corroborate_its_upstream():
    """Last.fm largely restates MusicBrainz; counting both would manufacture
    agreement that was never there."""
    resolution = reconcile(
        [claim("musicbrainz", "A Band", 1.0), claim("lastfm", "A Band", 0.55)],
        independent_of={"lastfm": {"musicbrainz"}},
    )
    assert resolution.status is Status.SINGLE_SOURCE


def test_a_mirror_counts_when_its_upstream_is_silent():
    resolution = reconcile(
        [claim("lastfm", "A Band", 0.55), claim("discogs", "A Band", 0.95)],
        independent_of={"lastfm": {"musicbrainz"}},
    )
    assert resolution.status is Status.AGREED


# --------------------------------------------------------------------------
# Disagreement
# --------------------------------------------------------------------------

def test_split_evidence_is_conflicted_not_silently_resolved():
    resolution = reconcile([
        claim("musicbrainz", "Version A", 1.0),
        claim("discogs", "Version B", 0.95),
    ])
    assert resolution.status is Status.CONFLICTED
    assert resolution.needs_judgement
    assert resolution.dissenting


def test_a_clear_majority_wins():
    resolution = reconcile([
        claim("musicbrainz", "Right", 1.0),
        claim("discogs", "Right", 0.95),
        claim("yt_comments", "Wrong", 0.40),
    ])
    assert resolution.status is Status.AGREED
    assert resolution.value == "Right"
    assert resolution.confidence > 0.6


def test_weight_beats_headcount():
    """Two weak sources should not outvote the curated databases."""
    resolution = reconcile([
        claim("musicbrainz", "Right", 1.0),
        claim("discogs", "Right", 0.95),
        claim("yt_comments", "Wrong", 0.40),
        claim("yt_description", "Wrong", 0.60),
    ])
    assert resolution.value == "Right"


def test_empty_and_blank_claims_are_ignored():
    resolution = reconcile([
        claim("musicbrainz", "", 1.0),
        claim("discogs", "   ", 0.95),
    ])
    assert resolution.status is Status.ABSENT
    assert resolution.value is None


def test_no_claims_at_all():
    assert reconcile([]).status is Status.ABSENT


# --------------------------------------------------------------------------
# The escalation brief
# --------------------------------------------------------------------------

def test_brief_omits_fields_the_arithmetic_settled():
    report = reconcile_all([
        claim("musicbrainz", "A Band", 1.0, "artist"),
        claim("discogs", "A Band", 0.95, "artist"),
        claim("musicbrainz", "Title A", 1.0, "title"),
        claim("discogs", "Title B", 0.95, "title"),
    ])
    brief = report.brief()
    assert "FIELD: title" in brief
    assert "FIELD: artist" not in brief
    assert "do not revisit" in brief.lower()


def test_brief_explains_why_a_single_source_is_insufficient():
    report = reconcile_all([claim("1001tracklists", "Some Track", 0.90, "title")])
    brief = report.brief()
    assert "lead, not a consensus" in brief
    assert "independently plausible" in brief


def test_brief_tells_the_host_to_prefer_independence_over_confidence():
    report = reconcile_all([
        claim("musicbrainz", "A", 1.0, "title"),
        claim("discogs", "B", 0.95, "title"),
    ])
    assert "independent" in report.brief()
    assert "leave it unset" in report.brief()


def test_nothing_to_escalate_is_stated_plainly():
    report = reconcile_all([
        claim("musicbrainz", "A Band", 1.0),
        claim("discogs", "A Band", 0.95),
    ])
    assert not report.needs_host
    assert "no judgement needed" in report.brief()


# --------------------------------------------------------------------------
# Folding judgement back in
# --------------------------------------------------------------------------

def test_host_ruling_settles_a_field():
    report = reconcile_all([
        claim("musicbrainz", "A", 1.0, "title"),
        claim("discogs", "B", 0.95, "title"),
    ])
    apply_judgement(report, {"title": "A"})
    assert report.resolutions[0].value == "A"
    assert report.resolutions[0].settled
    assert not report.needs_host


def test_a_field_the_host_declined_stays_unset():
    """Falling back to the leading candidate would defeat the purpose of
    asking: the leading candidate was what we did not trust."""
    report = reconcile_all([
        claim("musicbrainz", "A", 1.0, "title"),
        claim("discogs", "B", 0.95, "title"),
    ])
    apply_judgement(report, {"title": None})
    assert report.resolutions[0].value is None
    assert report.resolutions[0].status is Status.ABSENT


# --------------------------------------------------------------------------
# Per-field corroboration policy
#
# The line is drawn on consequence: a value that determines where a file
# lands needs corroboration, because getting it wrong misplaces the track
# rather than merely mislabelling it. A value that only describes the file
# does not, because a wrong year is corrected in place in seconds -- and
# measured across 273 uploads, independent corroboration exists for only
# ~18%, so a blanket rule leaves descriptive fields empty on most rips.
# --------------------------------------------------------------------------

def test_descriptive_field_settles_on_one_source():
    resolution = reconcile_all([claim("discogs", "1987", 0.95, "date")]).resolutions[0]
    assert resolution.settled
    assert resolution.accepted_on_single_source
    assert resolution.required_sources == 1


@pytest.mark.parametrize("fieldname", ["title", "artist", "album", "albumartist"])
def test_path_determining_fields_still_require_corroboration(fieldname):
    resolution = reconcile_all(
        [claim("musicbrainz", "Something", 1.0, fieldname)]
    ).resolutions[0]
    assert resolution.status is Status.SINGLE_SOURCE
    assert resolution.needs_judgement
    assert resolution.required_sources == 2


def test_unknown_fields_get_the_strict_default():
    resolution = reconcile_all(
        [claim("musicbrainz", "x", 1.0, "some_new_field")]
    ).resolutions[0]
    assert resolution.required_sources == 2
    assert resolution.status is Status.SINGLE_SOURCE


def test_a_corroborated_descriptive_field_is_not_marked_sole():
    resolution = reconcile_all([
        claim("musicbrainz", "1987", 1.0, "date"),
        claim("discogs", "1987", 0.95, "date"),
    ]).resolutions[0]
    assert resolution.settled
    assert not resolution.accepted_on_single_source


def test_relaxed_policy_does_not_rescue_a_genuine_conflict():
    """Disagreement is still disagreement, whatever the field costs."""
    resolution = reconcile_all([
        claim("musicbrainz", "1987", 1.0, "date"),
        claim("discogs", "1999", 0.95, "date"),
    ]).resolutions[0]
    assert resolution.status is Status.CONFLICTED
    assert resolution.needs_judgement


def test_explicit_override_applies_uniformly():
    """A caller wanting uniform strictness can still ask for it."""
    resolution = reconcile_all(
        [claim("discogs", "1987", 0.95, "date")], min_sources=2
    ).resolutions[0]
    assert resolution.status is Status.SINGLE_SOURCE


def test_single_source_brief_explains_the_filing_consequence():
    brief = reconcile_all([claim("musicbrainz", "X", 1.0, "album")]).brief()
    assert "where the file is filed" in brief
    assert "misplaces the track" in brief


def test_sole_acceptance_is_visible_in_the_description():
    resolution = reconcile_all([claim("discogs", "1987", 0.95, "date")]).resolutions[0]
    assert "single source" in resolution.describe()
