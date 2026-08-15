"""Configuration layering -- the steering mechanism.

The precedence order is the contract that lets two users share one skill and
get different behaviour, so it is pinned here explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hifirip.config import Config, load
from hifirip.environment import Environment, Handoff, Host

APPLE_ENV = Environment(
    host=Host.MACOS,
    profile="apple",
    handoff=Handoff.APPLE_MUSIC,
    library_root=Path("/Users/test/Music/hifi-rip"),
)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'profile = "archive"\n'
        'library_root = "/from/file"\n'
        'consensus_threshold = 3\n'
    )
    return path


def test_defaults_apply_when_nothing_else_does(tmp_path):
    config = load(environment=APPLE_ENV, path=tmp_path / "absent.toml", environ={})
    assert config.allow_transcode is False
    assert config.source_of("allow_transcode") == "default"


def test_environment_beats_defaults(tmp_path):
    config = load(environment=APPLE_ENV, path=tmp_path / "absent.toml", environ={})
    assert config.profile == "apple"
    assert config.source_of("profile") == "environment"
    assert config.handoff == "apple-music"


def test_file_beats_environment(config_file):
    config = load(environment=APPLE_ENV, path=config_file, environ={})
    assert config.profile == "archive"
    assert config.source_of("profile") == "file"
    assert config.library_root == Path("/from/file")


def test_env_vars_beat_file(config_file):
    config = load(
        environment=APPLE_ENV,
        path=config_file,
        environ={"HIFI_RIP_PROFILE": "sonos"},
    )
    assert config.profile == "sonos"
    assert config.source_of("profile") == "env"
    # Untouched keys keep the file's values.
    assert config.consensus_threshold == 3
    assert config.source_of("consensus_threshold") == "file"


def test_overrides_beat_everything(config_file):
    """A user's in-conversation instruction is the top layer."""
    config = load(
        environment=APPLE_ENV,
        path=config_file,
        environ={"HIFI_RIP_PROFILE": "sonos"},
        overrides={"profile": "opus-native"},
    )
    assert config.profile == "opus-native"
    assert config.source_of("profile") == "override"


def test_none_overrides_do_not_clobber(config_file):
    """Unset CLI flags arrive as None and must not erase lower layers."""
    config = load(
        environment=APPLE_ENV,
        path=config_file,
        environ={},
        overrides={"profile": None, "cookies_from_browser": "chrome"},
    )
    assert config.profile == "archive"
    assert config.source_of("profile") == "file"
    assert config.cookies_from_browser == "chrome"


def test_unknown_file_keys_are_ignored_not_fatal(tmp_path):
    """A config written for a newer version must not break an older install."""
    path = tmp_path / "config.toml"
    path.write_text('profile = "archive"\nfuture_feature = "whatever"\n')
    config = load(environment=APPLE_ENV, path=path, environ={})
    assert config.profile == "archive"
    assert not hasattr(config, "future_feature")


def test_env_var_scalar_parsing(tmp_path):
    config = load(
        environment=APPLE_ENV,
        path=tmp_path / "absent.toml",
        environ={
            "HIFI_RIP_ALLOW_TRANSCODE": "true",
            "HIFI_RIP_CONSENSUS_THRESHOLD": "4",
            "HIFI_RIP_SOURCES": "musicbrainz, discogs",
            "HIFI_RIP_UNRELATED": "ignored",
        },
    )
    assert config.allow_transcode is True
    assert config.consensus_threshold == 4
    assert config.sources == ("musicbrainz", "discogs")


def test_transcoding_is_never_enabled_implicitly(tmp_path):
    """It must take a deliberate human action, at any layer."""
    config = load(environment=APPLE_ENV, path=tmp_path / "absent.toml", environ={})
    assert config.allow_transcode is False


def test_describe_reports_every_field_with_provenance(config_file):
    text = load(environment=APPLE_ENV, path=config_file, environ={}).describe()
    assert "profile" in text and "<- file" in text
    assert "provenance" not in text
