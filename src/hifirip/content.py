"""Content classification -- what handling does this upload need?

The taxonomy here is derived from a survey of 720 sampled YouTube uploads
across 21 provisional groupings (`tools/survey_content.py`), not from
intuition. The first version of this module classified on duration buckets
and was wrong on two of the first four real videos tried. The survey shows
why that was not a tuning problem:

    documentary vs full-album       89% duration overlap
    documentary vs compilation      90%
    full-concert vs dj-set          95%
    live-single-song vs official    94%
    podcast vs dj-set               84%

Duration simply does not separate the classes that need different handling,
anywhere in the 20-160 minute band where the hard cases live. It is
informative only at the extremes: Shorts have a p90 of 3.4 minutes, ambient
longform a median of 484. So duration is used here as a weak prior at the
tails and never as a primary discriminator.

Two more survey results shaped this:

  * `track`/`album` metadata appeared in 0 of 84 detail-probed videos, so
    its *presence* is meaningful but its absence says nothing at all;
  * chapters appear in ~75% of radio shows, albums and analysis videos and
    0% of DJ sets, aftermovies, official videos and mashups -- again, a
    positive signal only.

What actually discriminates is the title lexicon, which is exactly the
signal a language model reads well and a regex list reads badly. Rather
than grow a pattern treadmill, the deterministic layer here is deliberately
thin: it resolves the clear cases cheaply and *defers* the rest, first to
the host agent and then, if that is still unsure, to sampled audio.

The classes are kinds of handling, not genres. Two uploads belong to the
same class when the same pipeline is correct for both.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContentClass(Enum):
    #: One complete recording: official video, lyric video, audio-only
    #: upload, cover, karaoke, single live song.
    SINGLE_RECORDING = "single-recording"
    #: Part of a recording: Shorts, clips, snippets. Never a whole track.
    FRAGMENT = "fragment"
    #: Separated tracks in one upload: album, compilation, OST, classical
    #: with movements. Silence between tracks is real.
    DISCRETE_MULTITRACK = "discrete-multitrack"
    #: Beatmatched and crossfaded: DJ sets, radio shows, megamixes.
    CONTINUOUS_MIX = "continuous-mix"
    #: Concerts: songs separated by applause and banter, not beatmatched
    #: and not silent. Authority is setlist.fm, not release databases.
    LIVE_PERFORMANCE = "live-performance"
    #: Speech-dominant: documentaries, podcasts, interviews, analysis,
    #: reactions. Music is incidental and usually overlaid with narration.
    SPEECH_DOMINANT = "speech-dominant"
    #: Many short snippets plus substantial non-music: aftermovies, recaps.
    MONTAGE = "montage"
    #: Hours-long curated or generated audio: lofi radio, sleep and study
    #: mixes. Frequently has no meaningful tracklist at all.
    AMBIENT_LONGFORM = "ambient-longform"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Handling:
    """What this class implies for the pipeline."""

    summary: str
    #: True = split, False = never split, None = ask the user.
    splits: bool | None
    #: What a boundary physically is here.
    boundary_kind: str
    #: Metadata sources worth consulting, most authoritative first.
    sources: tuple[str, ...]
    #: The splitting tradeoff, stated as consequences rather than jargon.
    splitting_note: str = ""


HANDLING: dict[ContentClass, Handling] = {
    ContentClass.SINGLE_RECORDING: Handling(
        summary="a single complete recording",
        splits=False,
        boundary_kind="none",
        sources=("musicbrainz", "discogs", "lastfm", "yt_description"),
        splitting_note="No splitting applies; only metadata and cover art need resolving.",
    ),
    ContentClass.FRAGMENT: Handling(
        summary="a fragment of a recording, not a complete track",
        splits=False,
        boundary_kind="none",
        sources=("acoustid", "musicbrainz", "yt_description"),
        splitting_note=(
            "This is a clip, so you will get part of a song rather than the "
            "song. Metadata is resolved against the source recording it was "
            "cut from, not the clip's own title."
        ),
    ),
    ContentClass.DISCRETE_MULTITRACK: Handling(
        summary="separate tracks collected in one upload",
        splits=True,
        boundary_kind="silence",
        sources=("yt_chapters", "yt_description", "musicbrainz", "discogs",
                 "silence", "yt_comments"),
        splitting_note=(
            "Tracks here are separated rather than mixed, so splitting is "
            "clean: cuts land in the gaps and each file holds one track and "
            "nothing else."
        ),
    ),
    ContentClass.CONTINUOUS_MIX: Handling(
        summary="a continuously mixed set",
        splits=None,
        boundary_kind="interval",
        sources=("1001tracklists", "yt_comments", "yt_description",
                 "yt_chapters", "acoustid", "sponsorblock"),
        splitting_note=(
            "Because the tracks are beatmatched into each other, boundaries "
            "are ranges rather than points -- across a transition both tracks "
            "are genuinely playing at once. Splitting means each file starts "
            "and ends with its neighbour audible; that is not an error, it is "
            "what the audio contains. Keeping one file with chapter markers "
            "stays skippable in any modern player, preserves the transitions "
            "the artist built, and cannot cut in the wrong place. Splitting "
            "gives real per-track files; nothing is re-encoded and no silence "
            "is inserted, and rejoining them restores the original audio byte "
            "for byte, though cuts land on packet edges rather than exact "
            "sample positions."
        ),
    ),
    ContentClass.LIVE_PERFORMANCE: Handling(
        summary="a live concert performance",
        splits=None,
        boundary_kind="applause",
        sources=("setlistfm", "yt_chapters", "yt_description", "yt_comments",
                 "musicbrainz", "acoustid"),
        splitting_note=(
            "Songs are separated by applause and stage banter rather than "
            "silence, so a cut has to land somewhere inside the crowd noise. "
            "Wherever it lands, one file keeps the applause that belongs to "
            "the song before it. Setlists give the running order; they do not "
            "give exact timings, because live arrangements differ from studio "
            "lengths."
        ),
    ),
    ContentClass.SPEECH_DOMINANT: Handling(
        summary="speech-dominant material with incidental music",
        splits=False,
        boundary_kind="none",
        sources=("yt_chapters", "yt_description"),
        splitting_note=(
            "This is mostly talking. Music appears under narration and in "
            "short cues, so there are no clean tracks to extract -- attempting "
            "it would produce fragments with speech over them. Chapter markers "
            "are offered instead of splitting."
        ),
    ),
    ContentClass.MONTAGE: Handling(
        summary="a montage of short clips with substantial non-music audio",
        splits=False,
        boundary_kind="none",
        sources=("yt_description", "yt_comments"),
        splitting_note=(
            "This cuts between many brief snippets, interleaved with crowd "
            "noise, narration and transitions. Splitting would yield dozens of "
            "fragments, most of them a few seconds long and many not music at "
            "all, so it is kept as one file."
        ),
    ),
    ContentClass.AMBIENT_LONGFORM: Handling(
        summary="hours-long continuous ambient or background audio",
        splits=None,
        boundary_kind="interval",
        sources=("yt_chapters", "yt_description", "yt_comments", "acoustid"),
        splitting_note=(
            "Uploads like this often have no meaningful tracklist -- the audio "
            "may be curated, looped, or generated, with no discrete tracks to "
            "recover. Splitting is only worth attempting if a tracklist is "
            "actually found."
        ),
    ),
    ContentClass.UNKNOWN: Handling(
        summary="material that could not be confidently classified",
        splits=None,
        boundary_kind="unknown",
        sources=("yt_chapters", "yt_description", "yt_comments"),
        splitting_note=(
            "Evidence was mixed or absent, so splitting decisions should be "
            "confirmed rather than assumed."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Lexical signals
#
# Ordered by how decisively the survey showed each token separates classes.
# This layer is intentionally small. It exists to resolve the obvious cases
# without a model call, not to cover the space -- coverage is the escalation
# path's job, and every pattern added here is a pattern to maintain forever.
# ---------------------------------------------------------------------------

LEXICON: list[tuple[re.Pattern[str], ContentClass, float]] = [
    (re.compile(r"#shorts|\bshorts?\b|\bsnippet\b|\bclip\b", re.I),
     ContentClass.FRAGMENT, 3.0),
    (re.compile(r"\bdocumentar(y|ies)\b|\bmaking of\b|\bthe story of\b"
                r"|\bbehind the (scenes|music)\b", re.I),
     ContentClass.SPEECH_DOMINANT, 4.0),
    (re.compile(r"\bpodcast\b|\binterview\b|\bin conversation\b|\bep(isode)? ?\d+\b"
                r"|\breaction\b|\bfirst time (hearing|listening)\b"
                r"|\bexplained\b|\banalysis\b|\bmusic theory\b", re.I),
     ContentClass.SPEECH_DOMINANT, 3.5),
    (re.compile(r"\baftermovie\b|\brecap\b|\bhighlights\b|\btrailer\b", re.I),
     ContentClass.MONTAGE, 4.0),
    (re.compile(r"\bdj[\s-]?set\b|\bboiler ?room\b|\bessential mix\b|\bb2b\b"
                r"|\bmixed by\b|\bcontinuous mix\b|\bmegamix\b|\bmixtape\b"
                r"|\ba state of trance\b|\bmainstage\b", re.I),
     ContentClass.CONTINUOUS_MIX, 4.0),
    (re.compile(r"\bfull (concert|show|gig)\b|\blive concert\b|\bsetlist\b"
                r"|\btiny desk\b|\bunplugged\b|\blive in concert\b", re.I),
     ContentClass.LIVE_PERFORMANCE, 3.5),
    # "Live from/at <venue>" is deliberately weak. Bands and DJs both use it,
    # and the survey put concerts and DJ sets at 95% duration overlap, so
    # nothing else in the metadata breaks the tie. Scored low enough that it
    # escalates on its own rather than picking a pipeline by coin flip.
    (re.compile(r"\blive (at|in|from)\b", re.I),
     ContentClass.LIVE_PERFORMANCE, 2.0),
    (re.compile(r"\bfull album\b|\bcomplete album\b|\balbum stream\b"
                r"|\bgreatest hits\b|\bfull (soundtrack|ost)\b|\bdiscography\b"
                r"|\bcompilation\b|\bsymphony\b|\bconcerto\b|\bopera\b", re.I),
     ContentClass.DISCRETE_MULTITRACK, 3.5),
    (re.compile(r"\blofi\b|\blo-fi\b|\bsleep (music|sounds)\b|\bstudy (music|beats)\b"
                r"|\bwhite noise\b|\b\d+ hours?\b|\bfocus music\b|\brelaxing\b", re.I),
     ContentClass.AMBIENT_LONGFORM, 3.5),
    (re.compile(r"\bofficial (music )?video\b|\blyric video\b|\bofficial audio\b"
                r"|\bkaraoke\b|\bbacking track\b|\binstrumental\b|\bcover\b", re.I),
     ContentClass.SINGLE_RECORDING, 3.0),
]

#: Duration is only trusted outside this band. Inside it, the survey shows
#: overlaps of 84-95% between classes needing different pipelines.
AMBIGUOUS_MINUTES = (4.0, 130.0)
#: Song-length band, used to judge whether chapters look like tracks.
TRACK_SECONDS = (90.0, 480.0)

#: Evidence weight at which a classification is well-supported: one strong
#: lexical hit plus something corroborating.
CONFIDENT_MASS = 5.0
#: Below this we name no class and escalate instead.
UNCERTAIN_BELOW = 0.45


@dataclass
class Signal:
    name: str
    detail: str
    supports: ContentClass | None
    weight: float

    def __str__(self) -> str:
        return self.detail


@dataclass
class Classification:
    content_class: ContentClass
    confidence: float
    signals: list[Signal] = field(default_factory=list)
    duration: float = 0.0
    chapter_count: int = 0
    description_timestamps: int = 0
    title: str = ""
    #: Set when a host agent or audio pass resolved what rules could not.
    resolved_by: str = "rules"

    @property
    def handling(self) -> Handling:
        return HANDLING[self.content_class]

    @property
    def needs_escalation(self) -> bool:
        return self.content_class is ContentClass.UNKNOWN

    @property
    def is_multitrack(self) -> bool:
        return self.content_class in (
            ContentClass.DISCRETE_MULTITRACK,
            ContentClass.CONTINUOUS_MIX,
            ContentClass.LIVE_PERFORMANCE,
            ContentClass.AMBIENT_LONGFORM,
        )

    @property
    def has_deterministic_boundaries(self) -> bool:
        return self.chapter_count > 1 or self.description_timestamps > 1

    def supporting(self) -> list[Signal]:
        return [s for s in self.signals if s.supports is self.content_class]

    def briefing(self) -> str:
        """A splitting explanation grounded in this specific upload.

        Deliberately not generic: it names what was and was not found here so
        the user is choosing between outcomes rather than acknowledging a
        warning they have read before.
        """
        minutes = self.duration / 60
        handling = self.handling
        lines = [f"This is a {minutes:.0f}-minute upload: {handling.summary}."]

        if self.is_multitrack and not self.has_deterministic_boundaries:
            missing = []
            if not self.chapter_count:
                missing.append("no chapter markers")
            if not self.description_timestamps:
                missing.append("no timestamps in the description")
            if missing:
                lines.append(
                    f"It has {' and '.join(missing)}, so any boundaries would "
                    f"come from {', '.join(handling.sources[:2])} rather than "
                    f"from anything the uploader stated."
                )
        elif self.chapter_count > 1:
            lines.append(
                f"The uploader supplied {self.chapter_count} chapter markers, "
                f"so boundaries are stated rather than guessed."
            )
        elif self.description_timestamps > 1:
            lines.append(
                f"The description carries {self.description_timestamps} "
                f"timestamps, which give boundaries directly."
            )

        if handling.splitting_note:
            lines.append(handling.splitting_note)
        if self.supporting():
            lines.append(
                "(Classified from: "
                + "; ".join(s.detail for s in self.supporting())
                + ".)"
            )
        if self.resolved_by != "rules":
            lines.append(f"(Class resolved by: {self.resolved_by}.)")
        return "\n".join(lines)

    def escalation_brief(self) -> str:
        """Structured evidence for a host agent to classify from.

        Reached when the cheap rules decline to guess. The host reads titles
        far better than a regex list does, which is the whole reason this
        path exists rather than a longer lexicon.
        """
        options = "\n".join(
            f"  - {c.value}: {HANDLING[c].summary}"
            for c in ContentClass if c is not ContentClass.UNKNOWN
        )
        observed = [
            f"  title: {self.title!r}",
            f"  duration_minutes: {self.duration / 60:.1f}",
            f"  chapters: {self.chapter_count}",
            f"  description_timestamps: {self.description_timestamps}",
        ]
        weak = "; ".join(s.detail for s in self.signals) or "none"
        return (
            "Deterministic rules could not classify this upload "
            f"(confidence {self.confidence:.2f} < {UNCERTAIN_BELOW}).\n\n"
            "Observed:\n" + "\n".join(observed) + "\n\n"
            f"Weak or conflicting signals: {weak}\n\n"
            "Choose one class:\n" + options + "\n\n"
            "Duration is a poor discriminator here -- in a survey of 720 "
            "uploads, documentaries and albums overlapped 89% by runtime, and "
            "concerts and DJ sets 95%. Weigh the title and description "
            "wording first. If they do not settle it, say so rather than "
            "guessing: sampled audio can then be used to measure the "
            "speech-to-music ratio and the length of musical segments, which "
            "separates the remaining cases."
        )

    def audio_probe_plan(self, windows: int = 6, seconds: float = 20.0) -> list[float]:
        """Offsets to sample when title and metadata are not enough.

        Evenly spaced, skipping the opening, which is unrepresentative:
        intros, crowd noise and channel idents sit there in every class.

        Short uploads are sampled whole rather than sliced. Asking for six
        twenty-second windows out of a thirty-second clip yields six nearly
        identical overlapping reads -- six times the cost of just analysing
        the file, for less coverage than doing so.
        """
        if self.duration <= 0:
            return []
        if self.duration <= seconds * 2:
            return [0.0]

        start = min(60.0, self.duration * 0.05)
        span = self.duration - start - seconds
        if span <= 0:
            return [0.0]
        # Never return overlapping windows: they cost a read each and cover
        # ground already sampled.
        windows = max(1, min(windows, int(span // seconds) + 1))
        if windows == 1:
            return [round(start, 1)]
        return [round(start + span * i / (windows - 1), 1) for i in range(windows)]


_TIMESTAMP_LINE = re.compile(r"(?m)^\s*\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?")


def count_timestamps(description: str) -> int:
    """Count line-leading timestamps, the shape a tracklist actually takes."""
    return len(_TIMESTAMP_LINE.findall(description or ""))


def classify(info: dict[str, Any]) -> Classification:
    """Classify from yt-dlp metadata alone, or decline and escalate.

    Audio is never fetched here: classification has to run before the
    download so it can choose sources and shape what we ask the user, and a
    three-hour download is far too expensive for a question the title
    usually answers.
    """
    title = info.get("title") or ""
    duration = float(info.get("duration") or 0.0)
    chapters = info.get("chapters") or []
    timestamps = count_timestamps(info.get("description") or "")
    minutes = duration / 60

    signals: list[Signal] = []
    scores: dict[ContentClass, float] = {}

    def add(name: str, detail: str, supports: ContentClass | None, weight: float):
        signals.append(Signal(name, detail, supports, weight))
        if supports is not None:
            scores[supports] = scores.get(supports, 0.0) + weight

    # -- Title lexicon: the strongest available signal -------------------
    for pattern, klass, weight in LEXICON:
        match = pattern.search(title)
        if match:
            add("title", f"the title says {match.group(0).strip()!r}", klass, weight)

    # -- Duration, at the tails only --------------------------------------
    if duration:
        if minutes < 1.5:
            # The one place duration is decisive: the survey put Shorts and
            # clips at a p90 of 3.4 minutes, and nothing else lives here.
            add("duration",
                f"it runs {minutes:.1f} minutes, far too short for a full track",
                ContentClass.FRAGMENT, 3.0)
        elif minutes < AMBIGUOUS_MINUTES[0]:
            add("duration",
                f"it runs {minutes:.1f} minutes, a single-track length",
                ContentClass.SINGLE_RECORDING, 2.0)
        elif minutes > AMBIGUOUS_MINUTES[1]:
            add("duration",
                f"it runs {minutes:.0f} minutes, beyond concert or album length",
                ContentClass.AMBIENT_LONGFORM, 1.5)
        else:
            # Explicitly recorded as uninformative, so the briefing can say so.
            add("duration",
                f"it runs {minutes:.0f} minutes, which the survey shows does "
                f"not distinguish these classes",
                None, 0.0)

    # -- Chapters: a positive signal only ---------------------------------
    if len(chapters) > 1:
        lengths = [
            float(c.get("end_time", 0)) - float(c.get("start_time", 0))
            for c in chapters
        ]
        lengths = [x for x in lengths if x > 0]
        median = statistics.median(lengths) if lengths else 0.0
        if TRACK_SECONDS[0] <= median <= TRACK_SECONDS[1]:
            add("chapters",
                f"{len(chapters)} chapters averaging {median / 60:.1f} minutes, "
                f"consistent with individual tracks",
                ContentClass.DISCRETE_MULTITRACK, 2.0)

    # -- YouTube track metadata: rare, so meaningful when present ---------
    if info.get("track") or info.get("album"):
        add("metadata",
            "YouTube supplied track/album metadata, which it attaches to "
            "identified single recordings",
            ContentClass.SINGLE_RECORDING, 2.0)

    base = Classification(
        content_class=ContentClass.UNKNOWN,
        confidence=0.0,
        signals=signals,
        duration=duration,
        chapter_count=len(chapters),
        description_timestamps=timestamps,
        title=title,
    )
    if not scores:
        return base

    best = max(scores, key=lambda c: scores[c])
    confidence = _confidence(scores[best], sum(scores.values()))
    if confidence < UNCERTAIN_BELOW:
        base.confidence = confidence
        return base

    base.content_class = best
    base.confidence = confidence
    return base


def _confidence(best: float, total: float) -> float:
    """Combine share of evidence with how much evidence there was.

    Share alone is badly calibrated: a lone unopposed signal scores 1.0,
    indistinguishable from a classification backed by title, chapters and
    metadata together. Since low confidence is what triggers escalation,
    that overconfidence would suppress exactly the deferrals we want.
    """
    share = best / total if total else 0.0
    saturation = min(1.0, best / CONFIDENT_MASS)
    return round(share * saturation, 3)


def apply_host_decision(
    classification: Classification, content_class: ContentClass, note: str = ""
) -> Classification:
    """Fold a host agent's judgement back into a classification."""
    classification.content_class = content_class
    classification.confidence = 1.0 if content_class is not ContentClass.UNKNOWN else 0.0
    classification.resolved_by = f"host agent{f' ({note})' if note else ''}"
    return classification
