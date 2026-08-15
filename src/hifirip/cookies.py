"""Cookie source discovery, diagnosis, and remediation.

Credentials are what separate 128k from 256k on eligible tracks, so a
credential problem is a *quality* problem -- and an unusually quiet one,
since YouTube answers an unauthenticated request perfectly well, just with
worse streams. See `entitlement` for how that silent downgrade is caught.

This module exists because "cookie extraction failed" is never a useful
thing to tell someone. Every failure below has a different cause and a
different fix, and on macOS the most likely one by far is not a hifi-rip
problem at all: Safari keeps its cookies inside a TCC-protected container,
so reading them requires granting Full Disk Access to the terminal
application. Safari is also the default browser on every Mac. A tool that
reports `Operation not permitted` and stops has told the median Mac user
nothing they can act on.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Browsers yt-dlp can extract from, in the order we suggest trying them.
SUPPORTED_BROWSERS = (
    "chrome", "brave", "edge", "firefox", "safari", "chromium", "vivaldi", "opera",
)

#: Cookies YouTube uses to establish an authenticated session. A cookie file
#: without at least one of these will never unlock Premium streams.
SESSION_COOKIES = frozenset({
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO",
})

#: Warn this far ahead of expiry, so a rip started today does not silently
#: cross the boundary partway through a long queue.
EXPIRY_WARNING_DAYS = 3


class CookieProblem(Enum):
    OK = "ok"
    NOT_INSTALLED = "not-installed"
    NO_COOKIE_DATABASE = "no-cookie-database"
    PERMISSION_DENIED = "permission-denied"
    BROWSER_LOCKED = "browser-locked"
    REJECTED = "rejected"
    EXPIRED = "expired"
    NO_SESSION_COOKIES = "no-session-cookies"
    MALFORMED = "malformed"
    UNKNOWN = "unknown"


#: yt-dlp's wording for each distinct failure. Order matters: the permission
#: patterns must be tested before the generic "could not find" ones, since a
#: TCC denial can surface as either depending on the browser.
_PATTERNS: tuple[tuple[re.Pattern[str], CookieProblem], ...] = (
    (re.compile(r"operation not permitted|permission denied|errno 1\b", re.I),
     CookieProblem.PERMISSION_DENIED),
    (re.compile(r"database is locked|could not copy .* cookie", re.I),
     CookieProblem.BROWSER_LOCKED),
    (re.compile(r"could not find .* cookies database|no cookies found"
                r"|could not find .* profile", re.I),
     CookieProblem.NO_COOKIE_DATABASE),
    (re.compile(r"sign in to confirm|please sign in|cookies are no longer valid"
                r"|account cookies are invalid", re.I),
     CookieProblem.REJECTED),
    (re.compile(r"failed to load cookies|unable to load cookies|invalid netscape", re.I),
     CookieProblem.MALFORMED),
)


@dataclass(frozen=True)
class Diagnosis:
    problem: CookieProblem
    source: str                 # browser name, or a file path
    detail: str = ""            # verbatim tool output, when there was any

    @property
    def ok(self) -> bool:
        return self.problem is CookieProblem.OK

    @property
    def is_user_fixable(self) -> bool:
        """Whether a remediation exists that the user can actually perform."""
        return self.problem not in (CookieProblem.OK, CookieProblem.UNKNOWN)

    def remedy(self) -> str:
        return remedy(self.problem, self.source)

    def report(self) -> str:
        if self.ok:
            return f"Credentials from {self.source}: OK"
        lines = [f"Credentials from {self.source}: {self.problem.value}"]
        if self.detail:
            lines.append(f"  reported: {self.detail.strip()[:200]}")
        lines.append(remedy(self.problem, self.source))
        return "\n".join(lines)


def diagnose_stderr(stderr: str, source: str) -> Diagnosis:
    """Classify a cookie failure from yt-dlp's own output."""
    text = stderr or ""
    for pattern, problem in _PATTERNS:
        if pattern.search(text):
            return Diagnosis(problem, source, text)
    return Diagnosis(CookieProblem.OK, source)


