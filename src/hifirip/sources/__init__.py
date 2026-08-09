"""Metadata sources, their reliability, and which content they apply to.

A source's weight is a claim about its track record, not its confidence.
Curated databases with editorial review sit near the top; crowd-sourced text
that anyone can type sits near the bottom. Weight decides how persuasive a
source is in `consensus.reconcile` -- it never decides a field alone, because
no single source is allowed to carry consensus regardless of its authority.

Routing matters as much as weighting. Release databases give exact track
durations for an album and are worthless for a DJ set, where tracks are mixed
in late, cut out early, or looped. Setlist.fm knows what a band played and
nothing about when. Consulting a source outside its competence does not
merely waste a request; it injects confident noise into a weighted vote.

Sources needing credentials degrade gracefully: they activate when a key is
present and are simply absent otherwise, so an install with no keys at all
still works with a smaller, entirely functional set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..content import ContentClass


class Kind(Enum):
    #: Identity of a recording: title, artist, album, date.
    METADATA = "metadata"
    #: Where tracks begin and end within one upload.
    TRACKLIST = "tracklist"
    #: Non-music regions.
    SEGMENTS = "segments"


@dataclass(frozen=True)
class SourceSpec:
    name: str
    kind: Kind
    #: Reliability, 0-1. See module docstring: track record, not certainty.
    weight: float
    #: Content classes this source can speak to competently.
    applies_to: frozenset[ContentClass]
    rationale: str
    requires_key: str | None = None
    #: Sources this one derives from. Agreement with an upstream is not
    #: corroboration, so `consensus` discounts it.
    derives_from: frozenset[str] = frozenset()

    @property
    def needs_credentials(self) -> bool:
        return self.requires_key is not None


MULTITRACK = frozenset({
    ContentClass.DISCRETE_MULTITRACK,
    ContentClass.CONTINUOUS_MIX,
    ContentClass.LIVE_PERFORMANCE,
    ContentClass.AMBIENT_LONGFORM,
})
RECORDINGS = frozenset({ContentClass.SINGLE_RECORDING, ContentClass.FRAGMENT})
EVERYTHING = MULTITRACK | RECORDINGS


REGISTRY: dict[str, SourceSpec] = {
    "musicbrainz": SourceSpec(
        name="musicbrainz", kind=Kind.METADATA, weight=1.00,
        applies_to=RECORDINGS | {ContentClass.DISCRETE_MULTITRACK},
        rationale=(
            "Open, editorially reviewed, with a stable identifier scheme and "
            "visible edit history. Release durations line up with discrete "
            "album uploads almost exactly, and mean nothing for mixed sets."
        ),
    ),
    "discogs": SourceSpec(
        name="discogs", kind=Kind.METADATA, weight=0.95,
        applies_to=RECORDINGS | {ContentClass.DISCRETE_MULTITRACK},
        rationale=(
            "Human-curated per physical release, so pressing-specific detail "
            "is unusually good. Slightly below MusicBrainz because entries "
            "are release-scoped and duplicates are common."
        ),
        requires_key="DISCOGS_TOKEN",
    ),
    "setlistfm": SourceSpec(
        name="setlistfm", kind=Kind.TRACKLIST, weight=0.90,
        applies_to=frozenset({ContentClass.LIVE_PERFORMANCE}),
        rationale=(
            "The authority on what a band played and in what order. It gives "
            "no timings at all -- live arrangements differ from studio "
            "lengths -- so it constrains order, never boundaries."
        ),
        requires_key="SETLISTFM_KEY",
    ),
    "1001tracklists": SourceSpec(
        name="1001tracklists", kind=Kind.TRACKLIST, weight=0.90,
        applies_to=frozenset({ContentClass.CONTINUOUS_MIX}),
        rationale=(
            "The best available record of what was played in a DJ set, and "
            "frequently the only one. Weighted high, but deliberately not "
            "high enough to settle a field alone: it is community-submitted, "
            "and a single unreviewed submission should never decide a "
            "tracklist by itself."
        ),
    ),
    "acoustid": SourceSpec(
        name="acoustid", kind=Kind.METADATA, weight=0.85,
        applies_to=EVERYTHING,
        rationale=(
            "Acoustic fingerprinting, so it identifies what is actually "
            "playing rather than what someone typed. Below the curated "
            "databases only because it resolves to a recording that may have "
            "many releases, and remixes fingerprint close to originals."
        ),
        requires_key="ACOUSTID_KEY",
        derives_from=frozenset({"musicbrainz"}),
    ),
    "yt_chapters": SourceSpec(
        name="yt_chapters", kind=Kind.TRACKLIST, weight=0.80,
        applies_to=MULTITRACK,
        rationale=(
            "Stated by the uploader, who knows what they uploaded. Machine "
            "readable and unambiguous when present -- but present for only "
            "about a quarter of multitrack uploads, and absent entirely from "
            "DJ sets in the survey sample."
        ),
    ),
    "sponsorblock": SourceSpec(
        name="sponsorblock", kind=Kind.SEGMENTS, weight=0.70,
        applies_to=EVERYTHING,
        rationale=(
            "Crowd-sourced but vote-moderated, and it answers a narrow "
            "question -- where music is not playing -- rather than asserting "
            "identity."
        ),
    ),
    "yt_description": SourceSpec(
        name="yt_description", kind=Kind.TRACKLIST, weight=0.60,
        applies_to=EVERYTHING,
        rationale=(
            "Uploader-written free text. Authoritative when it carries a real "
            "tracklist, unreliable in format, and often full of promotional "
            "material that parses as timestamps."
        ),
    ),
    "lastfm": SourceSpec(
        name="lastfm", kind=Kind.METADATA, weight=0.55,
        applies_to=RECORDINGS,
        rationale=(
            "Aggregated listening data, useful for disambiguating popular "
            "spellings. Much of its catalogue mirrors MusicBrainz, so it is "
            "marked as derived and does not corroborate its own upstream."
        ),
        requires_key="LASTFM_KEY",
        derives_from=frozenset({"musicbrainz"}),
    ),
    "silence": SourceSpec(
        name="silence", kind=Kind.TRACKLIST, weight=0.50,
        applies_to=frozenset({ContentClass.DISCRETE_MULTITRACK}),
        rationale=(
            "Detects gaps in the audio itself, so it is objective and needs "
            "no network. It finds nothing in a beatmatched mix, which has no "
            "silence anywhere, and so applies only to discrete uploads."
        ),
    ),
    "yt_comments": SourceSpec(
        name="yt_comments", kind=Kind.TRACKLIST, weight=0.40,
        applies_to=MULTITRACK,
        rationale=(
            "Fan-written tracklists, which for popular sets are often the "
            "only timings in existence and are startlingly accurate. Weighted "
            "lowest because any individual comment is unverified; upvotes "
            "help but are not review."
        ),
    ),
}


def derivation_map() -> dict[str, set[str]]:
    """What `consensus.reconcile` needs to avoid double-counting mirrors."""
    return {
        spec.name: set(spec.derives_from)
        for spec in REGISTRY.values() if spec.derives_from
    }


def sources_for(
    content_class: ContentClass,
    *,
    available_keys: frozenset[str] = frozenset(),
    enabled: tuple[str, ...] | None = None,
) -> list[SourceSpec]:
    """Sources competent for this content and usable in this install.

    Ordered most authoritative first, purely for readable output; weighting,
    not ordering, is what decides a value.
    """
    chosen = []
    for spec in REGISTRY.values():
        if enabled is not None and spec.name not in enabled:
            continue
        if content_class not in spec.applies_to:
            continue
        if spec.needs_credentials and spec.requires_key not in available_keys:
            continue
        chosen.append(spec)
    return sorted(chosen, key=lambda spec: -spec.weight)


def unavailable_for(
    content_class: ContentClass, *, available_keys: frozenset[str] = frozenset()
) -> list[SourceSpec]:
    """Competent sources that are switched off for want of a key.

    Reported so a user can see what a key would buy them, rather than
    silently receiving a weaker result.
    """
    return [
        spec for spec in REGISTRY.values()
        if content_class in spec.applies_to
        and spec.needs_credentials
        and spec.requires_key not in available_keys
    ]


def describe_routing(
    content_class: ContentClass, *, available_keys: frozenset[str] = frozenset()
) -> str:
    active = sources_for(content_class, available_keys=available_keys)
    missing = unavailable_for(content_class, available_keys=available_keys)

    lines = [f"Sources for {content_class.value}:"]
    for spec in active:
        lines.append(f"  {spec.weight:.2f}  {spec.name:16s} {spec.kind.value}")
    if missing:
        lines.append("Inactive (no credentials):")
        for spec in missing:
            lines.append(f"  {spec.weight:.2f}  {spec.name:16s} set {spec.requires_key}")
    if not active:
        lines.append("  none -- this content class has no competent sources configured")
    return "\n".join(lines)
