"""The skill manifests must not point at files that do not exist.

SKILL.md and AGENTS.md use progressive disclosure: they stay short and refer
an agent to deeper reference material as needed. A reference to a missing
file is invisible in normal use and only surfaces when an agent follows it
mid-task, at which point it silently loses the detail it went looking for.

install.sh also symlinks the references directory into each agent's skill
folder, so a missing directory produces a broken link on every install.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MANIFESTS = ("SKILL.md", "AGENTS.md")

#: Backtick-quoted paths to markdown inside the repo.
LINK = re.compile(r"`((?:references|docs)/[A-Za-z0-9_-]+\.md)`")


def referenced_paths() -> set[tuple[str, str]]:
    found = set()
    for manifest in MANIFESTS:
        text = (REPO / manifest).read_text()
        found |= {(manifest, path) for path in LINK.findall(text)}
    return found


def test_manifests_exist():
    for manifest in MANIFESTS:
        assert (REPO / manifest).is_file(), manifest


def test_manifests_actually_reference_something():
    """Guards the guard: a regex that stops matching would pass vacuously."""
    assert len(referenced_paths()) >= 3


@pytest.mark.parametrize(
    ("manifest", "path"), sorted(referenced_paths()), ids=lambda v: str(v)
)
def test_referenced_file_exists(manifest, path):
    assert (REPO / path).is_file(), f"{manifest} points at missing {path}"


def test_referenced_files_are_not_empty():
    for _, path in referenced_paths():
        assert len((REPO / path).read_text().strip()) > 200, path


def test_skill_frontmatter_has_name_and_description():
    """The Agent Skills spec keys a skill on these; without them a host has
    nothing to match a user's request against."""
    text = (REPO / "SKILL.md").read_text()
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    assert re.search(r"^name:\s*\S+", frontmatter, re.MULTILINE)
    assert re.search(r"^description:\s*\S+", frontmatter, re.MULTILINE)


def test_install_script_is_executable():
    assert (REPO / "install.sh").stat().st_mode & 0o111


def test_references_directory_exists_for_install_symlink():
    assert (REPO / "references").is_dir()
