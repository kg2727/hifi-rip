"""The free/Premium x Apple/non-Apple resolution matrix.

These are the load-bearing tests of the whole project: they pin down that
the destination picks the codec family, the account tier picks the bitrate
within it, and that no combination ever produces a transcode.

Stream lists are synthetic so both tiers are testable without credentials
in CI. The free fixture mirrors a real probe (see tests/fixtures) including
the detail that itag 140 measures slightly *above* itag 251 -- the case
that previously produced a wrong explanation.
"""

from __future__ import annotations

import pytest

from hifirip.entitlement import Severity, Tier, assess, cookies_were_accepted
from hifirip.environment import Environment, Handoff, Host
from hifirip.formats import CONTAINER, NoUsableStream, parse_streams, select
from hifirip.probe import ProbeResult
from hifirip.resolve import resolve

FREE_FORMATS = [
    {"format_id": "140", "acodec": "mp4a.40.2", "vcodec": "none", "abr": 130.0,
     "asr": 44100, "ext": "m4a", "filesize": 3_400_000},
    {"format_id": "251", "acodec": "opus", "vcodec": "none", "abr": 129.0,
     "asr": 48000, "ext": "webm", "filesize": 3_390_000},
    {"format_id": "139", "acodec": "mp4a.40.5", "vcodec": "none", "abr": 49.0,
     "asr": 22050, "ext": "m4a", "filesize": 1_300_000},
    {"format_id": "249", "acodec": "opus", "vcodec": "none", "abr": 46.0,
     "asr": 48000, "ext": "webm", "filesize": 1_200_000},
    # A muxed stream, which must never be selected: its audio track is a
    # second-generation re-encode.
    {"format_id": "18", "acodec": "mp4a.40.2", "vcodec": "avc1.42001E",
     "abr": 96.0, "asr": 44100, "ext": "mp4", "filesize": 9_000_000},
]

PREMIUM_ONLY = [
    {"format_id": "141", "acodec": "mp4a.40.2", "vcodec": "none", "abr": 256.0,
     "asr": 44100, "ext": "m4a", "filesize": 6_800_000},
    {"format_id": "774", "acodec": "opus", "vcodec": "none", "abr": 256.0,
     "asr": 48000, "ext": "webm", "filesize": 6_800_000},
]

PREMIUM_FORMATS = FREE_FORMATS + PREMIUM_ONLY

COOKIE_REJECTION = (
    "WARNING: [youtube] Some web client https formats have been skipped\n"
    "ERROR: [youtube] Sign in to confirm you're not a bot. Use --cookies\n"
)


def make_result(formats: list[dict], *, cookies: bool, stderr: str = "") -> ProbeResult:
    return ProbeResult(
        info={"id": "test", "title": "Test", "duration": 213.0, "formats": formats},
        streams=parse_streams(formats),
        stderr=stderr,
        cookies_configured=cookies,
    )


APPLE_ENV = Environment(
    host=Host.MACOS,
    profile="apple",
    handoff=Handoff.APPLE_MUSIC,
    library_root=__import__("pathlib").Path("/tmp/lib"),
)


# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("profile", "formats", "cookies", "expected_itag", "expected_ext"),
    [
        # Apple destinations pin AAC in both tiers; only the bitrate moves.
        ("apple", FREE_FORMATS, False, "140", "m4a"),
        ("apple", PREMIUM_FORMATS, True, "141", "m4a"),
        ("windows", FREE_FORMATS, False, "140", "m4a"),
        ("windows", PREMIUM_FORMATS, True, "141", "m4a"),
        ("sonos", PREMIUM_FORMATS, True, "141", "m4a"),
        # Non-Apple destinations pin Opus in both tiers.
        ("archive", FREE_FORMATS, False, "251", "opus"),
        ("archive", PREMIUM_FORMATS, True, "774", "opus"),
        ("opus-native", FREE_FORMATS, False, "251", "opus"),
        ("opus-native", PREMIUM_FORMATS, True, "774", "opus"),
    ],
)
def test_resolution_matrix(profile, formats, cookies, expected_itag, expected_ext):
    decision = resolve(make_result(formats, cookies=cookies), profile=profile)
    assert decision.stream.itag == expected_itag
    assert decision.stream.container == expected_ext


def test_premium_upgrade_is_within_family_never_across_it():
    """Premium must raise bitrate without ever changing the container.

    If Premium flipped an Apple rip from .m4a to .opus it would land an
    unplayable file in Music.app -- a strictly worse outcome than the
    lower-bitrate stream.
    """
    free = resolve(make_result(FREE_FORMATS, cookies=False), profile="apple")
    premium = resolve(make_result(PREMIUM_FORMATS, cookies=True), profile="apple")

    assert free.stream.family == premium.stream.family == "aac"
    assert free.stream.container == premium.stream.container == "m4a"
    assert (premium.stream.abr or 0) > (free.stream.abr or 0)


