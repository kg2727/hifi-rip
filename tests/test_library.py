"""Library placement and platform handoff.

The Music.app tests carry the only destructive decision in the project:
whether the staging copy may be deleted after an import. Deleting it when
Music is referencing the file in place leaves a broken library entry that
the user cannot easily undo, so the rule is that deletion requires positive
confirmation, and anything else keeps the file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hifirip.environment import Environment, Handoff, Host
from hifirip.library import (
    LibraryError,
    apple_music_import,
    hand_off,
    place,
    render_path,
    unique_path,
)
from hifirip.tag import Tags

ALBUM_TEMPLATE = "{albumartist}/{album}/{track:02d} - {title}"
SINGLE_TEMPLATE = "{albumartist}/{title}"

ALBUM_TRACK = Tags(title="Second Song", artist="A Band", album="A Record", track=2)
SINGLE = Tags(title="A Single", artist="A Band")


def env(handoff: Handoff, *, copies: bool | None = None) -> Environment:
    return Environment(
        host=Host.MACOS if handoff is Handoff.APPLE_MUSIC else Host.LINUX,
        profile="apple",
        handoff=handoff,
        library_root=Path("/tmp/library"),
        apple_music_copies_on_import=copies,
    )


# --------------------------------------------------------------------------
# Path rendering
# --------------------------------------------------------------------------

def test_album_track_uses_the_album_template(tmp_path):
    path = render_path(ALBUM_TEMPLATE, ALBUM_TRACK, "m4a", tmp_path,
                       single_template=SINGLE_TEMPLATE)
    assert path == tmp_path / "A Band" / "A Record" / "02 - Second Song.m4a"


def test_single_avoids_inventing_an_album_and_a_track_number(tmp_path):
    """The album template would produce 'Unknown Album/00 - A Single', naming
    a folder that does not exist and numbering a set of one."""
    path = render_path(ALBUM_TEMPLATE, SINGLE, "opus", tmp_path,
                       single_template=SINGLE_TEMPLATE)
    assert path == tmp_path / "A Band" / "A Single.opus"
    assert "Unknown Album" not in str(path)
    assert "00 - " not in str(path)


def test_track_without_album_still_counts_as_a_single(tmp_path):
    """Only when both are absent; a numbered track keeps album layout."""
    numbered = Tags(title="T", artist="A", track=3)
    path = render_path(ALBUM_TEMPLATE, numbered, "m4a", tmp_path,
                       single_template=SINGLE_TEMPLATE)
    assert "03 - T.m4a" in str(path)


def test_slash_in_a_field_does_not_create_a_directory(tmp_path):
    """An album called 'AC/DC Live' must not spawn a stray 'DC Live' folder."""
    tags = Tags(title="Song", artist="AC/DC", album="AC/DC Live", track=1)
    path = render_path(ALBUM_TEMPLATE, tags, "m4a", tmp_path,
                       single_template=SINGLE_TEMPLATE)
    assert path.relative_to(tmp_path).parts == (
        "AC-DC", "AC-DC Live", "01 - Song.m4a",
    )


def test_missing_fields_fall_back_rather_than_crashing(tmp_path):
    path = render_path(ALBUM_TEMPLATE, Tags(title="X", track=1), "m4a", tmp_path,
                       single_template=SINGLE_TEMPLATE)
    assert "Unknown Artist" in str(path)


def test_invalid_template_names_the_available_fields(tmp_path):
    with pytest.raises(LibraryError, match="albumartist"):
        render_path("{nonsense}/{title}", SINGLE, "m4a", tmp_path)


def test_overlong_component_is_truncated(tmp_path):
    tags = Tags(title="x" * 500, artist="A")
    path = render_path(SINGLE_TEMPLATE, tags, "m4a", tmp_path)
    assert all(len(part) <= 190 for part in path.parts)


# --------------------------------------------------------------------------
# Collisions
# --------------------------------------------------------------------------

def test_existing_file_is_never_overwritten(tmp_path):
    """Two uploads of the same track is exactly when a user wants both."""
    first = tmp_path / "Song.m4a"
    first.write_bytes(b"original")
    assert unique_path(first) == tmp_path / "Song (2).m4a"


def test_place_moves_without_clobbering(tmp_path):
    (tmp_path / "Song.m4a").write_bytes(b"existing")
    source = tmp_path / "staged.m4a"
    source.write_bytes(b"new")
    result = place(source, tmp_path / "Song.m4a")
    assert result.name == "Song (2).m4a"
    assert (tmp_path / "Song.m4a").read_bytes() == b"existing"
    assert not source.exists()


# --------------------------------------------------------------------------
# The destructive decision
# --------------------------------------------------------------------------

def test_staged_copy_is_kept_when_copy_preference_is_unknown(tmp_path, monkeypatch):
    """Unset is the common case on a fresh account, and must not mean delete."""
    imported = tmp_path / "track.m4a"
    imported.write_bytes(b"audio")
    monkeypatch.setattr("hifirip.library.apple_music_import", lambda p: "")

    result = hand_off(imported, env(Handoff.APPLE_MUSIC, copies=None))
    assert result.imported
    assert not result.staged_copy_removed
    assert imported.exists()
    assert "broken library entry" in result.report()


def test_staged_copy_is_kept_when_music_references_in_place(tmp_path, monkeypatch):
    imported = tmp_path / "track.m4a"
    imported.write_bytes(b"audio")
    monkeypatch.setattr("hifirip.library.apple_music_import", lambda p: "")

    result = hand_off(imported, env(Handoff.APPLE_MUSIC, copies=False))
    assert not result.staged_copy_removed
    assert imported.exists()


def test_staged_copy_is_removed_only_on_confirmed_copy(tmp_path, monkeypatch):
    imported = tmp_path / "track.m4a"
    imported.write_bytes(b"audio")
    monkeypatch.setattr("hifirip.library.apple_music_import", lambda p: "")

    result = hand_off(imported, env(Handoff.APPLE_MUSIC, copies=True))
    assert result.staged_copy_removed
    assert not imported.exists()


def test_dry_run_touches_nothing(tmp_path):
    target = tmp_path / "track.m4a"
    target.write_bytes(b"audio")
    result = hand_off(target, env(Handoff.APPLE_MUSIC, copies=True), dry_run=True)
    assert target.exists()
    assert not result.imported
    assert "Would import" in result.report()


# --------------------------------------------------------------------------
# AppleScript generation
# --------------------------------------------------------------------------

def test_import_script_references_the_file_and_reveals_it(tmp_path):
    script = apple_music_import(tmp_path / "Song.m4a", dry_run=True)
    assert "add POSIX file" in script
    assert "reveal newTrack" in script
    assert str(tmp_path / "Song.m4a") in script


def test_import_script_escapes_quotes_in_filenames(tmp_path):
    """A title containing a double quote would otherwise break the script."""
    script = apple_music_import(tmp_path / 'A "Quoted" Song.m4a', dry_run=True)
    assert '\\"Quoted\\"' in script


# --------------------------------------------------------------------------
# Other platforms
# --------------------------------------------------------------------------

def test_system_library_handoff_explains_indexing(tmp_path):
    target = tmp_path / "track.m4a"
    target.write_bytes(b"audio")
    result = hand_off(target, env(Handoff.SYSTEM_LIBRARY))
    assert "indexes automatically" in result.report()
    assert target.exists()


def test_filesystem_handoff_just_reports_the_path(tmp_path):
    target = tmp_path / "track.opus"
    target.write_bytes(b"audio")
    assert str(target) in hand_off(target, env(Handoff.FILESYSTEM)).report()
