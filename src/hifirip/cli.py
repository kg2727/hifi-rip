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
from pathlib import Path

from . import cookies as cookies_mod
from .config import EXAMPLE_CONFIG, load
from .content import classify
from .entitlement import Severity
from .environment import detect
from .formats import NoUsableStream
from .probe import ProbeError, probe
from .resolve import resolution_matrix, resolve

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DEGRADED = 2   # ran, but credentials failed and quality is capped


def _probe(url: str, config) -> object:
    return probe(
        url,
        cookies_from_browser=config.cookies_from_browser,
        cookies_file=config.cookies_file,
    )


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


def cmd_rip(args: argparse.Namespace) -> int:
    print(
        "`rip` is not implemented yet. The stream-selection, credential and\n"
        "classification stages are complete and inspectable with:\n"
        "    hifi-rip explain <url>\n"
        "Downloading, splitting and tagging are still being built.",
        file=sys.stderr,
    )
    return EXIT_ERROR


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

    rip = sub.add_parser("rip", help="download (not yet implemented)")
    rip.add_argument("url")
    add_common(rip)
    rip.set_defaults(func=cmd_rip)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
