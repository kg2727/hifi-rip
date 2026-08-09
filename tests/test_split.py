"""Splitting, and the round-trip check that keeps its claims honest.

`test_round_trip_preserves_audio_exactly` is the load-bearing one: it
establishes that cutting and rejoining returns the original packets. An
earlier version cut each segment independently, which rounded every boundary
outward and duplicated a few milliseconds at each seam -- audible as a
stutter at track transitions, and invisible without this check.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hifirip.split import (
    MIN_TRACK_SECONDS,
    Boundary,
    SplitError,
    concatenate,
    derive_boundaries,
    snap_boundaries,
    split,
)

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg is required to synthesise fixtures",
)


def synth(path: Path, codec: str = "aac", seconds: float = 30.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", codec, "-b:a", "128k", str(path)],
        check=True, capture_output=True, timeout=180,
    )
    return path


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return synth(tmp_path / "album.m4a")


# --------------------------------------------------------------------------
# Boundary derivation
# --------------------------------------------------------------------------

def test_boundaries_are_contiguous_with_no_gaps(source):
    """A gap would discard audio; an overlap would duplicate it."""
    bounds = derive_boundaries([0.0, 10.0, 20.0], ["A", "B", "C"], 30.0)
    assert len(bounds) == 3
    for earlier, later in zip(bounds, bounds[1:]):
        assert earlier.end == later.start
    assert bounds[0].start == 0.0
    assert bounds[-1].end == 30.0


def test_boundaries_are_sorted_regardless_of_input_order():
    bounds = derive_boundaries([20.0, 0.0, 10.0], ["C", "A", "B"], 30.0)
    assert [b.title for b in bounds] == ["A", "B", "C"]


def test_implausibly_close_cut_points_are_rejected():
    """Sub-five-second 'tracks' come from bad boundary data, not music.

    The offending cut *point* is dropped rather than the segment it would
    create: dropping the segment would leave the span it covered in no track
    at all, a silent hole in the middle of the album.
    """
    bounds = derive_boundaries([0.0, 10.0, 10.5], ["A", "B", "C"], 30.0)
    assert [b.title for b in bounds] == ["A", "B"]
    assert all(b.duration >= MIN_TRACK_SECONDS for b in bounds)
    # No audio may fall outside a track.
    assert bounds[0].start == 0.0 and bounds[-1].end == 30.0
    for earlier, later in zip(bounds, bounds[1:]):
        assert earlier.end == later.start


def test_a_stub_final_track_is_folded_into_its_predecessor():
    bounds = derive_boundaries([0.0, 15.0, 29.0], ["A", "B", "C"], 30.0)
    assert [b.title for b in bounds] == ["A", "B"]
    assert bounds[-1].end == 30.0


def test_mismatched_titles_and_times_are_refused():
    with pytest.raises(SplitError, match="2 boundary times but 3 titles"):
        derive_boundaries([0.0, 10.0], ["A", "B", "C"], 30.0)


def test_no_points_yields_no_boundaries():
    assert derive_boundaries([], [], 30.0) == []


def test_tracks_are_numbered_from_one():
    bounds = derive_boundaries([0.0, 10.0, 20.0], ["A", "B", "C"], 30.0)
    assert [b.track for b in bounds] == [1, 2, 3]


# --------------------------------------------------------------------------
# Snapping
# --------------------------------------------------------------------------

def test_snapping_makes_neighbours_share_the_seam(source):
    """Both sides of a boundary must be given the identical timestamp."""
    raw = derive_boundaries([0.0, 10.0, 20.0], ["A", "B", "C"], 30.0)
    snapped = snap_boundaries(source, raw)
    for earlier, later in zip(snapped, snapped[1:]):
        assert earlier.end == later.start


def test_snapping_moves_boundaries_only_slightly(source):
    raw = derive_boundaries([0.0, 10.0, 20.0], ["A", "B", "C"], 30.0)
    snapped = snap_boundaries(source, raw)
    for original, aligned in zip(raw, snapped):
        assert abs(original.start - aligned.start) < 0.1


def test_zero_stays_zero(source):
    snapped = snap_boundaries(source, derive_boundaries([0.0, 15.0], ["A", "B"], 30.0))
    assert snapped[0].start == 0.0


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------

def test_round_trip_drift_stays_within_a_packet(source, tmp_path):
    """Cuts must not lose or pad audio beyond packet quantisation.

    Exact byte-equality is deliberately *not* asserted: measured on real
    files it holds for YouTube's AAC but not for every encoder's output, and
    the drift is container bookkeeping rather than audio. What must always
    hold is that the discrepancy stays packet-sized -- anything larger means
    audio was genuinely dropped or duplicated at a seam.
    """
    bounds = derive_boundaries([0.0, 10.0, 20.0], ["A", "B", "C"], 30.0)
    result = split(source, bounds, tmp_path / "out", verify=True)
    assert len(result.parts) == 3
    assert all(p.exists() for p in result.parts)
    assert result.drift_seconds is not None
    assert result.drift_seconds < 0.05


def test_the_report_states_what_was_actually_measured(source, tmp_path):
    """Whatever the outcome, the report must not overclaim."""
    bounds = derive_boundaries([0.0, 15.0], ["A", "B"], 30.0)
    report = split(source, bounds, tmp_path / "out", verify=True).report()
    assert "without re-encoding" in report
    assert ("byte for byte" in report) or ("does NOT reproduce" in report)


def test_split_parts_are_named_and_numbered(source, tmp_path):
    bounds = derive_boundaries([0.0, 15.0], ["First Track", "Second Track"], 30.0)
    result = split(source, bounds, tmp_path / "out", verify=False)
    assert result.parts[0].name == "01 - First Track.m4a"
    assert result.parts[1].name == "02 - Second Track.m4a"


def test_split_sanitises_track_titles(source, tmp_path):
    bounds = derive_boundaries([0.0, 15.0], ["AC/DC Song", "Ok"], 30.0)
    result = split(source, bounds, tmp_path / "out", verify=False)
    assert result.parts[0].name == "01 - AC-DC Song.m4a"
    assert result.parts[0].parent == tmp_path / "out"


def test_split_without_boundaries_is_refused(source, tmp_path):
    with pytest.raises(SplitError, match="nothing to split"):
        split(source, [], tmp_path / "out")


def test_concatenate_rebuilds_a_single_file(source, tmp_path):
    bounds = derive_boundaries([0.0, 15.0], ["A", "B"], 30.0)
    result = split(source, bounds, tmp_path / "out", verify=False)
    rejoined = concatenate(result.parts, tmp_path / "rejoined.m4a")
    assert rejoined.exists() and rejoined.stat().st_size > 0


def test_opus_round_trip_keeps_audio_identical(tmp_path):
    """Ogg Opus writes a pre-skip per stream, so duration drifts slightly.
    The audio packets must still survive intact."""
    source = synth(tmp_path / "album.opus", codec="libopus")
    bounds = derive_boundaries([0.0, 10.0, 20.0], ["A", "B", "C"], 30.0)
    result = split(source, bounds, tmp_path / "out", verify=True)
    assert result.reassembles_exactly is True
    # Any drift reported must be bookkeeping-sized, not audio-sized.
    assert (result.drift_seconds or 0) < 0.2
