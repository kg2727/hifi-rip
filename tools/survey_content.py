"""Sample real YouTube music content to derive a content taxonomy from data.

The first cut of `hifirip.content` classified from priors: a handful of title
regexes and duration buckets I guessed at. Probing four real videos broke it
twice -- a 25-minute documentary landed in the single-track bucket by two
seconds, and a festival aftermovie was routed to DJ-set handling. Guessing
harder is not the fix.

So this samples a wide spread of real uploads and reports what the space
actually looks like: how long each kind of thing runs, how often uploaders
supply chapters, how much overlap there is between categories that need
different handling. The taxonomy is then written against the distributions
rather than against intuition.

Two passes, because they cost very differently:

  * a flat search pass, one request per query, yielding title/duration/channel
    for every hit -- cheap enough to run across the whole query set;
  * a detail pass over a stratified subsample, fetching chapters, description
    and category, which costs one request per video.

Usage:
    python tools/survey_content.py --out data/survey.jsonl
    python tools/survey_content.py --analyze data/survey.jsonl
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Provisional groupings used only to spread the sample across the space.
#: They are an input to the survey, deliberately not its output -- the
#: taxonomy is derived from the measurements, not asserted here.
QUERIES: dict[str, list[str]] = {
    "official-video": [
        "official music video 2026", "official video pop", "official video hip hop",
        "official music video rock band",
    ],
    "lyric-video": ["official lyric video", "lyrics video song"],
    "audio-only-single": ["official audio song", "audio only track official"],
    "live-single-song": [
        "live performance single song", "tiny desk concert", "acoustic live session",
    ],
    "full-concert": [
        "full concert live", "live at wembley full show", "full live set band",
    ],
    "dj-set": [
        "dj set full", "boiler room set", "essential mix full",
        "tomorrowland mainstage set", "b2b dj set live",
    ],
    "radio-show-mix": [
        "a state of trance episode", "radio show mix episode", "weekly mix show",
    ],
    "aftermovie-montage": [
        "festival official aftermovie", "festival recap video", "music festival highlights",
    ],
    "full-album": [
        "full album stream", "complete album 1970s", "full album jazz",
    ],
    "compilation-mix": [
        "best of mix compilation", "greatest hits playlist mix", "top songs mix hour",
    ],
    "long-ambient-mix": [
        "lofi hip hop radio", "8 hour sleep music", "study music long mix",
        "white noise ambient hours",
    ],
    "mashup-remix": ["mashup megamix", "remix official", "year end megamix"],
    "cover-version": ["cover song acoustic", "piano cover of song"],
    "karaoke-instrumental": ["karaoke instrumental version", "backing track instrumental"],
    "music-documentary": [
        "music documentary full", "band documentary", "album making of documentary",
        "electronic music documentary",
    ],
    "interview-podcast": [
        "musician interview podcast", "music podcast episode", "artist interview long",
    ],
    "analysis-education": [
        "music theory analysis song", "how this song was made breakdown",
    ],
    "reaction": ["reaction to song first time hearing"],
    "classical-longform": [
        "full symphony performance", "complete opera performance", "piano recital full",
    ],
    "soundtrack-ost": ["game soundtrack full ost", "anime opening full", "film score suite"],
    "shorts-fragment": ["#shorts music clip", "shorts song snippet"],
}

FLAT_PER_QUERY = 12
DETAIL_PER_GROUP = 4
#: Kept low deliberately. The first run of this tool issued roughly 800
#: requests with no pacing and tripped YouTube's bot detection, after which
#: every anonymous probe from this host failed with "Sign in to confirm
#: you're not a bot" -- including the unrelated ones being tested at the
#: time. A survey that breaks the thing it is surveying is worse than a
#: slow one.
CONCURRENCY = 2
#: Seconds to wait between requests on each worker.
THROTTLE = 1.5
TIMEOUT = 90


@dataclass
class Item:
    group: str
    query: str
    video_id: str
    title: str
    duration: float | None
    channel: str | None
    view_count: int | None = None
    # Detail pass only.
    chapters: int | None = None
    description_timestamps: int | None = None
    categories: list[str] = field(default_factory=list)
    has_track_metadata: bool | None = None


def yt_dlp() -> str:
    path = shutil.which("yt-dlp")
    if not path:
        sys.exit("yt-dlp not found on PATH")
    return path


def _run(cmd: list[str]) -> dict | None:
    time.sleep(THROTTLE)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.SubprocessError:
        return None
    if "not a bot" in (done.stderr or ""):
        print("  rate-limited by YouTube; stop and retry later with cookies",
              file=sys.stderr)
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        return None


def flat_search(group: str, query: str, limit: int) -> list[Item]:
    """One request per query; returns title/duration/channel per hit."""
    payload = _run([
        yt_dlp(), "-J", "--flat-playlist", "--no-warnings",
        f"ytsearch{limit}:{query}",
    ])
    if not payload:
        return []
    items = []
    for entry in payload.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        items.append(Item(
            group=group,
            query=query,
            video_id=entry["id"],
            title=entry.get("title") or "",
            duration=entry.get("duration"),
            channel=entry.get("channel") or entry.get("uploader"),
            view_count=entry.get("view_count"),
        ))
    return items


def detail(item: Item) -> Item:
    """Second pass: chapters, description timestamps, category, music tags."""
    from hifirip.content import count_timestamps  # local import; tool-only dep

    payload = _run([
        yt_dlp(), "-J", "--no-warnings", "--no-playlist",
        f"https://www.youtube.com/watch?v={item.video_id}",
    ])
    if not payload:
        return item
    item.chapters = len(payload.get("chapters") or [])
    item.description_timestamps = count_timestamps(payload.get("description") or "")
    item.categories = payload.get("categories") or []
    item.has_track_metadata = bool(payload.get("track") or payload.get("album"))
    item.duration = payload.get("duration", item.duration)
    return item


def collect(out: Path) -> list[Item]:
    jobs = [(g, q) for g, queries in QUERIES.items() for q in queries]
    items: list[Item] = []

    print(f"flat pass: {len(jobs)} queries x {FLAT_PER_QUERY}", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {
            pool.submit(flat_search, g, q, FLAT_PER_QUERY): (g, q) for g, q in jobs
        }
        for future in as_completed(futures):
            found = future.result()
            items.extend(found)
            print(f"  {futures[future][1][:40]:42s} {len(found):3d}", file=sys.stderr)

    # Stratified subsample for the expensive pass: the longest and shortest
    # of each group, where classification is most likely to be ambiguous.
    by_group: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        if item.duration:
            by_group[item.group].append(item)
    sample: list[Item] = []
    for group, group_items in by_group.items():
        group_items.sort(key=lambda i: i.duration or 0)
        sample.extend(group_items[: DETAIL_PER_GROUP // 2])
        sample.extend(group_items[-(DETAIL_PER_GROUP // 2):])

    print(f"detail pass: {len(sample)} videos", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        list(as_completed([pool.submit(detail, item) for item in sample]))

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as handle:
        for item in items:
            handle.write(json.dumps(asdict(item)) + "\n")
    print(f"wrote {len(items)} items to {out}", file=sys.stderr)
    return items


def analyze(path: Path) -> None:
    items = [Item(**json.loads(line)) for line in path.read_text().splitlines() if line]
    withdur = [i for i in items if i.duration]
    print(f"{len(items)} items sampled, {len(withdur)} with durations\n")

    print(f"{'group':24s} {'n':>4s} {'p10':>7s} {'med':>7s} {'p90':>7s}  minutes")
    print("-" * 72)
    rows = []
    for group in QUERIES:
        durations = sorted(i.duration / 60 for i in withdur if i.group == group)
        if not durations:
            continue
        def pct(p):
            return durations[min(len(durations) - 1, int(len(durations) * p))]
        rows.append((group, len(durations), pct(0.1), statistics.median(durations), pct(0.9)))
        print(f"{group:24s} {len(durations):4d} {pct(0.1):7.1f} "
              f"{statistics.median(durations):7.1f} {pct(0.9):7.1f}")

    print("\n--- duration overlap between groups needing different handling ---")
    for a, b in [
        ("music-documentary", "full-album"),
        ("music-documentary", "compilation-mix"),
        ("aftermovie-montage", "dj-set"),
        ("interview-podcast", "dj-set"),
        ("full-concert", "dj-set"),
        ("live-single-song", "official-video"),
    ]:
        da = [i.duration / 60 for i in withdur if i.group == a]
        db = [i.duration / 60 for i in withdur if i.group == b]
        if not da or not db:
            continue
        lo = max(min(da), min(db))
        hi = min(max(da), max(db))
        both = len([x for x in da + db if lo <= x <= hi])
        share = both / (len(da) + len(db))
        print(f"  {a:22s} vs {b:20s} overlap {lo:6.1f}-{hi:6.1f}m "
              f"covers {share:.0%} of both")

    detailed = [i for i in items if i.chapters is not None]
    if detailed:
        print(f"\n--- detail pass ({len(detailed)} videos) ---")
        print(f"{'group':24s} {'n':>3s} {'has_chapters':>13s} {'has_desc_ts':>12s} {'track_meta':>11s}")
        for group in QUERIES:
            sub = [i for i in detailed if i.group == group]
            if not sub:
                continue
            ch = sum(1 for i in sub if (i.chapters or 0) > 1) / len(sub)
            ts = sum(1 for i in sub if (i.description_timestamps or 0) > 1) / len(sub)
            tm = sum(1 for i in sub if i.has_track_metadata) / len(sub)
            print(f"{group:24s} {len(sub):3d} {ch:12.0%} {ts:11.0%} {tm:10.0%}")

    print("\n--- most frequent title tokens per group ---")
    for group in QUERIES:
        titles = [i.title.lower() for i in items if i.group == group]
        if not titles:
            continue
        tokens = Counter(
            word.strip("()[]|-–—,.!?\"'")
            for title in titles for word in title.split()
            if len(word) > 3
        )
        common = ", ".join(w for w, _ in tokens.most_common(6))
        print(f"  {group:24s} {common}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("data/survey.jsonl"))
    parser.add_argument("--analyze", type=Path)
    args = parser.parse_args()

    if args.analyze:
        analyze(args.analyze)
    else:
        collect(args.out)
        analyze(args.out)


if __name__ == "__main__":
    main()