def test_muxed_streams_are_never_offered():
    streams = parse_streams(FREE_FORMATS)
    assert "18" not in {s.itag for s in streams}


def test_container_always_matches_source_family():
    """No selection may imply a transcode."""
    for profile in ("apple", "windows", "archive", "sonos", "opus-native"):
        for formats in (FREE_FORMATS, PREMIUM_FORMATS):
            selection = select(parse_streams(formats), profile)
            assert selection.stream.container == CONTAINER[selection.stream.family]


def test_strict_profile_refuses_rather_than_transcoding():
    aac_only = [f for f in FREE_FORMATS if f["acodec"].startswith("mp4a")]
    with pytest.raises(NoUsableStream, match="second lossy generation"):
        select(parse_streams(aac_only), "opus-native")


def test_user_profile_overrides_detected_environment():
    result = make_result(FREE_FORMATS, cookies=False)
    assert resolve(result, environment=APPLE_ENV).profile_source == "environment"
    assert resolve(result, environment=APPLE_ENV).stream.itag == "140"

    override = resolve(result, profile="archive", environment=APPLE_ENV)
    assert override.profile_source == "user"
    assert override.stream.itag == "251"


# --------------------------------------------------------------------------
# Entitlement: the silent-downgrade trap
# --------------------------------------------------------------------------

def test_rejected_cookies_raise_a_warning_not_silence():
    entitlement = assess(
        parse_streams(FREE_FORMATS), cookies_configured=True, stderr=COOKIE_REJECTION
    )
    assert entitlement.tier is Tier.FREE
    assert entitlement.cookies_accepted is False
    assert entitlement.silently_downgraded
    assert len(entitlement.warnings) == 1


def test_accepted_cookies_on_ineligible_content_is_info_not_warning():
    """The confounder: valid credentials, no 256k, nothing actually wrong."""
    entitlement = assess(
        parse_streams(FREE_FORMATS), cookies_configured=True, stderr=""
    )
    assert entitlement.cookies_accepted is True
    assert not entitlement.silently_downgraded
    assert not entitlement.warnings
    assert entitlement.diagnostics[0].severity is Severity.INFO


def test_premium_streams_are_recognised():
    entitlement = assess(
        parse_streams(PREMIUM_FORMATS), cookies_configured=True, stderr=""
    )
    assert entitlement.tier is Tier.PREMIUM
    assert {s.itag for s in entitlement.premium_streams} == {"141", "774"}
    assert not entitlement.warnings


def test_anonymous_probe_is_never_reported_as_a_credential_failure():
    entitlement = assess(
        parse_streams(FREE_FORMATS), cookies_configured=False, stderr=""
    )
    assert entitlement.cookies_accepted is None
    assert not entitlement.silently_downgraded
    assert not entitlement.warnings


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: could not find chrome cookies database",
        "WARNING: The cookies are no longer valid",
        "ERROR: Sign in to confirm you're not a bot",
        "unable to load cookies from firefox",
    ],
)
def test_cookie_failure_wordings_are_detected(stderr):
    assert not cookies_were_accepted(stderr)


def test_ordinary_warnings_are_not_mistaken_for_cookie_failure():
    noise = (
        "WARNING: [youtube] Falling back to generic n function search\n"
        "WARNING: [youtube] Some tv client https formats have been skipped\n"
    )
    assert cookies_were_accepted(noise)


# --------------------------------------------------------------------------
# Explanations must not misattribute a codec preference to compatibility
# --------------------------------------------------------------------------

def test_codec_preference_is_not_reported_as_a_compatibility_sacrifice():
    selection = select(parse_streams(FREE_FORMATS), "archive")
    assert selection.stream.itag == "251"
    reason = selection.explain()
    assert "lower-preference codec" in reason
    assert "cannot decode" not in reason


def test_genuine_compatibility_sacrifice_is_reported_as_such():
    selection = select(parse_streams(PREMIUM_FORMATS), "apple")
    # 774 (Opus 256k) ties 141 on bitrate, so force a clearly-higher Opus.
    louder = PREMIUM_FORMATS + [
        {"format_id": "999", "acodec": "opus", "vcodec": "none", "abr": 400.0,
         "asr": 48000, "ext": "webm", "filesize": 9_000_000}
    ]
    selection = select(parse_streams(louder), "apple")
    assert selection.stream.family == "aac"
    assert "cannot decode" in selection.explain()


def test_left_on_table_is_zero_when_compatibility_costs_nothing():
    decision = resolve(make_result(PREMIUM_FORMATS, cookies=True), profile="apple")
    assert decision.left_on_table == pytest.approx(0.0)
