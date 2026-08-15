"""Where a finished file goes, and how it reaches the user's player.

Two jobs: turn tags into a path under the library root, and hand the result
to whatever the platform actually uses to play music.

The Apple handoff carries the only genuinely destructive decision in the
project. Music.app has a "Copy files to Media folder when adding to library"
preference. When it is on, importing duplicates the file and our staging copy
is redundant. When it is off, Music references our file *in place*, and
deleting it leaves a broken entry in the user's library.

So the staging copy is removed only when that preference is confirmed on.
Apple's default is on, and the preference is frequently unset (never written
until the user changes it), which reads back as unknown -- and unknown is
treated as "keep the file". A stray duplicate is a minor annoyance; a library
full of dead entries is not something the user can easily undo.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .environment import Environment, Handoff
from .tag import Tags, safe_component


class LibraryError(RuntimeError):
    pass


def render_path(
    template: str,
    tags: Tags,
    container: str,
    root: Path,
    *,
    single_template: str | None = None,
) -> Path:
    """Turn a naming template into a real path under `root`.

    Each rendered component is sanitised separately, so a slash inside a
    field value becomes part of the name rather than a directory boundary --
    an album called "AC/DC Live" must not create a stray "DC Live" folder.

    Recordings with no album and no track number use `single_template`
    instead. The album template exists to keep a record's tracks together in
    order; applied to a standalone single it invents both facts, producing
    paths like "Unknown Album/00 - Title" where the folder names something
    that does not exist and the number orders a set of one.
    """
    if single_template and not tags.album and not tags.track:
        template = single_template

    values: dict[str, object] = {
        "title": tags.title or "Unknown Title",
        "artist": tags.artist or "Unknown Artist",
        "albumartist": tags.effective_albumartist or "Unknown Artist",
        "album": tags.album or "Unknown Album",
        "track": tags.track or 0,
        "date": tags.date or "",
    }
    # Sanitise field *values* before substitution, so that only the separators
    # written in the template itself divide directories. Formatting first and
    # splitting afterwards cannot tell the two apart: an artist called "AC/DC"
    # would silently become a "DC" folder nested inside an "AC" one.
    # `track` stays numeric, since the template formats it with :02d.
    safe_values = {
        key: safe_component(value) if isinstance(value, str) else value
        for key, value in values.items()
    }

    try:
        rendered = template.format(**safe_values)
    except (KeyError, IndexError, ValueError) as exc:
        raise LibraryError(
            f"naming template {template!r} is invalid: {exc}. Available fields: "
            f"{', '.join(sorted(values))}"
        ) from None

    parts = [safe_component(part) for part in rendered.split("/") if part.strip()]
    if not parts:
        raise LibraryError(f"naming template {template!r} rendered to an empty path")
    return root.joinpath(*parts).with_suffix(f".{container}")


def unique_path(path: Path) -> Path:
    """Avoid clobbering an existing file.

    Overwriting silently would be the wrong default: two different uploads of
    the same track are exactly the case where a user wants to compare rather
    than lose one.
    """
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise LibraryError(f"could not find a free filename near {path}")


def place(source: Path, destination: Path) -> Path:
    """Move a finished file into the library, without overwriting."""
    destination = unique_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return destination


# ---------------------------------------------------------------------------
# Handoff
# ---------------------------------------------------------------------------

APPLE_IMPORT_SCRIPT = """
tell application "Music"
    set newTrack to add POSIX file "{path}"
    reveal newTrack
    activate
end tell
"""


@dataclass
class HandoffResult:
    kind: Handoff
    path: Path
    imported: bool = False
    staged_copy_removed: bool = False
    message: str = ""

    def report(self) -> str:
        lines = [self.message or f"Left at {self.path}"]
        if self.imported and not self.staged_copy_removed:
            lines.append(
                "  Kept the file in place: Music.app was not confirmed to copy "
                "imports into its own media folder, so it may be referencing "
                "this file directly. Deleting it would leave a broken library "
                "entry."
            )
        return "\n".join(lines)


def apple_music_import(path: Path, *, dry_run: bool = False) -> str:
    """Add a file to Music.app and reveal it.

    Returns the AppleScript that was (or would be) run, so a caller can show
    the user exactly what is about to touch their library.
    """
    script = APPLE_IMPORT_SCRIPT.format(path=str(path).replace('"', '\\"'))
    if dry_run:
        return script
    if sys.platform != "darwin" or not shutil.which("osascript"):
        raise LibraryError("Music.app import requires macOS with osascript")

    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise LibraryError(
            f"Music.app refused the import: {result.stderr.strip()}"
        )
    return script


def hand_off(
    path: Path,
    environment: Environment,
    *,
    dry_run: bool = False,
) -> HandoffResult:
    """Deliver a finished file to the platform's music library."""
    if environment.handoff is Handoff.APPLE_MUSIC:
        if dry_run:
            return HandoffResult(
                kind=environment.handoff, path=path,
                message=f"Would import into Music.app:\n{apple_music_import(path, dry_run=True)}",
            )
        apple_music_import(path)
        copies = environment.apple_music_copies_on_import
        removed = False
        if copies is True:
            # Music has its own copy; ours is now a duplicate.
            path.unlink(missing_ok=True)
            removed = True
        return HandoffResult(
            kind=environment.handoff, path=path, imported=True,
            staged_copy_removed=removed,
            message=f"Imported into Music.app and revealed: {path.name}",
        )

    if environment.handoff is Handoff.SYSTEM_LIBRARY:
        return HandoffResult(
            kind=environment.handoff, path=path,
            message=(
                f"Placed in {path.parent}, which your system music player "
                f"indexes automatically."
            ),
        )

    return HandoffResult(
        kind=environment.handoff, path=path, message=f"Wrote {path}"
    )
