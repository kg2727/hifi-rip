"""Caching and circuit breaking for external sources.

Two mechanics, both aimed at the same failure: a source that has stopped
working costing something on every single item of a long run.

**The breaker.** MusicBrainz rate-limited this project during development and
every subsequent lookup spent its full timeout before failing. Across fifty
uploads that is fifty timeouts for a service already known to be refusing us.
After a few consecutive failures a source is skipped for a cooldown, then
probed once to see if it has recovered. Being down should cost one timeout,
not one per item.

**The cache.** Re-running a rip, re-scoring a corpus, or retrying after a
crash must not re-fetch. Most repeat traffic from a tool like this is
avoidable, and avoidable traffic is exactly what gets a client blocked.

Both are deliberately process-external: state lives on disk, so a batch that
crashes and restarts does not re-learn that a service is down, and two tools
sharing a machine share the knowledge. Neither is a substitute for a central
broker when several machines share one address -- that genuinely needs a
server -- but on one host this is most of the benefit.

Failures here are never fatal. A cache that cannot be written and a breaker
that cannot be read both degrade to "no memory", which is exactly how the
system behaved before they existed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

CACHE_ROOT = Path(
    os.environ.get("HIFI_RIP_CACHE", Path.home() / ".cache" / "hifi-rip")
)

#: How long a source's answer stays good. Release metadata is effectively
#: static; a tracklist is edited for a while after an event and then settles.
DEFAULT_TTL = 30 * 24 * 3600

#: Consecutive failures before a source is considered down.
FAILURE_THRESHOLD = 3
#: How long to leave it alone before probing again.
COOLDOWN = 300.0


def _digest(*parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class DiskCache:
    """A small JSON cache, namespaced per source."""

    def __init__(self, namespace: str, *, root: Path | None = None,
                 ttl: float = DEFAULT_TTL) -> None:
        self.namespace = namespace
        self.root = (root or CACHE_ROOT) / namespace
        self.ttl = ttl

    def _path(self, key: str) -> Path:
        return self.root / f"{_digest(key)}.json"

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        try:
            if not path.is_file() or time.time() - path.stat().st_mtime > self.ttl:
                return None
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def put(self, key: str, value: Any) -> None:
        """Write atomically.

        A batch interrupted mid-write would otherwise leave a truncated file
        that parses as corrupt forever, turning a transient interruption into
        a permanent cache miss.
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=self.root, suffix=".tmp")
            with os.fdopen(handle, "w") as stream:
                json.dump(value, stream)
            os.replace(temporary, self._path(key))
        except (OSError, TypeError, ValueError):
            pass

    def fetch(self, key: str, producer: Callable[[], Any]) -> Any:
        """Return a cached value, or produce and store one.

        Empty results are deliberately *not* cached. "No data" is far more
        often a transient outage than a fact about the query, and caching it
        for a month would silently disable a source that recovered minutes
        later.
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        produced = producer()
        if produced:
            self.put(key, produced)
        return produced

    def clear(self) -> None:
        try:
            for path in self.root.glob("*.json"):
                path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

@dataclass
class BreakerState:
    failures: int = 0
    opened_at: float = 0.0
    last_error: str = ""

    @property
    def is_open(self) -> bool:
        return self.opened_at > 0.0


@dataclass
class Breaker:
    """Per-source failure tracking, persisted so restarts remember."""

    name: str
    threshold: int = FAILURE_THRESHOLD
    cooldown: float = COOLDOWN
    root: Path = field(default_factory=lambda: CACHE_ROOT / "breakers")
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def path(self) -> Path:
        return self.root / f"{self.name}.json"

    def _read(self) -> BreakerState:
        try:
            return BreakerState(**json.loads(self.path.read_text()))
        except (OSError, ValueError, TypeError):
            return BreakerState()

    def _write(self, state: BreakerState) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(asdict(state)))
        except (OSError, TypeError):
            pass

    def allows(self) -> bool:
        """Whether to attempt this source now.

        An open breaker lets exactly one request through after the cooldown --
        the probe that discovers recovery. Without it a source that came back
        would stay marked down until something reset it by hand.
        """
        with self._lock:
            state = self._read()
            if not state.is_open:
                return True
            if time.time() - state.opened_at >= self.cooldown:
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._read() != BreakerState():
                self._write(BreakerState())

    def record_failure(self, error: str = "") -> None:
        with self._lock:
            state = self._read()
            state.failures += 1
            state.last_error = str(error)[:200]
            if state.failures >= self.threshold and not state.is_open:
                state.opened_at = time.time()
            self._write(state)

    def status(self) -> str:
        state = self._read()
        if not state.is_open:
            return "ok" if not state.failures else f"degraded ({state.failures})"
        remaining = self.cooldown - (time.time() - state.opened_at)
        if remaining <= 0:
            return "open (probing on next use)"
        return f"open ({remaining:.0f}s remaining)"

    def reset(self) -> None:
        self._write(BreakerState())


_breakers: dict[str, Breaker] = {}
_registry_lock = threading.Lock()


def breaker_for(name: str) -> Breaker:
    with _registry_lock:
        if name not in _breakers:
            _breakers[name] = Breaker(name=name)
        return _breakers[name]


def health() -> dict[str, str]:
    """Current breaker status per known source, for `doctor`."""
    root = CACHE_ROOT / "breakers"
    names = set(_breakers)
    try:
        names |= {path.stem for path in root.glob("*.json")}
    except OSError:
        pass
    return {name: breaker_for(name).status() for name in sorted(names)}
