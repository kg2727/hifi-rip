"""Run the analysis pipeline across a CSV of links and report what happened.

This is the end-to-end check the project is graded on: probe, classify,
resolve the stream, and -- for multitrack material -- assemble a tracklist
from whatever sources apply. Nothing is downloaded. A list of DJ sets and
concerts is hundreds of gigabytes and many hours of transfer, and none of
the decisions being verified here depend on having the bytes.

Concurrency is deliberately low. An earlier unpaced survey tripped YouTube's
bot detection and broke unrelated probes for the rest of the session, so the
default is a small pool with a pause between requests.

Usage:
    python tools/batch_check.py links.csv --out results.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

from hifirip.content import ContentClass, classify
from hifirip.formats import NoUsableStream
from hifirip.probe import ProbeError, probe
from hifirip.resolve import resolve
from hifirip.resolver import resolve as resolve_sources

CONCURRENCY = 3
THROTTLE = 1.0
#: Beyond this, comment fetching is worth its cost: only multitrack material
#: benefits, and it roughly triples probe time.
COMMENT_MINUTES = 20.0


@dataclass
class Outcome:
    name: str
    url: str
    declared_type: str = ""
    title: str = ""
    duration_min: float = 0.0
    content_class: str = ""
    confidence: float = 0.0
    escalated: bool = False
    tier: str = ""
    itag: str = ""
    codec: str = ""
    abr: float = 0.0
    container: str = ""
    premium_available: bool = False
    tracklist_sources: list[str] = field(default_factory=list)
    tracks_found: int = 0
    tracks_corroborated: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def check(name: str, url: str, declared: str, cookies: str | None) -> Outcome:
    outcome = Outcome(name=name, url=url, declared_type=declared)
    try:
        first = probe(url, cookies_from_browser=cookies)
    except ProbeError as exc:
        outcome.error = str(exc).strip().splitlines()[-1][:180]
        return outcome

    outcome.title = (first.title or "")[:90]
    outcome.duration_min = round((first.duration or 0) / 60, 1)

    classification = classify(first.info)
    outcome.content_class = classification.content_class.value
    outcome.confidence = classification.confidence
    outcome.escalated = classification.needs_escalation

    try:
        decision = resolve(first)
    except NoUsableStream as exc:
        outcome.error = str(exc)[:180]
        return outcome

    outcome.tier = decision.entitlement.tier.value
    outcome.itag = decision.stream.itag
    outcome.codec = decision.stream.family
    outcome.abr = round(decision.stream.abr or 0.0, 1)
    outcome.container = decision.stream.container
    outcome.premium_available = bool(decision.entitlement.premium_streams)

    # Long uploads get the slower comment-bearing probe, since that is where
    # tracklists actually live for mixed material.
    info = first.info
    if outcome.duration_min >= COMMENT_MINUTES:
        try:
            info = probe(url, cookies_from_browser=cookies, with_comments=True).info
        except ProbeError:
            pass

    # Classification is unreliable on ambiguous titles by design; for the
    # tracklist pass, treat anything long and unclassified as a mix so the
    # tracklist sources are still exercised.
    effective = classification
    if classification.needs_escalation and outcome.duration_min >= COMMENT_MINUTES:
        from hifirip.content import apply_host_decision
        effective = apply_host_decision(
            classify(info), ContentClass.CONTINUOUS_MIX, "batch-check heuristic"
        )

    try:
        resolution = resolve_sources(info, effective)
    except Exception as exc:
        outcome.error = f"resolver: {exc}"[:180]
        return outcome

    if resolution.tracklist:
        outcome.tracklist_sources = sorted(resolution.tracklist.contributors)
        outcome.tracks_found = len(resolution.tracklist.tracks)
        outcome.tracks_corroborated = sum(
            1 for t in resolution.tracklist.tracks if not t.needs_judgement
        )
    return outcome


def load(path: Path) -> list[tuple[str, str, str]]:
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            link = (row.get("link") or "").strip()
            if link:
                rows.append((
                    (row.get("name") or "").strip(),
                    link,
                    (row.get("type") or "").strip(),
                ))
    return rows


def summarise(results: list[Outcome]) -> str:
    total = len(results)
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    lines = [
        "=" * 78,
        f"{total} link(s): {len(ok)} analysed, {len(failed)} failed",
        "=" * 78,
        "",
        "CONTENT CLASSES",
    ]
    classes: dict[str, int] = {}
    for r in ok:
        classes[r.content_class] = classes.get(r.content_class, 0) + 1
    for name, count in sorted(classes.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {name:22s} {count:3d}")
    escalated = sum(1 for r in ok if r.escalated)
    lines.append(f"  (escalated to host: {escalated})")

    lines += ["", "STREAM SELECTION"]
    premium = [r for r in ok if r.premium_available]
    lines.append(f"  premium 256k offered:  {len(premium)}/{len(ok)}")
    tiers: dict[str, int] = {}
    for r in ok:
        tiers[r.tier] = tiers.get(r.tier, 0) + 1
    for tier, count in sorted(tiers.items()):
        lines.append(f"  tier {tier:10s}      {count}")
    codecs: dict[str, int] = {}
    for r in ok:
        codecs[f"{r.codec}/{r.container}"] = codecs.get(f"{r.codec}/{r.container}", 0) + 1
    for codec, count in sorted(codecs.items()):
        lines.append(f"  {codec:20s} {count}")
    if ok:
        lines.append(
            f"  mean bitrate:          {sum(r.abr for r in ok) / len(ok):.0f}k"
        )

    lines += ["", "TRACKLIST RESOLUTION (multitrack only)"]
    multi = [r for r in ok if r.tracks_found]
    lines.append(f"  uploads with a tracklist: {len(multi)}/{len(ok)}")
    if multi:
        lines.append(f"  total tracks found:       {sum(r.tracks_found for r in multi)}")
        lines.append(
            f"  corroborated (2+ sources):{sum(r.tracks_corroborated for r in multi)}"
        )
        contributors: dict[str, int] = {}
        for r in multi:
            for source in r.tracklist_sources:
                contributors[source] = contributors.get(source, 0) + 1
        for source, count in sorted(contributors.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {source:18s} contributed to {count}")

    if failed:
        lines += ["", "FAILURES"]
        for r in failed:
            lines.append(f"  {r.name[:52]:54s} {r.error[:70]}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--out", type=Path, default=Path("batch_results.json"))
    parser.add_argument("--cookies-from-browser", default="safari")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    rows = load(args.csv)
    if args.limit:
        rows = rows[: args.limit]
    print(f"Checking {len(rows)} link(s)...", file=sys.stderr)

    results: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {}
        for name, url, declared in rows:
            futures[pool.submit(check, name, url, declared,
                                args.cookies_from_browser)] = name
            time.sleep(THROTTLE)
        for index, future in enumerate(as_completed(futures), start=1):
            outcome = future.result()
            results.append(outcome)
            flag = "ok " if outcome.ok else "ERR"
            print(f"  [{index:2d}/{len(rows)}] {flag} {outcome.name[:56]}",
                  file=sys.stderr)

    results.sort(key=lambda r: r.name)
    args.out.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(summarise(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
