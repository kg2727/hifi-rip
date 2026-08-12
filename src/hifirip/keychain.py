"""Reading API keys from the macOS Keychain when the environment lacks them.

Exporting a key from a shell profile is more fragile than it looks. `.zshrc`
is sourced only for interactive shells, `.zprofile` only for login shells, and
tools invoked from an editor, a launch agent, or another program get neither.
That produces the worst kind of failure: the key is visibly present when the
user checks by hand and absent everywhere else, so the tool appears to be
ignoring credentials that are plainly configured.

Since setup already puts the keys in the Keychain, reading them from there
removes the shell entirely from the path. The environment still wins when set,
so CI and containers behave normally and a user can always override.

Two safety properties matter here:

*Values never leave this module except as return values.* Nothing is logged,
printed, or included in an error message.

*A lookup can never hang.* `security` may present a GUI authorisation dialog
if an item's ACL does not permit access, and a blocked prompt in a
non-interactive run would stall a rip indefinitely. Every call is bounded by a
short timeout and degrades to "not found".
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

#: Keychain service names follow from the documented setup commands, so an
#: item created by `docs/credentials.md` is found without further config.
SERVICE_FOR: dict[str, str] = {
    "ACOUSTID_KEY": "hifi-rip-acoustid",
    "DISCOGS_TOKEN": "hifi-rip-discogs",
    "SETLISTFM_KEY": "hifi-rip-setlistfm",
    "LASTFM_KEY": "hifi-rip-lastfm",
}

#: Bounded so an authorisation prompt cannot stall an unattended run.
LOOKUP_TIMEOUT = 3.0


def available() -> bool:
    return sys.platform == "darwin" and bool(shutil.which("security"))


def lookup(service: str, account: str | None = None) -> str | None:
    """Read one generic password, or None if it is absent or inaccessible.

    Deliberately silent about *why* it failed: a missing item, a denied ACL
    and an absent `security` binary all mean the same thing to the caller,
    and distinguishing them in a message risks echoing the item's contents.
    """
    if not available():
        return None

    command = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        command[2:2] = ["-a", account]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=LOOKUP_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


@dataclass(frozen=True)
class Resolved:
    """Where a credential came from. Never carries the value itself."""

    variable: str
    present: bool
    origin: str  # "environment" | "keychain" | "absent"


def resolve(
    variable: str, environ: dict[str, str], *, account: str | None = None
) -> tuple[str | None, Resolved]:
    """Find a credential, preferring the environment over the Keychain."""
    from_env = environ.get(variable)
    if from_env:
        return from_env, Resolved(variable, True, "environment")

    service = SERVICE_FOR.get(variable)
    if service:
        from_keychain = lookup(service, account)
        if from_keychain:
            return from_keychain, Resolved(variable, True, "keychain")

    return None, Resolved(variable, False, "absent")


def resolve_all(
    variables, environ: dict[str, str], *, account: str | None = None
) -> dict[str, Resolved]:
    """Origins for a set of variables, for reporting. Values are discarded."""
    return {
        variable: resolve(variable, environ, account=account)[1]
        for variable in variables
    }
