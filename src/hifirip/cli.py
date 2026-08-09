"""Command-line entry point.

Kept thin on purpose. Agents drive this tool through the same commands a
human uses, so anything an agent needs to reason about -- which stream was
chosen, why, what the credentials are doing -- has to be printable here
rather than hidden behind a Python API.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from . import cookies as cookies_mod
from .config import EXAMPLE_CONFIG, load
from .consensus import strip_noise
from .content import ContentClass, apply_host_decision, classify
from .download import DownloadError, fetch, remux
from .entitlement import Severity
from .environment import detect
from .formats import NoUsableStream
from .library import LibraryError, hand_off, place, render_path
from .probe import ProbeError, probe
from .resolve import resolution_matrix, resolve
from .resolver import describe_credentials
from .resolver import resolve as resolve_sources
from .split import split
from .tag import TagError, Tags, fetch_thumbnail, square_crop, write_tags
from .tracklist import to_boundaries

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DEGRADED = 2   # ran, but credentials failed and quality is capped


def _probe(url: str, config, *, with_comments: bool = False) -> object:
    return probe(
        url,
        cookies_from_browser=config.cookies_from_browser,
        cookies_file=config.cookies_file,
        with_comments=with_comments,
    )


def _emit_tracks(staged, tracklist, result, decision, config, args, workdir,
                 resolution):
    """Split a staged file into tracks, tag each, and file them."""
    duration = float(result.info.get("duration") or 0.0)
    boundaries = to_boundaries(tracklist, duration)
    if not boundaries:
        print("Tracklist produced no usable boundaries.", file=sys.stderr)
        return EXIT_ERROR

    outcome = split(staged, boundaries, workdir / "tracks",
                    container=decision.stream.container)
    print(outcome.report())

    # For multitrack uploads the resolver puts the set or record name on the
    # album field; the raw video title is the last resort, not the default.
    album = next(
        (r.value for r in resolution.metadata.resolutions
         if r.field == "album" and r.value),
        None,
    ) or strip_noise(result.info.get("title") or "")

    cover = None
    thumbnail = fetch_thumbnail(args.url, workdir / "thumb")
    if thumbnail:
        try:
            cover = square_crop(thumbnail, workdir / "cover.jpg").read_bytes()
        except TagError:
            cover = None

    root = Path(args.output_dir) if args.output_dir else config.library_root
    written = []
    for index, (part, boundary) in enumerate(zip(outcome.parts, boundaries), start=1):
        track = tracklist.tracks[min(index - 1, len(tracklist.tracks) - 1)]
        artist = track.artist.value if track.artist else None
        tags = Tags(
            title=track.title.value or boundary.title,
            artist=artist,
            albumartist=result.info.get("uploader"),
            album=album,
            track=index,
            track_total=len(boundaries),
            source_url=args.url,
            provenance={"tracklist": ",".join(sorted(track.supporting_sources))},
        )
        write_tags(part, tags, cover)
        destination = render_path(config.naming, tags, decision.stream.container,
                                  root, single_template=config.naming_single)
        if args.dry_run:
            print(f"  would write {destination}")
        else:
            written.append(place(part, destination))

    if args.dry_run:
        return EXIT_OK

    print(f"\nWrote {len(written)} track(s) under {root}")
    if not args.no_handoff and written:
        print(hand_off(written[0].parent, detect()).report())
    return EXIT_OK




def cmd_explain(args: argparse.Namespace) -> int:
    config = load(overrides=_overrides(args))
    try:
        result = _probe(args.url, config)
    except ProbeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    try:
        decision = resolve(result, profile=args.profile)
    except NoUsableStream as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    classification = classify(result.info)

    print(f"{result.title}\n")
    print(decision.trace())
    print()
    print("=== content ===")
    print(f"Class: {classification.content_class.value} "
          f"(confidence {classification.confidence})")
    print(classification.briefing())

    if classification.needs_escalation:
        print()
        print("=== escalation ===")
        print(classification.escalation_brief())

    return EXIT_DEGRADED if decision.entitlement.silently_downgraded else EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load(overrides=_overrides(args))
    status = EXIT_OK

    print("=== dependencies ===")
    for tool in ("yt-dlp", "ffmpeg"):
        path = shutil.which(tool)
        print(f"  {tool:8s} {path or 'NOT FOUND -- install with `brew install ' + tool + '`'}")
        if not path:
            status = EXIT_ERROR

    print("\n=== environment ===")
    print(detect().describe())

    print("\n=== browsers ===")
    for browser in cookies_mod.survey_browsers():
        print(browser)

    blocked = cookies_mod.blocked_browsers()
    if blocked and not cookies_mod.suggest_browser():
        # The only credentials on this machine are ones we cannot read. That
        # is a fixable situation, and saying nothing here would leave the
        # user configuring a browser that is guaranteed to fail.
        names = ", ".join(b.name for b in blocked)
        print(f"\n{names}: cookies exist but are unreadable by this process.")
        print(cookies_mod.remedy(cookies_mod.CookieProblem.PERMISSION_DENIED,
                                 blocked[0].name))
        status = EXIT_DEGRADED

    print("\n=== api credentials ===")
    print(describe_credentials())

    if config.cookies_file:
        print("\n=== cookie file ===")
        diagnosis = cookies_mod.inspect_cookie_file(config.cookies_file)
        print(diagnosis.report())
        if not diagnosis.ok:
            status = EXIT_DEGRADED

    if not config.cookies_from_browser and not config.cookies_file:
        suggestion = cookies_mod.suggest_browser()
        print("\nNo credentials configured, so 256k streams will never be offered.")
        if suggestion:
            print(f"  Set `cookies_from_browser = \"{suggestion}\"` in your config "
                  f"to use the cookies found above.")
        elif not blocked:
            print("  No browser on this machine has a cookie store to read. Sign in\n"
                  "  to YouTube in a supported browser, or export a cookies.txt file\n"
                  "  and set `cookies_file` in your config.")

    if args.url:
        print("\n=== live credential check ===")
        try:
            result = _probe(args.url, config)
        except ProbeError as exc:
            source = config.cookies_from_browser or config.cookies_file or "none"
            diagnosis = cookies_mod.diagnose_stderr(str(exc), source)
            print(diagnosis.report())
            return EXIT_ERROR

        decision = resolve(result, profile=args.profile)
        print(decision.entitlement.summary())
        for warning in decision.entitlement.diagnostics:
            if warning.severity is Severity.WARNING:
                status = EXIT_DEGRADED
    else:
        print("\nPass a known YouTube Music track URL to verify credentials against\n"
              "eligible content: `hifi-rip doctor <url>`. Checking against an\n"
              "ordinary music video proves nothing, because 256k streams are gated\n"
              "on the content as well as the account.")

    return status


def cmd_config(args: argparse.Namespace) -> int:
    if args.example:
        print(EXAMPLE_CONFIG)
        return EXIT_OK
    print(load(overrides=_overrides(args)).describe())
    return EXIT_OK


def cmd_matrix(_: argparse.Namespace) -> int:
    print(resolution_matrix())
    return EXIT_OK


def _tags_from_resolution(resolution, info: dict, url: str) -> Tags:
    """Build tags from what the sources agreed, not from the raw upload.

    Using YouTube's own strings here would waste the resolution entirely: a
    real rip filed itself as "Exit - Still Here (Official Music Video) -
    Indie Rock 2026" under the uploader's channel name, while the resolver
    had already established the artist and a clean title from corroborated
    sources.

    Unsettled fields fall back to the upload rather than being dropped. A
    field the sources could not agree on is still better filled with the
    uploader's own words than left blank, but its provenance records which
    it was, so the audit trail in the file distinguishes the two.
    """
    values: dict[str, str] = {}
    provenance: dict[str, str] = {}
    for entry in resolution.metadata.resolutions:
        if entry.value:
            values[entry.field] = entry.value
            provenance[entry.field] = (
                ",".join(sorted({c.source for c in entry.supporting}))
                if entry.settled else f"uncorroborated:{entry.status.value}"
            )

    fallback_title = info.get("track") or strip_noise(info.get("title") or "")
    title = values.get("title") or fallback_title or "Unknown Title"
    artist = values.get("artist") or info.get("artist") or info.get("uploader")
    if "title" not in provenance:
        provenance["title"] = "youtube"
    if "artist" not in provenance and artist:
        provenance["artist"] = "youtube"

    return Tags(
        title=title,
        artist=artist,
        albumartist=artist,
        album=values.get("album"),
        date=values.get("date")
        or (info.get("release_year") and str(info["release_year"])) or None,
        source_url=url,
        provenance=provenance,
    )


def _resolve_class(classification, args) -> int | None:
    """Settle the content class, or explain what is needed and stop."""
    if args.content_class:
        apply_host_decision(classification, ContentClass(args.content_class), "--content-class")
        return None

    if classification.needs_escalation:
        print(classification.escalation_brief(), file=sys.stderr)
        print(
            "\nRe-run with --content-class <class> once decided.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    return None


def cmd_rip(args: argparse.Namespace) -> int:
    config = load(overrides=_overrides(args))
    try:
        result = _probe(args.url, config)
    except ProbeError as exc:
        source = config.cookies_from_browser or config.cookies_file or "none"
        diagnosis = cookies_mod.diagnose_stderr(str(exc), source)
        print(diagnosis.report() if diagnosis.is_user_fixable else str(exc),
              file=sys.stderr)
        return EXIT_ERROR

    try:
        decision = resolve(result, profile=args.profile)
    except NoUsableStream as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    classification = classify(result.info)
    early_exit = _resolve_class(classification, args)
    if early_exit is not None:
        return early_exit

    for note in decision.entitlement.diagnostics:
        if note.severity is Severity.WARNING:
            print(str(note), file=sys.stderr)

    # Splitting is a user decision for classes where boundaries are ranges
    # rather than points; see content.HANDLING.
    handling = classification.handling
    wants_split = args.split if args.split is not None else handling.splits
    if wants_split is None:
        print(classification.briefing())
        print(
            "\nThis needs your decision: re-run with --split for per-track "
            "files, or --no-split to keep it as one file.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(decision.selection.explain())
    print()

    # Comments are the highest-yield tracklist source for mixed material and
    # frequently the only timings that exist, but fetching them turns a fast
    # probe into a slow one. Pay that cost only when splitting.
    if wants_split and classification.is_multitrack:
        try:
            result = _probe(args.url, config, with_comments=True)
        except ProbeError:
            pass  # Keep the comment-free probe; a slower source is optional.

    resolution = resolve_sources(result.info, classification)
    print(resolution.describe())
    print()

    tracklist = resolution.tracklist
    if wants_split:
        if not tracklist or not tracklist.tracks:
            print(
                "No source produced a tracklist for this upload, so there is "
                "nothing to split on. Re-run with --no-split to keep it whole.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        if tracklist.needs_host and not args.accept_unverified:
            print(tracklist.brief(), file=sys.stderr)
            print(
                "\nSplitting stopped: the tracklist is not corroborated. Settle "
                "the tracks above, or pass --accept-unverified to split on the "
                "leading candidates anyway.",
                file=sys.stderr,
            )
            return EXIT_ERROR

    workdir = Path(tempfile.mkdtemp(prefix="hifi-rip-"))
    try:
        source = fetch(
            args.url, decision.stream, workdir,
            cookies_from_browser=config.cookies_from_browser,
            cookies_file=config.cookies_file,
            progress=not args.quiet,
        )
        staged = workdir / f"audio.{decision.stream.container}"
        rip = remux(source, staged)
        print(rip.report())

        if wants_split and tracklist:
            return _emit_tracks(
                staged, tracklist, result, decision, config, args, workdir,
                resolution,
            )

        tags = _tags_from_resolution(resolution, result.info, args.url)
        cover = None
        thumbnail = fetch_thumbnail(args.url, workdir / "thumb")
        if thumbnail:
            try:
                cover = square_crop(thumbnail, workdir / "cover.jpg").read_bytes()
            except TagError as exc:
                print(f"  cover art skipped: {exc}", file=sys.stderr)
        write_tags(staged, tags, cover)

        root = Path(args.output_dir) if args.output_dir else config.library_root
        destination = render_path(
            config.naming, tags, decision.stream.container, root,
            single_template=config.naming_single,
        )

        if args.dry_run:
            print(f"\nWould write: {destination}")
            print(hand_off(destination, detect(), dry_run=True).report())
            return EXIT_OK

        final = place(staged, destination)
        print(f"\nPlaced: {final}")

        if args.no_handoff:
            print("  handoff skipped (--no-handoff)")
        else:
            print(hand_off(final, detect()).report())

    except (DownloadError, TagError, LibraryError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return EXIT_DEGRADED if decision.entitlement.silently_downgraded else EXIT_OK


def _overrides(args: argparse.Namespace) -> dict:
    """CLI flags become the top configuration layer.

    Unset flags arrive as None and are dropped by `config._apply`, so an
    omitted flag never clobbers a config-file value.
    """
    return {
        "profile": getattr(args, "profile", None),
        "cookies_from_browser": getattr(args, "cookies_from_browser", None),
        "cookies_file": getattr(args, "cookies_file", None),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hifi-rip",
        description="Highest-quality YouTube audio extraction, without transcoding.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--profile", help="apple | windows | sonos | archive | opus-native")
        p.add_argument("--cookies-from-browser", dest="cookies_from_browser",
                       choices=cookies_mod.SUPPORTED_BROWSERS)
        p.add_argument("--cookies-file", dest="cookies_file", type=Path)

    explain = sub.add_parser("explain", help="show the resolved decision for a URL")
    explain.add_argument("url")
    add_common(explain)
    explain.set_defaults(func=cmd_explain)

    doctor = sub.add_parser("doctor", help="check dependencies and credentials")
    doctor.add_argument("url", nargs="?", help="a known YouTube Music track URL")
    add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    config = sub.add_parser("config", help="show resolved settings and their source")
    config.add_argument("--example", action="store_true", help="print a starter config")
    add_common(config)
    config.set_defaults(func=cmd_config)

    matrix = sub.add_parser("matrix", help="print the tier x destination table")
    matrix.set_defaults(func=cmd_matrix)

    rip = sub.add_parser("rip", help="download, tag, and file a track")
    rip.add_argument("url")
    add_common(rip)
    rip.add_argument("--output-dir", type=Path,
                     help="override the configured library root")
    rip.add_argument("--content-class",
                     choices=[c.value for c in ContentClass
                              if c is not ContentClass.UNKNOWN],
                     help="settle the content class when rules could not")
    split = rip.add_mutually_exclusive_group()
    split.add_argument("--split", dest="split", action="store_true", default=None,
                       help="produce per-track files")
    split.add_argument("--no-split", dest="split", action="store_false",
                       help="keep the upload as one file")
    rip.add_argument("--accept-unverified", action="store_true",
                     help="split on an uncorroborated tracklist anyway")
    rip.add_argument("--dry-run", action="store_true",
                     help="show the destination and handoff without writing")
    rip.add_argument("--no-handoff", action="store_true",
                     help="place the file but do not touch your music library")
    rip.add_argument("--quiet", action="store_true", help="suppress progress output")
    rip.set_defaults(func=cmd_rip)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
