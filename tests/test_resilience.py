"""Caching and circuit breaking.

Both exist for the same failure: a source that has stopped working costing
something on every item of a long run. During development a rate-limited
MusicBrainz spent its full timeout on every lookup, which across fifty
uploads is fifty timeouts for a service already known to be refusing us.
"""

from __future__ import annotations

import json
import time

import pytest

from hifirip.resilience import Breaker, DiskCache


@pytest.fixture
def cache(tmp_path) -> DiskCache:
    return DiskCache("test", root=tmp_path)


@pytest.fixture
def breaker(tmp_path) -> Breaker:
    return Breaker(name="test", threshold=3, cooldown=60.0, root=tmp_path)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def test_round_trip(cache):
    cache.put("k", {"a": 1})
    assert cache.get("k") == {"a": 1}


def test_missing_key_is_none(cache):
    assert cache.get("absent") is None


def test_expired_entries_are_ignored(tmp_path):
    cache = DiskCache("test", root=tmp_path, ttl=-1)
    cache.put("k", {"a": 1})
    assert cache.get("k") is None


def test_fetch_produces_once_then_serves_from_disk(cache):
    calls = []

    def produce():
        calls.append(1)
        return {"v": len(calls)}

    assert cache.fetch("k", produce) == {"v": 1}
    assert cache.fetch("k", produce) == {"v": 1}
    assert len(calls) == 1


def test_empty_results_are_not_cached(cache):
    """"No data" is usually a transient outage, not a fact about the query.

    Caching it for a month would silently disable a source that recovered
    minutes later.
    """
    calls = []

    def produce():
        calls.append(1)
        return {}

    cache.fetch("k", produce)
    cache.fetch("k", produce)
    assert len(calls) == 2


def test_corrupt_entry_degrades_to_a_miss(cache, tmp_path):
    cache.put("k", {"a": 1})
    path = next((tmp_path / "test").glob("*.json"))
    path.write_text("{ truncated")
    assert cache.get("k") is None


def test_unwritable_cache_does_not_raise(tmp_path):
    cache = DiskCache("test", root=tmp_path / "nope" / "\0bad")
    cache.put("k", {"a": 1})       # must not raise
    assert cache.get("k") is None


def test_namespaces_do_not_collide(tmp_path):
    a = DiskCache("a", root=tmp_path)
    b = DiskCache("b", root=tmp_path)
    a.put("k", {"from": "a"})
    b.put("k", {"from": "b"})
    assert a.get("k") == {"from": "a"}
    assert b.get("k") == {"from": "b"}


def test_writes_are_atomic(cache, tmp_path):
    """An interrupted write must not leave a permanently corrupt entry."""
    cache.put("k", {"a": 1})
    leftovers = list((tmp_path / "test").glob("*.tmp"))
    assert not leftovers


# --------------------------------------------------------------------------
# Breaker
# --------------------------------------------------------------------------

def test_starts_closed(breaker):
    assert breaker.allows()
    assert breaker.status() == "ok"


def test_failures_below_threshold_do_not_open(breaker):
    breaker.record_failure("timeout")
    breaker.record_failure("timeout")
    assert breaker.allows()
    assert "degraded" in breaker.status()


def test_threshold_opens_the_circuit(breaker):
    for _ in range(3):
        breaker.record_failure("timeout")
    assert not breaker.allows()
    assert "open" in breaker.status()


def test_success_resets_the_count(breaker):
    breaker.record_failure("x")
    breaker.record_failure("x")
    breaker.record_success()
    for _ in range(2):
        breaker.record_failure("x")
    assert breaker.allows()


def test_cooldown_lets_exactly_one_probe_through(tmp_path):
    """Without a probe, a recovered source stays marked down forever."""
    breaker = Breaker(name="t", threshold=1, cooldown=0.05, root=tmp_path)
    breaker.record_failure("down")
    assert not breaker.allows()
    time.sleep(0.06)
    assert breaker.allows()


def test_state_survives_a_restart(tmp_path):
    """A batch that crashes must not re-learn that a service is down."""
    first = Breaker(name="t", threshold=2, cooldown=60.0, root=tmp_path)
    first.record_failure("x")
    first.record_failure("x")
    assert not first.allows()

    second = Breaker(name="t", threshold=2, cooldown=60.0, root=tmp_path)
    assert not second.allows()


def test_reset_clears_state(breaker):
    for _ in range(3):
        breaker.record_failure("x")
    breaker.reset()
    assert breaker.allows()


def test_last_error_is_recorded_and_truncated(breaker, tmp_path):
    breaker.record_failure("e" * 500)
    state = json.loads((tmp_path / "test.json").read_text())
    assert state["last_error"].startswith("e")
    assert len(state["last_error"]) <= 200


def test_unreadable_state_is_treated_as_healthy(tmp_path):
    """A corrupt breaker file must not permanently disable a working source."""
    breaker = Breaker(name="t", threshold=2, cooldown=60.0, root=tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "t.json").write_text("{ not json")
    assert breaker.allows()
