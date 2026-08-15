"""Tag and cover-art writing.

MP4 and Ogg need entirely different handling: MP4 uses atom keys with a
dedicated `covr` field, while Ogg Opus uses Vorbis comments and has no
artwork field at all, carrying cover art as a base64 FLAC picture block.
Both paths are exercised here, because a bug in either is invisible until
someone opens their library and finds blank tiles.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hifirip.tag import TagError, Tags, read_tags, safe_component, square_crop, write_tags

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg is required to synthesise fixtures",
)


def synth(path: Path, codec: str, bitrate: str = "128k") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", "-c:a", codec, "-b:a", bitrate,
         str(path)],
        check=True, capture_output=True, timeout=120,
    )
    return path


def synth_image(path: Path, width: int = 1280, height: int = 720) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc=size={width}x{height}:duration=1",
         "-frames:v", "1", str(path)],
        check=True, capture_output=True, timeout=120,
    )
    return path


@pytest.fixture
def tags() -> Tags:
    return Tags(
        title="A Song", artist="A Band", album="A Record",
        track=3, track_total=10, date="1999",
        source_url="https://example.invalid/watch?v=abc",
        provenance={"title": "musicbrainz", "artist": "discogs"},
    )


# --------------------------------------------------------------------------
# Sanitisation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Normal Title", "Normal Title"),
        ("With/Slash", "With-Slash"),
        ("Colons: And *Stars?", "Colons- And -Stars-"),
        ("  padded  ", "padded"),
        ("...", "Unknown"),
        ("", "Unknown"),
        ("multiple   spaces", "multiple spaces"),
    ],
)
def test_safe_component(raw, expected):
    assert safe_component(raw) == expected


def test_safe_component_never_returns_empty():
    assert safe_component('/\\:*?"<>|') != ""


# --------------------------------------------------------------------------
# Provenance travels with the file
# --------------------------------------------------------------------------

def test_comment_records_source_and_provenance(tags):
    comment = tags.comment()
    assert "example.invalid" in comment
    assert "title=musicbrainz" in comment
    assert "without re-encoding" in comment


def test_albumartist_falls_back_to_artist():
    assert Tags(title="T", artist="A").effective_albumartist == "A"


# --------------------------------------------------------------------------
# Round trips
# --------------------------------------------------------------------------

def test_m4a_round_trip(tmp_path, tags):
    path = synth(tmp_path / "a.m4a", "aac")
    write_tags(path, tags)
    read = read_tags(path)
    assert read["TITLE"] == ["A Song"]
    assert read["ARTIST"] == ["A Band"]
    assert read["ALBUM"] == ["A Record"]
    assert read["TRACKNUMBER"] == ["3"]
    assert read["DATE"] == ["1999"]


def test_opus_round_trip(tmp_path, tags):
    path = synth(tmp_path / "a.opus", "libopus", "160k")
    write_tags(path, tags)
    read = read_tags(path)
    assert read["TITLE"] == ["A Song"]
    assert read["ALBUMARTIST"] == ["A Band"]
    assert read["TRACKNUMBER"] == ["3"]
    assert read["TRACKTOTAL"] == ["10"]


def test_cover_art_embeds_in_mp4(tmp_path, tags):
    path = synth(tmp_path / "a.m4a", "aac")
    cover = synth_image(tmp_path / "c.jpg").read_bytes()
    write_tags(path, tags, cover)
    assert read_tags(path).get("COVER") == ["present"]


def test_cover_art_embeds_in_opus_as_a_picture_block(tmp_path, tags):
    """Ogg has no artwork field; art rides in METADATA_BLOCK_PICTURE."""
    path = synth(tmp_path / "a.opus", "libopus", "160k")
    cover = synth_image(tmp_path / "c.jpg").read_bytes()
    write_tags(path, tags, cover)
    read = read_tags(path)
    assert read.get("COVER") == ["present"]
    assert read["METADATA_BLOCK_PICTURE"]


def test_tagging_does_not_change_the_audio(tmp_path, tags):
    """Tags live in the container; the packets must be untouched."""
    from hifirip.download import packet_md5

    path = synth(tmp_path / "a.m4a", "aac")
    before = packet_md5(path)
    write_tags(path, tags, synth_image(tmp_path / "c.jpg").read_bytes())
    assert packet_md5(path) == before


def test_unsupported_container_is_refused(tmp_path, tags):
    path = tmp_path / "a.flac"
    path.write_bytes(b"not really a flac")
    with pytest.raises(TagError, match="no tag writer"):
        write_tags(path, tags)


def test_partial_tags_are_written_without_empty_fields(tmp_path):
    path = synth(tmp_path / "a.m4a", "aac")
    write_tags(path, Tags(title="Just A Title"))
    read = read_tags(path)
    assert read["TITLE"] == ["Just A Title"]
    assert "ALBUM" not in read


# --------------------------------------------------------------------------
# Artwork shaping
# --------------------------------------------------------------------------

def test_square_crop_produces_a_square(tmp_path):
    """YouTube thumbnails are 16:9; players lay art out in square tiles."""
    source = synth_image(tmp_path / "wide.jpg", 1280, 720)
    out = square_crop(source, tmp_path / "square.jpg", size=400)
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, timeout=60,
    )
    assert probed.stdout.strip().startswith("400,400")


def test_square_crop_rejects_a_non_image(tmp_path):
    junk = tmp_path / "junk.jpg"
    junk.write_text("not an image")
    with pytest.raises(TagError):
        square_crop(junk, tmp_path / "out.jpg")
