"""Audio stream selection.

The governing rule of this project: **never transcode.**

YouTube does not derive its audio streams from one another. It publishes
several *independent* encodes of the same master -- Opus in WebM, AAC in
MP4, occasionally Vorbis or EC-3. Choosing an output container is
therefore a matter of picking the right source stream and remuxing it
with `-c copy`, not of converting one encode into another. Converting
Opus to AAC would stack a second lossy generation on top of the first and
buy nothing; converting either to FLAC or ALAC would produce a file five
times larger with provably identical information content.

So: the destination decides the family, the family decides the stream,
and ffmpeg only ever copies packets.

Note that itags are deliberately *not* the selection key. YouTube adds,
retires, and silently re-encodes itags, so we classify on the codec and
bitrate that yt-dlp actually reports for the video in hand, and use the
itag table below only to annotate output for humans.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

Family = Literal["opus", "aac", "vorbis", "other"]

# Annotation only -- never used to choose a stream. See module docstring.
KNOWN_ITAGS: dict[str, str] = {
    "139": "AAC HE-AACv1 ~48k (mp4)",
    "140": "AAC-LC 128k (mp4)",
    "141": "AAC 256k (mp4, YouTube Music/Premium)",
    "171": "Vorbis 128k (webm, legacy)",
    "172": "Vorbis 256k (webm, legacy)",
    "249": "Opus ~50k (webm)",
    "250": "Opus ~70k (webm)",
    "251": "Opus ~160k (webm)",
    "774": "Opus ~256k (webm, YouTube Music/Premium)",
}

#: Remux target per codec family. Container change only, `-c copy`.
CONTAINER: dict[Family, str] = {
    "opus": "opus",   # Ogg Opus
    "aac": "m4a",
    "vorbis": "ogg",
    "other": "mka",   # Matroska holds anything we failed to classify
}

#: Bitrate above which a stream could not have come from the free tier.
#: The free ceilings are Opus ~160k and AAC 128k, so anything meaningfully
#: above 160k implies an entitled (Premium / YouTube Music) probe.
PREMIUM_ABR_THRESHOLD = 180.0


def classify(acodec: str | None) -> Family:
    """Map a yt-dlp `acodec` string onto a codec family.

    yt-dlp reports things like "opus", "mp4a.40.2", "mp4a.40.5", "vorbis",
    "ec-3", or "none". Anything unrecognised lands in "other", which is
    still remuxable -- we just can't reason about its compatibility.
    """
    if not acodec or acodec == "none":
        return "other"
    codec = acodec.lower()
    if codec.startswith("opus"):
        return "opus"
    if codec.startswith(("mp4a", "aac")):
        return "aac"
    if codec.startswith("vorbis"):
        return "vorbis"
    return "other"


@dataclass(frozen=True)
class AudioStream:
    """One audio-only format offered by YouTube for a given video."""

    itag: str
    acodec: str
    family: Family
    abr: float | None       # kbps, as reported
    asr: int | None         # sample rate, Hz
    ext: str
    filesize: int | None
    language: str | None = None

    @property
    def container(self) -> str:
        return CONTAINER[self.family]

    @property
    def note(self) -> str:
        return KNOWN_ITAGS.get(self.itag, f"{self.acodec} {self.abr or '?'}k")

    @property
    def is_premium_tier(self) -> bool:
        """True if this stream is only served to entitled accounts."""
        return (self.abr or 0.0) >= PREMIUM_ABR_THRESHOLD

    def __str__(self) -> str:
        rate = f"{self.abr:.0f}k" if self.abr else "?k"
        return f"itag {self.itag} [{self.family} {rate} @ {self.asr or '?'}Hz]"


@dataclass(frozen=True)
class Profile:
    """A destination's codec constraints.

    `families` is a strict preference order. A profile that omits a family
    will never be served that family, even if it is the only stream with a
    higher bitrate -- an unplayable file is not a better file.
    """

    name: str
    families: tuple[Family, ...]
    description: str
    strict: bool = True

    def rank(self, stream: AudioStream) -> tuple[int, float]:
        """Sort key: family preference first, then measured bitrate.

        Ranking on measured bitrate is what makes the free/Premium split
        work without branching: when an entitled probe surfaces a 256k
        stream it simply outranks the free-tier one inside the same family.
        """
        try:
            preference = self.families.index(stream.family)
        except ValueError:
            preference = len(self.families)
        return (preference, -(stream.abr or 0.0))


PROFILES: dict[str, Profile] = {
    "archive": Profile(
        name="archive",
        families=("opus", "aac", "vorbis"),
        description=(
            "Opus preferred, then highest measured bitrate. Opus is chosen even "
            "when an AAC stream reports a slightly higher bitrate, because Opus "
            "beats AAC at equal bitrate in blind listening tests -- a nominally "
            "larger AAC number is not more quality. Plays on Android, Plex, "
            "Navidrome, VLC, and every browser; not in Apple Music or on Sonos."
        ),
        strict=False,
    ),
    "apple": Profile(
        name="apple",
        families=("aac",),
        description=(
            "AAC only, remuxed to .m4a. Required for Apple Music.app, iOS, and "
            "CarPlay, none of which decode Opus. With YouTube Music credentials "
            "this reaches 256k AAC; without them, 128k."
        ),
    ),
    "windows": Profile(
        name="windows",
        families=("aac", "opus"),
        description=(
            "AAC preferred for .m4a files that every Windows player indexes and "
            "plays. Falls back to Opus, which Windows 11's Media Player handles "
            "but older players and most car head units do not."
        ),
        strict=False,
    ),
    "sonos": Profile(
        name="sonos",
        families=("aac",),
        description="AAC only. Sonos has never shipped an Opus decoder.",
    ),
    "opus-native": Profile(
        name="opus-native",
        families=("opus",),
        description=(
            "Opus only, failing loudly if unavailable. For libraries that have "
            "standardised on Opus and want no silent container drift."
        ),
    ),
}

DEFAULT_PROFILE = "archive"


class NoUsableStream(RuntimeError):
    """Raised when no offered stream satisfies the profile's constraints."""


