---
name: hifi-rip
description: Extract the highest-quality available audio from YouTube and YouTube Music, choosing the correct source stream for the user's devices and account tier without ever transcoding. Use when the user wants to rip, download, archive, or save audio from a YouTube video, music video, playlist, or album upload, or asks about audio quality, bitrate, or format when pulling from YouTube.
license: MIT
---

# hifi-rip

Pull audio out of YouTube at the best quality the user's account and devices can
actually reach, and be able to prove that's what happened.

## The rule that governs everything

**Never transcode.** YouTube publishes several *independent* encodes of the same
master — Opus in WebM, AAC in MP4. Picking an output format means picking the
right source stream and remuxing with `ffmpeg -c copy`. It never means
converting one encode into another.

If a user asks for MP3, or for FLAC "to get better quality", explain the actual
tradeoff before doing it:

- **MP3/AAC from Opus** stacks a second lossy generation. Strictly worse.
- **FLAC/ALAC from any of these** is ~5× the size with identical information
  content. A lossless container around lossy audio is not lossless audio.
- **AI "upscaling"** (AudioSR, FlashSR, UniverSR) *generates* high frequencies
  rather than recovering them. The output provably differs from the master.

Do it if they still want it — `--allow-transcode` exists, and it's their call —
but say the tradeoff once, plainly, first.

## Workflow

1. **Check prerequisites.** `yt-dlp` and `ffmpeg` must be on PATH. If missing,
   offer `brew install yt-dlp ffmpeg` (macOS) or the platform equivalent.
2. **Probe before promising.** Run `hifi-rip explain <url>` to see the streams
   actually on offer and the resolved decision. Do not tell the user what
   quality they'll get before this returns.
3. **Report the decision, then act.** Show which stream was chosen and which
   were rejected. "Highest quality available" is unverifiable without it.
4. **Surface warnings loudly.** In particular, see credential expiry below.
5. **Hand off to the user's library** per their environment (Music.app on macOS,
   the indexed Music folder on Windows, plain files elsewhere).

## What gets chosen, and why

The destination pins the codec family. The account tier picks the bitrate inside
that family. These are different axes and both matter.

| Destination | Free | Premium / YT Music |
|---|---|---|
| Apple (Music.app, iOS, CarPlay) | AAC 128k → `.m4a` | AAC 256k → `.m4a` |
| Windows | AAC 128k → `.m4a` | AAC 256k → `.m4a` |
| Archive / Plex / Android | Opus ~160k → `.opus` | Opus ~256k → `.opus` |

Apple destinations are pinned to AAC because **Music.app cannot decode Opus**.
Never "upgrade" an Apple rip to a higher-bitrate Opus stream — an unplayable
file is not a higher-quality file.

## Two failure modes worth knowing by name

**Silent downgrade.** Browser cookies expire constantly. When they do, an
authenticated probe quietly falls back to free-tier streams and the user keeps
ripping at 128k believing they're getting 256k. Nothing else will tell them.
When `hifi-rip` reports rejected credentials, stop and say so before ripping.

**The 256k confounder.** A Premium account finding no 256k stream is *usually
normal* — 256k is gated on content as well as account, and only official
YouTube Music tracks carry it. Do not tell the user their cookies are broken
just because a music video topped out at 128k. `hifi-rip` distinguishes these
two cases from yt-dlp's own diagnostics; report what it says, don't infer.

**Bitrate numbers are VBR.** Opus itag 251's "160k" is a target, not a floor.
A track measuring 129k is fine. Never flag it as a problem.

## Steering

Configuration resolves through layers — defaults, detected environment, config
file, environment variables, explicit overrides — and the user's in-conversation
instruction is the top layer. When they say "actually keep these as Opus" or
"put them in ~/Archive instead", pass it through as an override for this run;
offer to persist it to `~/.config/hifi-rip/config.toml` only if they repeat it.

`hifi-rip config --explain` prints every resolved setting and which layer
produced it. Use it when a user is surprised by the tool's behaviour — the
answer is almost always that a layer they forgot about is winning.

## Metadata and album splitting

For album uploads and multi-track videos, evidence is gathered from many
independent sources (YouTube chapters, description timestamps, top comments,
MusicBrainz, Discogs, Last.fm, SponsorBlock, AcoustID fingerprints, silence
detection) and reconciled.

**Your role in this is judgement, not fetching.** The tool gathers evidence in
parallel and emits a structured conflict report; sources that agree are resolved
deterministically and never reach you. Adjudicate only what it escalates, and
prefer the reading supported by the most independent sources rather than the
most confident-sounding one. Full protocol in `references/consensus.md`.

Split points are cut on packet boundaries with `-c copy` — no re-encode, and no
silence inserted between tracks. Gapless playback also requires the user's
player to have crossfade disabled; see `references/gapless.md`.

## References

- `references/formats.md` — itag tables, codec families, device compatibility
- `references/consensus.md` — conflict report schema and adjudication protocol
- `references/gapless.md` — packet-boundary splitting and per-player setup
- `docs/no-upscaling.md` — why neural bandwidth extension is excluded
