"""Remuxing, and the proof that it does not re-encode.

Fixtures are synthesised with ffmpeg rather than downloaded, so these run
offline and deterministically. The important test here is the negative one:
`test_reencoding_changes_the_packet_hash` establishes that the guard can
actually fail. Without it, the bit-identical check would be indistinguishable
from a function that returns True.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hifirip.download import (
    DownloadError,
    TranscodeDetected,
    packet_md5,
    probe_media,
    remux,
)

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe are required to synthesise fixtures",
)


def synth(path: Path, *, codec: str = "aac", bitrate: str = "128k",
          seconds: float = 3.0) -> Path:
    """Render a short tone in the requested codec."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:a", codec, "-b:a", bitrate, str(path),
        ],
        check=True, capture_output=True, timeout=120,
    )
    return path


@pytest.fixture
def aac_source(tmp_path: Path) -> Path:
    return synth(tmp_path / "source.m4a")


@pytest.fixture
def opus_source(tmp_path: Path) -> Path:
    return synth(tmp_path / "source.webm", codec="libopus", bitrate="160k")


# --------------------------------------------------------------------------
# probe_media
# --------------------------------------------------------------------------

def test_probe_reads_codec_and_rate(aac_source):
    media = probe_media(aac_source)
    assert media.codec == "aac"
    assert media.sample_rate
    assert media.duration == pytest.approx(3.0, abs=0.3)


def test_probe_derives_bitrate_when_ffprobe_omits_it(opus_source):
    """ffprobe reports no bit_rate for Opus in WebM; '?k' on our most-selected
    format would be a poor report."""
    media = probe_media(opus_source)
    assert media.codec == "opus"
    assert media.bitrate and media.bitrate > 0


def test_probe_raises_on_a_non_media_file(tmp_path):
    junk = tmp_path / "not-audio.m4a"
    junk.write_text("definitely not media")
    with pytest.raises(DownloadError):
        probe_media(junk)


# --------------------------------------------------------------------------
# The hash must be stable, and must be sensitive to re-encoding
# --------------------------------------------------------------------------

def test_packet_hash_is_stable(aac_source):
    assert packet_md5(aac_source) == packet_md5(aac_source)


def test_packet_hash_ignores_container_metadata(aac_source, tmp_path):
    """Tags must not perturb the hash, or every tagged rip would look altered."""
    tagged = tmp_path / "tagged.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(aac_source),
         "-c", "copy", "-metadata", "title=Something", str(tagged)],
        check=True, capture_output=True, timeout=120,
    )
    assert packet_md5(tagged) == packet_md5(aac_source)


def test_reencoding_changes_the_packet_hash(aac_source, tmp_path):
    """The guard must be capable of failing.

    Without this, a bit-identical check that always passed would be
    indistinguishable from a correct one.
    """
    reencoded = tmp_path / "reencoded.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(aac_source),
         "-c:a", "aac", "-b:a", "64k", str(reencoded)],
        check=True, capture_output=True, timeout=120,
    )
    assert probe_media(reencoded).codec == "aac"      # same codec...
    assert packet_md5(reencoded) != packet_md5(aac_source)   # ...different audio


# --------------------------------------------------------------------------
# remux
# --------------------------------------------------------------------------

def test_remux_preserves_audio_exactly(opus_source, tmp_path):
    result = remux(opus_source, tmp_path / "out.opus")
    assert result.verified_identical
    assert result.source.codec == result.output.codec == "opus"
    assert "bit-identical" in result.report()


def test_remux_across_containers_keeps_the_codec(aac_source, tmp_path):
    """mp4 -> m4a is the Apple path, and must remain a pure container change."""
    result = remux(aac_source, tmp_path / "out.m4a")
    assert result.output.codec == "aac"
    assert result.verified_identical


def test_remux_strips_source_metadata(aac_source, tmp_path):
    """A rip must not inherit whatever the uploader left in the file."""
    tagged = tmp_path / "tagged.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(aac_source), "-c", "copy",
         "-metadata", "title=Uploader Junk", "-metadata", "comment=spam",
         str(tagged)],
        check=True, capture_output=True, timeout=120,
    )
    out = remux(tagged, tmp_path / "clean.m4a").path
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags",
         "-of", "default=noprint_wrappers=1", str(out)],
        capture_output=True, text=True, timeout=60,
    )
    assert "Uploader Junk" not in probed.stdout
    assert "spam" not in probed.stdout


def test_incompatible_container_is_rejected_not_transcoded(opus_source, tmp_path):
    """Opus cannot live in an MP4 that Apple players will accept.

    ffmpeg either refuses outright or would need to re-encode; either way the
    correct outcome is a hard failure, never a silently converted file.
    """
    with pytest.raises(DownloadError):
        remux(opus_source, tmp_path / "impossible.m4a")


def test_verification_can_be_skipped_but_is_reported_as_such(aac_source, tmp_path):
    result = remux(aac_source, tmp_path / "out.m4a", verify=False)
    assert not result.verified_identical
    assert "NOT CHECKED" in result.report()
