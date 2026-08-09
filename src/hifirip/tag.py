"""Writing tags and cover art.

Tags are written after the remux rather than carried through it: `remux()`
strips source metadata with `-map_metadata -1`, so a rip never inherits
whatever the uploader happened to leave in the file. Everything here is
written deliberately.

Two container families need entirely different handling. MP4/M4A uses
atom keys and stores artwork in `covr`; Ogg Opus uses Vorbis comments and
has no artwork field at all, so cover art is carried as a base64-encoded
FLAC picture block in `METADATA_BLOCK_PICTURE` -- the de-facto convention
every player that displays Opus artwork expects.

Cover art is centre-cropped to a square before embedding. YouTube
thumbnails are 16:9, and music players lay art out in square tiles; an
un-cropped thumbnail is either letterboxed or stretched in every library
view the user will ever see it in.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from mutagen.flac import Picture
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

#: Filesystem-hostile characters, plus control codes. Replaced rather than
#: stripped so two different titles cannot collapse onto one filename.
ILLEGAL = str.maketrans({c: "-" for c in '/\\:*?"<>|\0'})

#: Most filesystems cap a component at 255 bytes; leave room for suffixes
#: like " (2).opus" that collision handling may append.
MAX_COMPONENT = 180


class TagError(RuntimeError):
    pass


@dataclass
class Tags:
    """The metadata written to a finished file."""

    title: str
    artist: str | None = None
    albumartist: str | None = None
    album: str | None = None
    track: int | None = None
    track_total: int | None = None
    date: str | None = None
    source_url: str | None = None
    #: Where each field came from, for the provenance line in `comment`.
    provenance: dict[str, str] = field(default_factory=dict)

    @property
    def effective_albumartist(self) -> str | None:
        return self.albumartist or self.artist

    def comment(self) -> str:
        """A short audit trail, embedded in the file itself.

        Months later, the file is the only artifact left: a user asking
        "where did this come from and how was it tagged" has nothing else to
        consult, so the answer travels with it.
        """
        parts = []
        if self.source_url:
            parts.append(f"Source: {self.source_url}")
        if self.provenance:
            resolved = ", ".join(f"{k}={v}" for k, v in sorted(self.provenance.items()))
            parts.append(f"Metadata: {resolved}")
        parts.append("Remuxed without re-encoding by hifi-rip.")
        return " | ".join(parts)


def safe_component(text: str) -> str:
    """Make one path component safe without letting it become empty."""
    cleaned = "".join(
        ch for ch in text.translate(ILLEGAL) if ch.isprintable()
    ).strip().strip(".")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_COMPONENT:
        cleaned = cleaned[:MAX_COMPONENT].rstrip()
    return cleaned or "Unknown"


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------

def square_crop(source: Path, destination: Path, size: int = 800) -> Path:
    """Centre-crop artwork to a square and normalise it to JPEG.

    Crops to the shorter edge rather than scaling, so nothing is distorted;
    the sides of a 16:9 thumbnail are lost, which is where YouTube puts
    letterboxing and channel furniture anyway.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TagError("ffmpeg not found on PATH; cannot prepare cover art")

    result = subprocess.run(
        [
            ffmpeg, "-v", "error", "-y", "-i", str(source),
            "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}",
            "-frames:v", "1", str(destination),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise TagError(f"could not crop cover art: {result.stderr.strip()}")
    return destination


def _flac_picture(data: bytes, mime: str = "image/jpeg") -> str:
    """Encode artwork the way Ogg containers expect it."""
    picture = Picture()
    picture.data = data
    picture.type = 3            # front cover
    picture.mime = mime
    picture.desc = "Cover"
    return base64.b64encode(picture.write()).decode("ascii")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _write_mp4(path: Path, tags: Tags, cover: bytes | None) -> None:
    audio = MP4(str(path))
    audio["\xa9nam"] = [tags.title]
    if tags.artist:
        audio["\xa9ART"] = [tags.artist]
    if tags.effective_albumartist:
        audio["aART"] = [tags.effective_albumartist]
    if tags.album:
        audio["\xa9alb"] = [tags.album]
    if tags.date:
        audio["\xa9day"] = [tags.date]
    if tags.track:
        audio["trkn"] = [(tags.track, tags.track_total or 0)]
    audio["\xa9cmt"] = [tags.comment()]
    if cover:
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def _write_ogg(path: Path, tags: Tags, cover: bytes | None, opus: bool) -> None:
    audio = OggOpus(str(path)) if opus else OggVorbis(str(path))
    audio["TITLE"] = tags.title
    if tags.artist:
        audio["ARTIST"] = tags.artist
    if tags.effective_albumartist:
        audio["ALBUMARTIST"] = tags.effective_albumartist
    if tags.album:
        audio["ALBUM"] = tags.album
    if tags.date:
        audio["DATE"] = tags.date
    if tags.track:
        audio["TRACKNUMBER"] = str(tags.track)
        if tags.track_total:
            audio["TRACKTOTAL"] = str(tags.track_total)
    audio["COMMENT"] = tags.comment()
    if cover:
        audio["METADATA_BLOCK_PICTURE"] = [_flac_picture(cover)]
    audio.save()


def write_tags(path: Path, tags: Tags, cover: bytes | None = None) -> None:
    """Write tags and optional artwork, dispatching on container."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".m4b"):
            _write_mp4(path, tags, cover)
        elif suffix == ".opus":
            _write_ogg(path, tags, cover, opus=True)
        elif suffix == ".ogg":
            _write_ogg(path, tags, cover, opus=False)
        else:
            raise TagError(f"no tag writer for {suffix or 'files without a suffix'}")
    except TagError:
        raise
    except Exception as exc:  # mutagen raises a wide variety
        raise TagError(f"could not tag {path.name}: {exc}") from exc


def read_tags(path: Path) -> dict[str, list[str]]:
    """Read back written tags, normalised for verification and tests."""
    suffix = path.suffix.lower()
    if suffix in (".m4a", ".mp4", ".m4b"):
        audio = MP4(str(path))
        mapping = {
            "\xa9nam": "TITLE", "\xa9ART": "ARTIST", "aART": "ALBUMARTIST",
            "\xa9alb": "ALBUM", "\xa9day": "DATE", "\xa9cmt": "COMMENT",
        }
        out: dict[str, list[str]] = {}
        for key, name in mapping.items():
            if key in audio:
                out[name] = [str(v) for v in audio[key]]
        if "trkn" in audio and audio["trkn"]:
            out["TRACKNUMBER"] = [str(audio["trkn"][0][0])]
        if "covr" in audio:
            out["COVER"] = ["present"]
        return out

    audio = OggOpus(str(path)) if suffix == ".opus" else OggVorbis(str(path))
    out = {key.upper(): list(value) for key, value in audio.items()}
    if "METADATA_BLOCK_PICTURE" in out:
        out["COVER"] = ["present"]
    return out


def fetch_thumbnail(url: str, destination: Path, *, timeout: int = 60) -> Path | None:
    """Download the video thumbnail with yt-dlp, returning None on failure.

    Artwork is a nice-to-have: a rip that succeeded should never be failed
    for want of a picture.
    """
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    stem = destination.with_suffix("")
    result = subprocess.run(
        [
            yt_dlp, "--skip-download", "--write-thumbnail",
            "--no-warnings", "--no-playlist",
            "-o", f"{stem}.%(ext)s", url,
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        return None
    for candidate in sorted(stem.parent.glob(f"{stem.name}.*")):
        if candidate.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            return candidate
    return None