@dataclass
class Selection:
    """A chosen stream, plus the reasoning that produced it."""

    stream: AudioStream
    profile: Profile
    considered: Sequence[AudioStream] = field(default_factory=tuple)

    @property
    def rejected(self) -> list[AudioStream]:
        return [s for s in self.considered if s is not self.stream]

    @property
    def best_available_abr(self) -> float:
        return max((s.abr or 0.0) for s in self.considered) if self.considered else 0.0

    def rejection_reason(self, other: AudioStream) -> str:
        """Why `other` lost, distinguishing the two very different causes.

        A stream can be passed over because the destination cannot decode it
        (a real compatibility sacrifice, worth flagging loudly) or because it
        is a lower-preference codec at comparable bitrate (not a sacrifice at
        all). Reporting the second as the first would train users to distrust
        correct decisions.
        """
        if other.family not in self.profile.families:
            return (
                f"excluded by profile '{self.profile.name}' -- the destination "
                f"cannot decode {other.family}"
            )
        return (
            f"lower-preference codec; {self.stream.family} is preferred over "
            f"{other.family} at comparable bitrate, so the nominal "
            f"{(other.abr or 0) - (self.stream.abr or 0):+.0f}k does not "
            f"represent more quality"
        )

    def explain(self) -> str:
        """Human-readable justification, surfaced by the agent to the user.

        Steerability depends on the user being able to see *why* a stream was
        picked, especially when a higher-bitrate stream was passed over.
        """
        lines = [
            f"Selected {self.stream} -> .{self.stream.container}  ({self.stream.note})",
            f"Profile '{self.profile.name}': {self.profile.description}",
        ]
        better = [s for s in self.rejected if (s.abr or 0) > (self.stream.abr or 0)]
        for other in better:
            lines.append(f"Passed over {other}: {self.rejection_reason(other)}")
        lines.append(
            "Remuxed with `-c copy`; audio is bit-identical to the source stream."
        )
        return "\n".join(lines)


def parse_streams(formats: Iterable[dict]) -> list[AudioStream]:
    """Extract audio-only streams from a yt-dlp `-J` format list."""
    streams: list[AudioStream] = []
    for fmt in formats:
        acodec = fmt.get("acodec")
        if not acodec or acodec == "none":
            continue
        # Audio-only: yt-dlp reports vcodec "none" for these. Muxed streams
        # carry a re-encoded, lower-bitrate audio track and are never useful.
        if fmt.get("vcodec") not in (None, "none"):
            continue
        streams.append(
            AudioStream(
                itag=str(fmt.get("format_id", "?")),
                acodec=acodec,
                family=classify(acodec),
                abr=_bitrate(fmt),
                asr=fmt.get("asr"),
                ext=fmt.get("ext", ""),
                filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
                language=fmt.get("language"),
            )
        )
    return streams


def _bitrate(fmt: dict) -> float | None:
    """Best available bitrate estimate, in kbps.

    yt-dlp omits `abr` on some audio-only formats, so fall back to `tbr`
    (identical for audio-only streams) and finally to filesize over
    duration, which is accurate enough to rank by.
    """
    for key in ("abr", "tbr"):
        value = fmt.get(key)
        if value:
            return float(value)
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    duration = fmt.get("duration")
    if size and duration:
        return size * 8 / duration / 1000
    return None


def select(
    streams: Sequence[AudioStream],
    profile: Profile | str = DEFAULT_PROFILE,
    language: str | None = None,
) -> Selection:
    """Pick the best stream for `profile`, or raise `NoUsableStream`."""
    if isinstance(profile, str):
        try:
            profile = PROFILES[profile]
        except KeyError:
            raise NoUsableStream(
                f"Unknown profile {profile!r}. Known: {', '.join(PROFILES)}"
            ) from None

    candidates = list(streams)
    if language:
        localized = [s for s in candidates if s.language in (language, None)]
        if localized:
            candidates = localized

    eligible = [s for s in candidates if s.family in profile.families]
    if not eligible and not profile.strict:
        eligible = candidates
    if not eligible:
        offered = ", ".join(sorted({s.family for s in candidates})) or "none"
        raise NoUsableStream(
            f"Profile '{profile.name}' accepts {'/'.join(profile.families)} but "
            f"this video offers only: {offered}. Transcoding would stack a "
            f"second lossy generation, so it is not done automatically -- "
            f"choose another profile or pass --allow-transcode explicitly."
        )

    eligible.sort(key=profile.rank)
    return Selection(stream=eligible[0], profile=profile, considered=tuple(candidates))
