"""Scoring the resolver against ground truth, per content class.

Three regimes, because one does not generalise. The plan was originally to
derive labels everywhere from canonical release durations -- YouTube grading
itself, with no hand-labelling. Probing a real DJ set showed that only works
for discrete albums: in a mix, tracks are mixed in late, cut out early,
looped or extended, so their studio lengths bear no relation to how long they
appear. Applying that method to a set would produce impressive-looking
accuracy figures measured against a ruler that does not fit.

Two rules keep the numbers meaningful.

**Leave one source out.** Any source used to derive a fixture's label is
disabled as an input when scoring that fixture. Without it we would be
grading the exam with the answer key: MusicBrainz supplies album labels and
is also a resolver input, so scoring it against itself would report near
perfection and tell us nothing.

**Remove the constant offset before scoring.** Album uploads routinely carry
a lead-in, which shifts every boundary equally. That is a benign fixed
offset, not an accuracy failure, and charging it against every boundary
would swamp the real signal. Residual drift after removing it is the thing
that matters: a constant shift is a lead-in, drift is a different master.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum

from .content import ContentClass

#: How close a predicted boundary must be to count as correct, per class.
#: A crossfade is genuinely tens of seconds wide, so demanding album-grade
#: precision on a mix would score correct answers as failures.
TOLERANCE: dict[ContentClass, float] = {
    ContentClass.DISCRETE_MULTITRACK: 2.0,
    ContentClass.CONTINUOUS_MIX: 20.0,
    ContentClass.LIVE_PERFORMANCE: 15.0,
    ContentClass.AMBIENT_LONGFORM: 30.0,
}
DEFAULT_TOLERANCE = 10.0

#: A fixture needs this many independent derivations agreeing before it is
#: trusted as a label. One derivation is an assertion, not ground truth.
MIN_DERIVATIONS = 2


class Regime(Enum):
    """How a fixture's ground truth was established."""

    #: Cumulative track durations from a canonical release. Exact, and valid
    #: only where the upload really is that release.
    RELEASE_DURATIONS = "release-durations"
    #: Community tracklist timings. Noisier, and the only option for mixes.
    COMMUNITY_TIMINGS = "community-timings"
    #: Field-by-field metadata comparison, for single recordings.
    METADATA_FIELDS = "metadata-fields"


@dataclass
class Fixture:
    """One labelled example."""

    video_id: str
    content_class: ContentClass
    regime: Regime
    #: Ground-truth boundary times, seconds.
    boundaries: list[float] = field(default_factory=list)
    #: Ground-truth metadata, for single recordings.
    fields: dict[str, str] = field(default_factory=dict)
    #: Sources the label was derived from. Excluded as inputs when scoring.
    derived_from: set[str] = field(default_factory=set)
    #: How many independent derivations agreed.
    derivations: int = 0
    duration: float = 0.0
    note: str = ""

    @property
    def trustworthy(self) -> bool:
        return self.derivations >= MIN_DERIVATIONS

    @property
    def tolerance(self) -> float:
        return TOLERANCE.get(self.content_class, DEFAULT_TOLERANCE)

    def permitted_sources(self, all_sources: set[str]) -> set[str]:
        """Sources usable as inputs when scoring against this fixture."""
        return all_sources - self.derived_from


@dataclass
class BoundaryScore:
    matched: int = 0
    predicted: int = 0
    expected: int = 0
    offset: float = 0.0
    residuals: list[float] = field(default_factory=list)
    #: Signed error in time order. Drift is a *directional* trend, and the
    #: absolute residuals above cannot express it: an error running from -5s
    #: to +16s is steadily growing, but in absolute terms it falls and then
    #: rises, which reads as noise.
    signed: list[float] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.matched / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.matched / self.expected if self.expected else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def median_residual(self) -> float:
        return statistics.median(self.residuals) if self.residuals else 0.0

    @property
    def drifting(self) -> bool:
        """Whether error grows through the upload rather than staying flat.

        A constant offset is a lead-in and is corrected for. Growing error
        means the upload is a different master or a different edit, which is
        a genuine mismatch rather than a scoring artefact.
        """
        if len(self.signed) < 4:
            return False
        half = len(self.signed) // 2
        early = statistics.median(self.signed[:half])
        late = statistics.median(self.signed[half:])
        # A lead-in has already been removed as a constant offset, so any
        # remaining systematic movement between the start and end of the
        # upload is a genuine mismatch of edits.
        return abs(late - early) > 1.0

    def describe(self) -> str:
        return (
            f"P {self.precision:.2f}  R {self.recall:.2f}  F1 {self.f1:.2f}  "
            f"offset {self.offset:+.1f}s  median residual "
            f"{self.median_residual:.2f}s" + ("  [DRIFTING]" if self.drifting else "")
        )


