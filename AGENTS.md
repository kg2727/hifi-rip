# hifi-rip — agent instructions

This file exists so hosts that read `AGENTS.md` (Codex CLI and others) get the
same behaviour as hosts that read `SKILL.md`. **`SKILL.md` is the canonical
document** — read it in full before acting. What follows is the short form.

## Install as an add-on

This is designed to drop into an existing agent setup without replacing
anything. Nothing here assumes a particular host, model, or tool namespace.

```sh
git clone https://github.com/<handle>/hifi-rip && cd hifi-rip && ./install.sh
```

`install.sh` checks for `yt-dlp`/`ffmpeg`, installs the Python package, and
links `SKILL.md` into whichever agent directories it finds
(`~/.claude/skills/`, `~/.codex/`, and so on). It never overwrites an existing
file without asking.

## Contract

You drive one command-line tool. You do not fetch, parse, or download anything
yourself.

```sh
hifi-rip explain <url>     # resolved decision + streams on offer. Run first.
hifi-rip rip <url>         # do it
hifi-rip doctor <url>      # verify credentials independently of content
hifi-rip config --explain  # every setting and which layer set it
```

## Non-negotiables

1. **Never transcode.** Opus→AAC, →MP3, →FLAC all make things worse or bigger
   for nothing. If the user insists, state the tradeoff once, then use
   `--allow-transcode`.
2. **Never "upgrade" an Apple destination to Opus.** Music.app cannot decode it.
   An unplayable file is not a higher-quality file.
3. **Probe before promising.** Don't state what quality the user will get until
   `hifi-rip explain` has returned.
4. **Report rejected credentials before ripping.** Expired cookies silently cap
   quality at 128k with no error anywhere.
5. **Don't diagnose from absence.** A Premium account with no 256k stream is
   usually just ineligible content. Report what the tool determined; never
   infer cookie failure from a missing stream.

## Your job in the metadata pipeline

Judgement, not fetching. The tool gathers evidence from all sources in parallel
and resolves agreement deterministically. It escalates only genuine conflicts,
as a structured report. Adjudicate those, preferring the reading with the most
*independent* support over the most confidently-worded one.

See `references/consensus.md` for the report schema.

## Steering

The user's in-conversation instruction is the highest-precedence config layer.
Pass it through as a per-run override. Offer to persist to
`~/.config/hifi-rip/config.toml` only if they ask twice.
