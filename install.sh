#!/usr/bin/env bash
#
# Install hifi-rip and register it as an agent skill.
#
# Designed to drop into an existing setup as an add-on: it links the skill
# into whichever agent directories already exist and never replaces a file
# without being told to.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORCE="${FORCE:-0}"

say()  { printf '%s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
ok()   { printf '  ✓ %s\n' "$*"; }

# --- dependencies ---------------------------------------------------------

say "Checking dependencies..."

missing=()
for tool in yt-dlp ffmpeg ffprobe; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool $("$tool" --version 2>/dev/null | head -1 | cut -c1-40)"
    else
        warn "$tool not found"
        missing+=("$tool")
    fi
done

if [ ${#missing[@]} -gt 0 ]; then
    say ""
    say "Missing: ${missing[*]}"
    if command -v brew >/dev/null 2>&1; then
        say "Install with:  brew install ${missing[*]}"
    elif command -v apt-get >/dev/null 2>&1; then
        say "Install with:  sudo apt-get install ${missing[*]}"
    else
        say "Install these with your package manager, then re-run."
    fi
    exit 1
fi

# Python 3.11 is the floor: the config layer parses TOML with the standard
# library's tomllib rather than adding a dependency for it.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    warn "python3 is older than 3.11"
    say "  hifi-rip needs 3.11+. On macOS: brew install python@3.13"
    exit 1
fi
ok "python3 $(python3 --version 2>&1 | cut -d' ' -f2)"

# --- package --------------------------------------------------------------

say ""
say "Installing the hifi-rip package..."

if command -v pipx >/dev/null 2>&1; then
    pipx install --force "$REPO_DIR" >/dev/null
    ok "installed with pipx"
elif command -v uv >/dev/null 2>&1; then
    uv tool install --force "$REPO_DIR" >/dev/null
    ok "installed with uv"
else
    python3 -m pip install --user --upgrade "$REPO_DIR" >/dev/null
    ok "installed with pip --user"
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) warn "add \$HOME/.local/bin to your PATH to run hifi-rip" ;;
    esac
fi

# --- agent registration ---------------------------------------------------

link_skill() {
    local target_dir="$1" label="$2"
    [ -d "$(dirname "$target_dir")" ] || return 0

    mkdir -p "$target_dir"
    local target="$target_dir/SKILL.md"

    if [ -e "$target" ] && [ "$FORCE" != "1" ]; then
        if [ -L "$target" ] && [ "$(readlink "$target")" = "$REPO_DIR/SKILL.md" ]; then
            ok "$label already linked"
        else
            warn "$label: $target exists; leaving it alone (FORCE=1 to replace)"
        fi
        return 0
    fi

    ln -sfn "$REPO_DIR/SKILL.md" "$target"
    # References sit beside the skill so progressive disclosure resolves.
    [ -d "$REPO_DIR/references" ] && ln -sfn "$REPO_DIR/references" "$target_dir/references"
    ok "$label linked"
}

say ""
say "Registering the skill with any agents found..."

link_skill "$HOME/.claude/skills/hifi-rip" "Claude Code"
link_skill "$HOME/.config/claude/skills/hifi-rip" "Claude (XDG)"

for agents_home in "$HOME/.codex" "$HOME/.config/codex"; do
    if [ -d "$agents_home" ]; then
        target="$agents_home/AGENTS.md"
        if [ -e "$target" ] && [ "$FORCE" != "1" ]; then
            warn "Codex: $target exists; append the contents of AGENTS.md yourself"
        else
            ln -sfn "$REPO_DIR/AGENTS.md" "$target"
            ok "Codex linked"
        fi
    fi
done

# --- verify ---------------------------------------------------------------

say ""
if command -v hifi-rip >/dev/null 2>&1; then
    ok "hifi-rip is on your PATH"
    say ""
    say "Next:  hifi-rip doctor"
    say "       Reports what this machine can reach, including whether your"
    say "       credentials unlock 256k streams."
else
    warn "hifi-rip is installed but not on your PATH yet; open a new shell"
fi
