"""Cookie discovery, diagnosis, and remediation.

The governing case: on macOS, Safari's cookie file is visible and stat-able
but unreadable without Full Disk Access. A survey that checks existence
alone reports it as usable and sends the user into an opaque `Operation not
permitted`. Safari is the default browser on every Mac, so that is the most
likely install failure this project will ever hit.
"""

from __future__ import annotations

import sys
import time

import pytest

from hifirip.cookies import (
    SESSION_COOKIES,
    BrowserStatus,
    CookieProblem,
    diagnose_stderr,
    find_cookie_store,
    inspect_cookie_file,
    remedy,
)

NETSCAPE_HEADER = "# Netscape HTTP Cookie File\n"


def cookie_line(name: str, expires: float, domain: str = ".youtube.com") -> str:
    return f"{domain}\tTRUE\t/\tTRUE\t{int(expires)}\t{name}\tvalue123\n"


# --------------------------------------------------------------------------
# Diagnosis from yt-dlp output
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("ERROR: [Errno 1] Operation not permitted: '/Users/x/Cookies.binarycookies'",
         CookieProblem.PERMISSION_DENIED),
        ("ERROR: could not find chrome cookies database in \"/Users/x\"",
         CookieProblem.NO_COOKIE_DATABASE),
        ("ERROR: could not copy chrome cookie database", CookieProblem.BROWSER_LOCKED),
        ("ERROR: [youtube] Sign in to confirm you're not a bot", CookieProblem.REJECTED),
        ("WARNING: The cookies are no longer valid", CookieProblem.REJECTED),
        ("ERROR: failed to load cookies", CookieProblem.MALFORMED),
    ],
)
def test_each_failure_is_diagnosed_distinctly(stderr, expected):
    assert diagnose_stderr(stderr, "safari").problem is expected


def test_permission_denial_is_not_confused_with_a_missing_database():
    """Both mention the cookie store; only one is fixable in System Settings."""
    denied = diagnose_stderr("Operation not permitted: could not find cookies", "safari")
    assert denied.problem is CookieProblem.PERMISSION_DENIED


def test_clean_output_diagnoses_as_ok():
    noise = "WARNING: [youtube] Some tv client https formats have been skipped"
    assert diagnose_stderr(noise, "chrome").ok


# --------------------------------------------------------------------------
# Remediation must be actionable
# --------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific guidance")
def test_permission_remedy_names_the_actual_macos_control():
    text = remedy(CookieProblem.PERMISSION_DENIED, "safari")
    assert "Full Disk Access" in text
    assert "Privacy & Security" in text
    # Restarting the app is required and routinely missed.
    assert "restart" in text.lower()
    # There must be a path for users who decline the permission.
    assert "cookies.txt" in text


def test_rejected_remedy_explains_the_quality_consequence():
    text = remedy(CookieProblem.REJECTED, "chrome")
    assert "free-tier" in text or "128k" in text
    assert "nothing else" in text


def test_locked_remedy_says_to_quit_the_browser():
    assert "quit" in remedy(CookieProblem.BROWSER_LOCKED, "chrome").lower()


def test_every_problem_has_a_remedy():
    for problem in CookieProblem:
        if problem is CookieProblem.OK:
            continue
        assert remedy(problem, "chrome").strip()


# --------------------------------------------------------------------------
# Existence is not access
# --------------------------------------------------------------------------

def test_unreadable_store_is_blocked_not_usable(tmp_path):
    store = tmp_path / "Cookies.binarycookies"
    store.write_bytes(b"binary")
    status = BrowserStatus("safari", tmp_path, installed=True, store=store,
                           readable=False)
    assert status.has_cookies
    assert status.blocked
    assert not status.usable
    assert "NOT READABLE" in str(status)


def test_readable_store_is_usable(tmp_path):
    store = tmp_path / "Cookies.binarycookies"
    store.write_bytes(b"binary")
    status = BrowserStatus("safari", tmp_path, installed=True, store=store,
                           readable=True)
    assert status.usable
    assert not status.blocked


