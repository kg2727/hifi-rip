"""Collect uploads that carry enough independent tracklists to score against.

Leave-one-out needs three sources on the same upload: hold one out, and two
must remain to agree on a label. Run against a corpus of DJ sets that
condition held exactly once in fifty, which is why the first weight
validation was inconclusive.

Album uploads are a far better hunting ground. The content survey put
chapters on ~75% of full-album uploads and 0% of DJ sets, and album
descriptions carry tracklists far more often, so three-way overlap is common
rather than freakish.

This only produces *candidate links*. `validate_weights.py` does the
filtering, since it already knows what a scoreable upload looks like.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

#: Phrasings that reliably surface discrete multi-track uploads. Genre spread
#: matters: chaptering conventions differ sharply between communities, and a
#: corpus drawn from one genre would measure that genre's habits.
QUERIES = [
    "full album with timestamps", "full album tracklist", "full album 1970s",
    "full album jazz", "full album classical", "full album hip hop",
    "full album rock", "full album metal", "full album electronic",
    "full album ambient", "full album soul", "full album folk",
    "complete album stream", "album full with chapters",
    "greatest hits full album", "full soundtrack ost with timestamps",
    "anime ost full album", "game soundtrack full album",
    "compilation full album timestamps", "best of full album tracklist",
]

PER_QUERY = 15
CONCURRENCY = 2
#: The first unpaced survey tripped bot detection and broke unrelated probes
#: for the rest of the session. Slow is cheaper than blocked.
THROTTLE = 1.5


def yt_dlp() -> str:
    path = shutil.which("yt-dlp")
    if not path:
        sys.exit("yt-dlp not found on PATH")
    return path


def search(query: str, limit: int) -> list[tuple[str, str]]:
    try:
        done = subprocess.run(
            [yt_dlp(), "-J", "--flat-playlist", "--no-warnings",
             f"ytsearch{limit}:{query}"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.SubprocessError:
        return []
    if done.returncode != 0 or not done.stdout.strip():
        return []
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError:
        return []

    found = []
    for entry in payload.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        duration = entry.get("duration") or 0
        # Below ~20 minutes it is a single track, not a multi-track upload.
        if duration < 1200:
            continue
        found.append((
            (entry.get("title") or "")[:110],
            f"https://www.youtube.com/watch?v={entry['id']}",
        ))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("data/corpus_links.csv"))
    parser.add_argument("--per-query", type=int, default=PER_QUERY)
    args = parser.parse_args()

    print(f"Searching {len(QUERIES)} queries...", file=sys.stderr)
    rows: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {}
        for query in QUERIES:
            futures[pool.submit(search, query, args.per_query)] = query
            time.sleep(THROTTLE)
        for future in as_completed(futures):
            for title, url in future.result():
                rows.setdefault(url, title)
            print(f"  {futures[future][:44]:46s} total {len(rows)}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "type", "genre", "link"])
        writer.writeheader()
        for url, title in rows.items():
            writer.writerow(
                {"name": title, "type": "album-upload", "genre": "", "link": url}
            )

    print(f"\nWrote {len(rows)} candidate links to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