def remedy(problem: CookieProblem, source: str) -> str:
    """Concrete, platform-specific instructions -- never a generic apology."""
    macos = sys.platform == "darwin"

    if problem is CookieProblem.PERMISSION_DENIED:
        if macos:
            return (
                "  macOS is blocking access to the browser's cookie store. This\n"
                "  is a system privacy control, not a browser or hifi-rip setting.\n"
                "  Fix: System Settings -> Privacy & Security -> Full Disk Access,\n"
                "  then enable the terminal application you run hifi-rip from\n"
                "  (Terminal, iTerm, VS Code, etc.) and restart it completely --\n"
                "  quit the app, not just the window.\n"
                "  Safari is affected most often because its cookies live in a\n"
                "  protected container, and it is the default browser on macOS.\n"
                "  If you would rather not grant Full Disk Access, export a\n"
                "  cookies.txt file instead and set `cookies_file` in your config;\n"
                "  see `hifi-rip doctor --help` for the format."
            )
        return (
            "  The cookie store could not be read. Close the browser fully and\n"
            "  retry; if it persists, check the file permissions on its profile\n"
            "  directory, or export a cookies.txt file and set `cookies_file`."
        )

    if problem is CookieProblem.NO_COOKIE_DATABASE:
        return (
            f"  No cookie database was found for {source}. Either it is not\n"
            f"  installed, or it has never been signed in to YouTube, or your\n"
            f"  cookies live in a non-default profile.\n"
            f"  Fix: sign in to YouTube in {source} once, or point hifi-rip at a\n"
            f"  browser you actually use -- `hifi-rip doctor` lists what it found."
        )

    if problem is CookieProblem.BROWSER_LOCKED:
        return (
            f"  {source} is holding its cookie database open. Chromium-based\n"
            f"  browsers lock it while running.\n"
            f"  Fix: quit {source} completely and retry."
        )

    if problem is CookieProblem.REJECTED:
        return (
            "  The cookies were read but YouTube rejected them, so the probe ran\n"
            "  anonymously and saw only free-tier streams. This caps quality at\n"
            "  Opus ~160k / AAC 128k despite a Premium account, and nothing else\n"
            "  would have told you.\n"
            "  Fix: open YouTube in that browser, confirm you are still signed\n"
            "  in, then retry. Signing out and back in forces fresh cookies."
        )

    if problem is CookieProblem.EXPIRED:
        return (
            "  The session cookies have expired or are about to.\n"
            "  Fix: sign in to YouTube again and re-export, or switch to\n"
            "  `cookies_from_browser` so they refresh automatically."
        )

    if problem is CookieProblem.NO_SESSION_COOKIES:
        return (
            "  The file parsed, but contains no YouTube session cookies, so it\n"
            "  cannot authenticate anything. This usually means it was exported\n"
            "  while signed out, or exported for a different site.\n"
            "  Fix: sign in to YouTube, then re-export with all cookies for the\n"
            "  youtube.com domain included."
        )

    if problem is CookieProblem.MALFORMED:
        return (
            "  The cookie file is not in Netscape format. Most browser extensions\n"
            "  offer this as 'Netscape' or 'cookies.txt'.\n"
            "  Fix: re-export in that format; the first line should read\n"
            "  '# Netscape HTTP Cookie File'."
        )

    return "  No remediation is known for this failure."


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _macos_paths() -> dict[str, Path]:
    home = Path.home()
    support = home / "Library" / "Application Support"
    return {
        "chrome": support / "Google" / "Chrome",
        "chromium": support / "Chromium",
        "brave": support / "BraveSoftware" / "Brave-Browser",
        "edge": support / "Microsoft Edge",
        "vivaldi": support / "Vivaldi",
        "opera": support / "com.operasoftware.Opera",
        "firefox": support / "Firefox" / "Profiles",
        "safari": home / "Library" / "Containers" / "com.apple.Safari"
                  / "Data" / "Library" / "Cookies",
    }


def _linux_paths() -> dict[str, Path]:
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return {
        "chrome": config / "google-chrome",
        "chromium": config / "chromium",
        "brave": config / "BraveSoftware" / "Brave-Browser",
        "edge": config / "microsoft-edge",
        "vivaldi": config / "vivaldi",
        "opera": config / "opera",
        "firefox": home / ".mozilla" / "firefox",
    }


def _windows_paths() -> dict[str, Path]:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return {
        "chrome": local / "Google" / "Chrome" / "User Data",
        "chromium": local / "Chromium" / "User Data",
        "brave": local / "BraveSoftware" / "Brave-Browser" / "User Data",
        "edge": local / "Microsoft" / "Edge" / "User Data",
        "vivaldi": local / "Vivaldi" / "User Data",
        "opera": roaming / "Opera Software" / "Opera Stable",
        "firefox": roaming / "Mozilla" / "Firefox" / "Profiles",
    }


def browser_paths() -> dict[str, Path]:
    if sys.platform == "darwin":
        return _macos_paths()
    if sys.platform.startswith("win"):
        return _windows_paths()
    return _linux_paths()


