"""Evaluation scoring.

Two rules make these numbers mean anything, and both are pinned here: a
source used to derive a label may not also be an input when scoring against
it, and a constant lead-in offset is corrected before scoring rather than
charged against every boundary.
"""

from __future__ import annotations

import pytest

from hifirip.content import ContentClass
from hifirip.evaluation import (
    MIN_DERIVATIONS,
    TOLERANCE,
    Fixture,
    Regime,
    best_fit_offset,
    evaluate,
    score_boundaries,
    score_fields,
)

ALL_SOURCES = {"musicbrainz", "discogs", "yt_chapters", "yt_comments", "silence"}


def album_fixture(**overrides) -> Fixture:
    base = dict(
        video_id="album1",
        content_class=ContentClass.DISCRETE_MULTITRACK,
        regime=Regime.RELEASE_DURATIONS,
        boundaries=[0.0, 200.0, 420.0, 610.0],
        derived_from={"musicbrainz"},
        derivations=2,
        duration=800.0,
    )
    base.update(overrides)
    return Fixture(**base)


# --------------------------------------------------------------------------
# Leave one source out
# --------------------------------------------------------------------------

def test_a_source_that_produced_the_label_cannot_also_be_an_input():
    """Otherwise we grade the exam with the answer key."""
    fixture = album_fixture(derived_from={"musicbrainz", "discogs"})
    permitted = fixture.permitted_sources(ALL_SOURCES)
    assert "musicbrainz" not in permitted
    assert "discogs" not in permitted
    assert "yt_chapters" in permitted


def test_permitted_sources_are_passed_to_the_predictor_not_filtered_after():
    """The excluded source must never contribute to the prediction at all."""
    seen = {}

    def predict(fixture, permitted):
        seen["permitted"] = permitted
        return fixture.boundaries

    evaluate([album_fixture()], predict, all_sources=ALL_SOURCES)
    assert "musicbrainz" not in seen["permitted"]


def test_weakly_derived_fixtures_are_skipped_not_scored():
    """One derivation is an assertion, not ground truth."""
    fixture = album_fixture(derivations=1)
    reports = evaluate([fixture], lambda f, p: f.boundaries, all_sources=ALL_SOURCES)
    assert reports[0].skipped == ["album1"]
    assert not reports[0].scores


def test_min_derivations_is_at_least_two():
    assert MIN_DERIVATIONS >= 2


# --------------------------------------------------------------------------
# Offset correction
# --------------------------------------------------------------------------

def test_a_constant_lead_in_is_not_counted_as_error():
    """Album uploads routinely start late; that shifts every boundary
    equally and is benign."""
    truth = [0.0, 200.0, 420.0, 610.0]
    shifted = [t + 13.5 for t in truth]
    score = score_boundaries(shifted, truth, tolerance=2.0)
    assert score.matched == 4
    assert score.offset == pytest.approx(13.5, abs=0.01)
    assert score.median_residual < 0.1


def test_offset_is_robust_to_a_spurious_boundary():
    truth = [0.0, 200.0, 420.0, 610.0]
    predicted = [10.0, 210.0, 430.0, 620.0, 55.0]   # one bogus entry
    assert best_fit_offset(predicted, truth) == pytest.approx(10.0, abs=1.0)


def test_growing_error_is_flagged_as_drift():
    """A constant shift is a lead-in; growing error is a different master."""
    truth = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0]
    predicted = [0.0, 101.0, 203.0, 307.0, 413.0, 521.0]
    score = score_boundaries(predicted, truth, tolerance=30.0)
    assert score.matched == 6
    assert score.drifting


def test_flat_error_is_not_flagged_as_drift():
    truth = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0]
    predicted = [t + 5.0 for t in truth]
    assert not score_boundaries(predicted, truth, tolerance=30.0).drifting


def test_offset_correction_can_be_disabled():
    truth = [0.0, 100.0]
    predicted = [20.0, 120.0]
    score = score_boundaries(predicted, truth, tolerance=5.0, correct_offset=False)
    assert score.matched == 0


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def test_each_ground_truth_boundary_can_only_be_matched_once():
    """Two predictions crowding one boundary must not both score as hits."""
    truth = [100.0]
    score = score_boundaries([99.0, 101.0], truth, tolerance=5.0)
    assert score.matched == 1
    assert score.precision == 0.5
    assert score.recall == 1.0


def test_missing_boundaries_reduce_recall():
    score = score_boundaries([0.0, 200.0], [0.0, 200.0, 420.0], tolerance=2.0)
    assert score.recall == pytest.approx(2 / 3)
    assert score.precision == 1.0


def test_empty_predictions_score_zero_without_raising():
    score = score_boundaries([], [0.0, 100.0], tolerance=2.0)
    assert score.f1 == 0.0
    assert score.matched == 0


# --------------------------------------------------------------------------
# Per-class tolerance
# --------------------------------------------------------------------------

def test_mixes_are_scored_far_more_loosely_than_albums():
    """A crossfade is genuinely tens of seconds wide; album-grade precision
    would score correct answers as failures."""
    assert TOLERANCE[ContentClass.CONTINUOUS_MIX] > TOLERANCE[ContentClass.DISCRETE_MULTITRACK] * 5


def test_fixture_uses_its_class_tolerance():
    mix = album_fixture(content_class=ContentClass.CONTINUOUS_MIX)
    assert mix.tolerance == TOLERANCE[ContentClass.CONTINUOUS_MIX]


# --------------------------------------------------------------------------
# Metadata fields
# --------------------------------------------------------------------------

def test_field_scoring_uses_consensus_normalisation():
    result = score_fields(
        {"artist": "Beyonce", "title": "Song (Official Video)"},
        {"artist": "Beyoncé", "title": "Song"},
    )
    assert result == {"artist": True, "title": True}


def test_missing_field_scores_false_rather_than_raising():
    assert score_fields({}, {"artist": "A Band"}) == {"artist": False}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_regimes_are_reported_separately():
    """The regimes are not comparable and must not be averaged together."""
    fixtures = [
        album_fixture(),
        album_fixture(
            video_id="mix1", content_class=ContentClass.CONTINUOUS_MIX,
            regime=Regime.COMMUNITY_TIMINGS, derived_from={"yt_comments"},
        ),
    ]
    reports = evaluate(fixtures, lambda f, p: f.boundaries, all_sources=ALL_SOURCES)
    assert len(reports) == 2
    assert {r.regime for r in reports} == {
        Regime.RELEASE_DURATIONS, Regime.COMMUNITY_TIMINGS,
    }


def test_perfect_prediction_summarises_as_such():
    reports = evaluate([album_fixture()], lambda f, p: f.boundaries,
                       all_sources=ALL_SOURCES)
    assert "mean F1:        1.000" in reports[0].summary()


def test_predictor_returning_nothing_is_skipped_not_scored_zero():
    """A source outage is missing data, not a wrong answer."""
    reports = evaluate([album_fixture()], lambda f, p: None, all_sources=ALL_SOURCES)
    assert reports[0].skipped == ["album1"]
    assert not reports[0].scores