def test_profile_without_a_store_is_neither(tmp_path):
    """An uninstalled Chrome leaves its profile directory behind."""
    status = BrowserStatus("chrome", tmp_path, installed=True)
    assert not status.has_cookies
    assert not status.blocked
    assert "never signed in" in str(status)


def test_find_cookie_store_locates_chromium_layout(tmp_path):
    store = tmp_path / "Default" / "Network" / "Cookies"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"x")
    assert find_cookie_store("chrome", tmp_path) == store


def test_find_cookie_store_returns_none_for_empty_profile(tmp_path):
    assert find_cookie_store("chrome", tmp_path) is None


def test_find_cookie_store_locates_firefox_profile(tmp_path):
    store = tmp_path / "abc.default" / "cookies.sqlite"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"x")
    assert find_cookie_store("firefox", tmp_path) == store


# --------------------------------------------------------------------------
# cookies.txt validation, checked before use rather than after a failed rip
# --------------------------------------------------------------------------

def test_missing_file_is_reported_as_such(tmp_path):
    result = inspect_cookie_file(tmp_path / "nope.txt")
    assert result.problem is CookieProblem.NO_COOKIE_DATABASE


def test_non_netscape_file_is_malformed(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text("this is not a cookie file at all\n")
    assert inspect_cookie_file(path).problem is CookieProblem.MALFORMED


def test_file_without_session_cookies_cannot_authenticate(tmp_path):
    path = tmp_path / "cookies.txt"
    future = time.time() + 86400 * 30
    path.write_text(NETSCAPE_HEADER + cookie_line("VISITOR_INFO1_LIVE", future))
    result = inspect_cookie_file(path)
    assert result.problem is CookieProblem.NO_SESSION_COOKIES
    assert "re-export" in result.remedy()


def test_expired_session_cookies_are_caught_before_use(tmp_path):
    """An expired file behaves exactly like no file: a successful-looking
    free-tier download. It has to be caught up front."""
    path = tmp_path / "cookies.txt"
    past = time.time() - 86400
    path.write_text(NETSCAPE_HEADER + cookie_line("SID", past))
    result = inspect_cookie_file(path)
    assert result.problem is CookieProblem.EXPIRED
    assert "days ago" in result.detail


def test_imminent_expiry_is_warned_about(tmp_path):
    path = tmp_path / "cookies.txt"
    soon = time.time() + 3600
    path.write_text(NETSCAPE_HEADER + cookie_line("SID", soon))
    result = inspect_cookie_file(path)
    assert result.problem is CookieProblem.EXPIRED
    assert "hours" in result.detail


def test_valid_file_passes(tmp_path):
    path = tmp_path / "cookies.txt"
    future = time.time() + 86400 * 30
    path.write_text(
        NETSCAPE_HEADER
        + cookie_line("SID", future)
        + cookie_line("HSID", future)
        + cookie_line("VISITOR_INFO1_LIVE", future)
    )
    assert inspect_cookie_file(path).ok


def test_session_cookies_without_expiry_are_not_treated_as_expired(tmp_path):
    """Expiry 0 means a browser-session cookie, not one that expired in 1970."""
    path = tmp_path / "cookies.txt"
    path.write_text(NETSCAPE_HEADER + cookie_line("SID", 0))
    assert inspect_cookie_file(path).ok


def test_non_youtube_cookies_are_ignored(tmp_path):
    path = tmp_path / "cookies.txt"
    future = time.time() + 86400 * 30
    path.write_text(
        NETSCAPE_HEADER
        + cookie_line("SID", time.time() - 999, domain=".example.com")
        + cookie_line("SID", future)
    )
    assert inspect_cookie_file(path).ok


def test_session_cookie_names_cover_the_modern_secure_variants():
    assert "__Secure-1PSID" in SESSION_COOKIES
    assert "SAPISID" in SESSION_COOKIES