def find_cookie_store(browser: str, path: Path) -> Path | None:
    """Locate a browser's cookie file, if it has one.

    A present profile directory is not enough: an uninstalled Chrome
    routinely leaves its Application Support folder behind, which is what
    produced a confusing 'could not find cookies database' during
    development.
    """
    if not path.exists():
        return None
    if browser == "safari":
        candidate = path / "Cookies.binarycookies"
        return candidate if candidate.exists() else None
    if browser == "firefox":
        return next(iter(sorted(path.glob("*/cookies.sqlite"))), None)
    for pattern in ("**/Network/Cookies", "**/Cookies"):
        found = next(iter(sorted(path.glob(pattern))), None)
        if found and found.is_file():
            return found
    return None


def _is_readable(store: Path) -> bool:
    """Whether this process can actually open the cookie store.

    Existence is not access. On macOS, Safari's cookies sit inside a
    TCC-protected container: the file is plainly visible and stat-able, and
    opening it raises PermissionError unless the calling terminal has been
    granted Full Disk Access. Reporting such a store as usable would send
    the user straight into an opaque `Operation not permitted` -- the exact
    failure this module exists to pre-empt.
    """
    try:
        with store.open("rb") as handle:
            handle.read(1)
    except (PermissionError, OSError):
        return False
    return True


@dataclass(frozen=True)
class BrowserStatus:
    name: str
    path: Path
    installed: bool
    store: Path | None = None
    readable: bool = False

    @property
    def has_cookies(self) -> bool:
        return self.store is not None

    @property
    def usable(self) -> bool:
        return self.has_cookies and self.readable

    @property
    def blocked(self) -> bool:
        """Cookies are present but this process cannot read them."""
        return self.has_cookies and not self.readable

    def __str__(self) -> str:
        if self.usable:
            return f"  {self.name:10s} cookies found and readable"
        if self.blocked:
            return (f"  {self.name:10s} cookies found but NOT READABLE "
                    f"(blocked by system privacy settings)")
        if self.installed:
            return f"  {self.name:10s} profile present, no cookie store (never signed in?)"
        return f"  {self.name:10s} not installed"


def survey_browsers() -> list[BrowserStatus]:
    """What is actually present *and usable* on this machine."""
    statuses = []
    for name, path in browser_paths().items():
        store = find_cookie_store(name, path)
        statuses.append(BrowserStatus(
            name=name,
            path=path,
            installed=path.exists(),
            store=store,
            readable=_is_readable(store) if store else False,
        ))
    statuses.sort(key=lambda s: (not s.usable, not s.blocked, not s.installed, s.name))
    return statuses


def suggest_browser() -> str | None:
    """The best browser to actually configure -- never a blocked one."""
    for status in survey_browsers():
        if status.usable:
            return status.name
    return None


def blocked_browsers() -> list[BrowserStatus]:
    """Browsers whose cookies exist but cannot be read, with a real fix."""
    return [s for s in survey_browsers() if s.blocked]


# ---------------------------------------------------------------------------
# cookies.txt files -- the escape hatch when Full Disk Access is unavailable
# ---------------------------------------------------------------------------

def inspect_cookie_file(path: Path | str) -> Diagnosis:
    """Validate a Netscape cookie file before it is ever used.

    Checked up front rather than after a failed rip: an expired file behaves
    exactly like no file at all, quietly producing a free-tier download that
    looks completely successful.
    """
    path = Path(path).expanduser()
    source = str(path)

    if not path.is_file():
        return Diagnosis(CookieProblem.NO_COOKIE_DATABASE, source, "file not found")

    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return Diagnosis(CookieProblem.PERMISSION_DENIED, source, str(exc))

    names: set[str] = set()
    soonest: float | None = None
    parsed_any = False

    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        parsed_any = True
        domain, _, _, _, expires, name, _ = fields[:7]
        if "youtube.com" not in domain and "google.com" not in domain:
            continue
        names.add(name)
        try:
            expiry = float(expires)
        except ValueError:
            continue
        # 0 means a session cookie, which never expires on a clock.
        if expiry > 0 and (soonest is None or expiry < soonest):
            soonest = expiry

    if not parsed_any:
        return Diagnosis(CookieProblem.MALFORMED, source, "no tab-separated records")
    if not names & SESSION_COOKIES:
        return Diagnosis(
            CookieProblem.NO_SESSION_COOKIES, source,
            f"found {len(names)} cookies, none of them session cookies",
        )
    if soonest is not None:
        remaining = soonest - time.time()
        if remaining <= 0:
            return Diagnosis(
                CookieProblem.EXPIRED, source,
                f"earliest session cookie expired {abs(remaining) / 86400:.1f} days ago",
            )
        if remaining < EXPIRY_WARNING_DAYS * 86400:
            return Diagnosis(
                CookieProblem.EXPIRED, source,
                f"earliest session cookie expires in {remaining / 3600:.1f} hours",
            )

    return Diagnosis(CookieProblem.OK, source)