def best_fit_offset(predicted: list[float], expected: list[float]) -> float:
    """The single shift that best aligns predictions with ground truth.

    Estimated as the median of nearest-neighbour differences rather than the
    mean, so a handful of spurious boundaries cannot drag the alignment.
    """
    if not predicted or not expected:
        return 0.0
    deltas = []
    for value in predicted:
        nearest = min(expected, key=lambda e: abs(e - value))
        deltas.append(value - nearest)
    return round(statistics.median(deltas), 3)


def score_boundaries(
    predicted: list[float],
    expected: list[float],
    tolerance: float,
    *,
    correct_offset: bool = True,
) -> BoundaryScore:
    """Match predicted boundaries to ground truth, one to one."""
    score = BoundaryScore(predicted=len(predicted), expected=len(expected))
    if not predicted or not expected:
        return score

    offset = best_fit_offset(predicted, expected) if correct_offset else 0.0
    score.offset = offset
    aligned = [value - offset for value in predicted]

    # Greedy nearest-match, each ground-truth boundary claimable once, so two
    # predictions near one boundary cannot both count as hits.
    unclaimed = sorted(expected)
    for value in sorted(aligned):
        if not unclaimed:
            break
        nearest = min(unclaimed, key=lambda e: abs(e - value))
        distance = abs(nearest - value)
        if distance <= tolerance:
            score.matched += 1
            score.residuals.append(round(distance, 3))
            score.signed.append(round(value - nearest, 3))
            unclaimed.remove(nearest)
    return score


def score_fields(
    predicted: dict[str, str | None], expected: dict[str, str]
) -> dict[str, bool]:
    """Compare metadata field by field, using consensus normalisation."""
    from .consensus import equivalent

    return {
        name: bool(predicted.get(name)) and equivalent(predicted[name] or "", truth)
        for name, truth in expected.items()
    }


@dataclass
class Report:
    regime: Regime
    content_class: ContentClass
    scores: list[BoundaryScore] = field(default_factory=list)
    field_results: list[dict[str, bool]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"{self.content_class.value} / {self.regime.value}",
            f"  fixtures scored: {len(self.scores) + len(self.field_results)}",
        ]
        if self.skipped:
            lines.append(
                f"  skipped (insufficient independent derivations): "
                f"{len(self.skipped)}"
            )
        if self.scores:
            lines.append(f"  mean F1:        {statistics.mean(s.f1 for s in self.scores):.3f}")
            lines.append(
                f"  mean recall:    {statistics.mean(s.recall for s in self.scores):.3f}"
            )
            residuals = [s.median_residual for s in self.scores if s.residuals]
            if residuals:
                lines.append(f"  median residual: {statistics.median(residuals):.2f}s")
            drifting = sum(1 for s in self.scores if s.drifting)
            if drifting:
                lines.append(
                    f"  drifting:       {drifting} (likely a different master, "
                    f"not a resolver error)"
                )
        if self.field_results:
            per_field: dict[str, list[bool]] = {}
            for result in self.field_results:
                for name, correct in result.items():
                    per_field.setdefault(name, []).append(correct)
            for name, values in sorted(per_field.items()):
                rate = sum(values) / len(values)
                lines.append(f"  {name:12s} {rate:.0%} correct ({len(values)})")
        return "\n".join(lines)


def evaluate(
    fixtures: list[Fixture],
    predict,
    *,
    all_sources: set[str],
) -> list[Report]:
    """Score a prediction function against fixtures, grouped by regime.

    `predict(fixture, permitted_sources)` returns either a list of boundary
    times or a metadata dict, depending on the regime. Passing the permitted
    set rather than filtering afterwards is what makes leave-one-out real:
    the excluded source never contributes to the prediction at all.
    """
    grouped: dict[tuple[Regime, ContentClass], Report] = {}

    for fixture in fixtures:
        key = (fixture.regime, fixture.content_class)
        report = grouped.setdefault(key, Report(fixture.regime, fixture.content_class))

        if not fixture.trustworthy:
            report.skipped.append(fixture.video_id)
            continue

        permitted = fixture.permitted_sources(all_sources)
        outcome = predict(fixture, permitted)
        if outcome is None:
            report.skipped.append(fixture.video_id)
            continue

        if fixture.regime is Regime.METADATA_FIELDS:
            report.field_results.append(score_fields(outcome, fixture.fields))
        else:
            report.scores.append(
                score_boundaries(outcome, fixture.boundaries, fixture.tolerance)
            )

    return list(grouped.values())
