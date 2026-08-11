"""Test the source weights against reality by holding each source out.

The weights in `sources/__init__.py` are asserted, not measured: I ranked
chapters above descriptions above comments because that ordering seemed
obviously right. Obvious is not evidence, and the weights decide every
contested field, so they are worth checking.

Method, per upload that several sources describe:

  1. pick one source to evaluate;
  2. build ground truth from the *other* sources that agree with each other;
  3. score the held-out source against that label.

The source under test never contributes to its own label, which is the same
leave-one-out rule `evaluation` enforces for fixtures. A label needs at least
two agreeing sources behind it, so a single unreviewed comment cannot become
the standard another source is judged against.

What this can and cannot show: it measures whether a source agrees with the
consensus of its peers, which is the property the weighting actually encodes.
It cannot show whether the consensus itself is right -- all sources could be
wrong together, and on an obscure upload they sometimes are.

Usage:
    python tools/validate_weights.py links.csv --out data/weights.json
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from hifirip.content import ContentClass, apply_host_decision, classify
from hifirip.evaluation import score_boundaries
from hifirip.probe import ProbeError, probe
from hifirip.sources import REGISTRY
from hifirip.sources import youtube as yt
from hifirip.tracklist import TOLERANCE, cluster

CONCURRENCY = 2
THROTTLE = 1.2
MULTITRACK_MINUTES = 20.0

#: A label needs this many agreeing sources behind it. One source is an
#: assertion; two that independently agree is evidence.
MIN_LABEL_SOURCES = 2

#: Trials per source before its measured accuracy is worth ranking on.
MIN_TRIALS_PER_SOURCE = 5

#: F1 differences below this are noise, not an ordering. Without this, three
#: sources tied at 1.00 produce a spurious ranking from sort order alone.
TIE_TOLERANCE = 0.05


@dataclass
class Trial:
    video_id: str
    title: str
    source: str
    label_from: list[str] = field(default_factory=list)
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    residual: float = 0.0
    predicted: int = 0
    expected: int = 0


def consensus_boundaries(
    contributions: dict[str, list[yt.TrackCandidate]],
    exclude: str,
    tolerance: float,
) -> tuple[list[float], list[str]]:
    """Ground truth from every source except `exclude`.

    A cluster becomes a labelled boundary only when at least two of the
    remaining sources place a boundary there, so a lone outlier cannot define
    the standard.
    """
    others = {name: entries for name, entries in contributions.items()
              if name != exclude and entries}
    if len(others) < MIN_LABEL_SOURCES:
        return [], []

    flattened = [
        (candidate, REGISTRY[name].weight if name in REGISTRY else 0.5)
        for name, entries in others.items() for candidate in entries
    ]

    boundaries = []
    for group in cluster(flattened, tolerance):
        sources = {candidate.source for candidate, _ in group}
        if len(sources) >= MIN_LABEL_SOURCES:
            boundaries.append(
                statistics.median([candidate.start for candidate, _ in group])
            )
    return sorted(boundaries), sorted(others)


def trials_for(url: str, cookies: str | None) -> list[Trial]:
    try:
        result = probe(url, cookies_from_browser=cookies, with_comments=True)
    except ProbeError:
        return []

    duration = float(result.info.get("duration") or 0.0)
    if duration / 60 < MULTITRACK_MINUTES:
        return []

    classification = classify(result.info)
    if classification.needs_escalation:
        classification = apply_host_decision(
            classification, ContentClass.CONTINUOUS_MIX, "weight-validation heuristic"
        )
    tolerance = TOLERANCE.get(classification.content_class, 10.0)

    contributions = yt.collect(result.info)
    if len(contributions) < MIN_LABEL_SOURCES + 1:
        # Holding one out must still leave enough to build a label.
        return []

    trials = []
    for source, entries in contributions.items():
        expected, label_from = consensus_boundaries(contributions, source, tolerance)
        if not expected:
            continue
        predicted = [candidate.start for candidate in entries]
        score = score_boundaries(predicted, expected, tolerance)
        trials.append(Trial(
            video_id=result.info.get("id", ""),
            title=(result.title or "")[:70],
            source=source,
            label_from=label_from,
            precision=round(score.precision, 3),
            recall=round(score.recall, 3),
            f1=round(score.f1, 3),
            residual=round(score.median_residual, 2),
            predicted=len(predicted),
            expected=len(expected),
        ))
    return trials


def summarise(trials: list[Trial]) -> str:
    by_source: dict[str, list[Trial]] = defaultdict(list)
    for trial in trials:
        by_source[trial.source].append(trial)

    lines = [
        "=" * 74,
        f"{len(trials)} trial(s) across {len({t.video_id for t in trials})} upload(s)",
        "=" * 74,
        "",
        f"{'source':18s} {'n':>3s} {'assigned':>9s} {'F1':>6s} {'prec':>6s} "
        f"{'recall':>7s} {'resid':>7s}",
    ]

    measured = []
    for name, group in sorted(by_source.items()):
        assigned = REGISTRY[name].weight if name in REGISTRY else 0.0
        f1 = statistics.mean(t.f1 for t in group)
        measured.append((name, assigned, f1))
        lines.append(
            f"{name:18s} {len(group):3d} {assigned:9.2f} {f1:6.2f} "
            f"{statistics.mean(t.precision for t in group):6.2f} "
            f"{statistics.mean(t.recall for t in group):7.2f} "
            f"{statistics.mean(t.residual for t in group):7.2f}"
        )

    lines += ["", "VERDICT"]
    thin = [name for name, group in by_source.items()
            if len(group) < MIN_TRIALS_PER_SOURCE]

    if len(measured) < 2:
        lines.append("  Inconclusive: fewer than two sources could be measured.")
    elif thin:
        # Ranking on one or two observations per source is astrology. The
        # first run of this tool reported a confident MISMATCH from three
        # trials on a single upload, where all three sources tied at F1 1.00
        # and the "measured ordering" was nothing but sort order.
        lines.append(
            f"  Inconclusive: {', '.join(sorted(thin))} have fewer than "
            f"{MIN_TRIALS_PER_SOURCE} trials each."
        )
        lines.append(
            "  Leave-one-out needs uploads carrying three or more independent "
            "tracklists, so that holding one out still leaves two to agree on "
            "a label. Those are rare; a larger or more chapter-rich corpus is "
            "required before these weights can be judged."
        )
    else:
        spread = max(f for _, _, f in measured) - min(f for _, _, f in measured)
        if spread < TIE_TOLERANCE:
            lines.append(
                f"  All sources agree with peer consensus to within "
                f"{spread:.2f} F1 -- no ordering is distinguishable, so the "
                f"assigned weights are neither confirmed nor refuted."
            )
        else:
            by_assigned = [n for n, _, _ in sorted(measured, key=lambda m: -m[1])]
            by_measured = [n for n, _, _ in sorted(measured, key=lambda m: -m[2])]
            lines.append(f"  assigned: {' > '.join(by_assigned)}")
            lines.append(f"  measured: {' > '.join(by_measured)}")
            lines.append(
                "  The assigned ordering matches the measured one."
                if by_assigned == by_measured else
                "  MISMATCH -- assigned weights do not reflect measured "
                "agreement. Reweighting is warranted."
            )

    lines += [
        "",
        "Caveat: this measures agreement with the consensus of other sources,",
        "not correctness. All sources can be wrong together.",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--out", type=Path, default=Path("data/weights.json"))
    parser.add_argument("--cookies-from-browser", default="safari")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    with args.csv.open(newline="", encoding="utf-8-sig") as handle:
        urls = [row["link"].strip() for row in csv.DictReader(handle)
                if (row.get("link") or "").strip()]
    if args.limit:
        urls = urls[: args.limit]

    print(f"Validating weights across {len(urls)} link(s)...", file=sys.stderr)
    trials: list[Trial] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {}
        for url in urls:
            futures[pool.submit(trials_for, url, args.cookies_from_browser)] = url
            time.sleep(THROTTLE)
        for index, future in enumerate(as_completed(futures), start=1):
            found = future.result()
            trials.extend(found)
            print(f"  [{index:2d}/{len(urls)}] {len(found)} trial(s)", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps([t.__dict__ for t in trials], indent=2))
    print(summarise(trials))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
