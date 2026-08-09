# hifi-rip

Highest-quality audio extraction from YouTube — with the emphasis on *provably*
highest, not "we picked a big number."

Installable as an agent skill across Claude Code, Codex, and any other agentic
host, and usable as a plain CLI with no agent at all.

## The one rule: never transcode

YouTube does not derive its audio streams from one another. It publishes several
**independent** encodes of the same master — Opus in WebM, AAC in MP4. So
choosing an output format is a matter of picking the right *source stream* and
remuxing it with `ffmpeg -c copy`, never of converting one encode into another.

This has three consequences that most YouTube rippers get wrong:

- **Converting Opus → AAC (or → MP3) stacks a second lossy generation** on top of
  the first and buys nothing. hifi-rip refuses to do it unless you pass
  `--allow-transcode` explicitly.
- **Converting to FLAC or ALAC produces a file ~5× larger with provably identical
  information content.** A lossless container around lossy audio is not lossless
  audio.
- **There is no "upscaling" to real high-resolution audio.** Neural bandwidth
  extension models (AudioSR, FlashSR, UniverSR) *generate* plausible
  high-frequency content; they do not recover it. The output measurably differs
  from the master. See [`docs/no-upscaling.md`](docs/no-upscaling.md).

## What you actually get

The destination picks the codec family. The account tier picks the bitrate
within that family. Neither ever triggers a conversion.

| Destination | Free | YouTube Premium / Music |
|---|---|---|
| **Apple** (Music.app, iOS, CarPlay) | AAC 128k → `.m4a` | **AAC 256k** → `.m4a` |
| **Windows** (Media Player) | AAC 128k → `.m4a` | **AAC 256k** → `.m4a` |
| **Sonos** | AAC 128k → `.m4a` | **AAC 256k** → `.m4a` |
| **Archive / Plex / Android** | Opus ~160k → `.opus` | **Opus ~256k** → `.opus` |

Apple destinations are pinned to AAC because **Music.app cannot decode Opus** —
an unplayable file is not a higher-quality file. Premium raises the bitrate
*within* the family; it never flips your container out from under you.

### The silent-downgrade trap

Browser cookies expire constantly. When they do, an authenticated probe quietly
falls back to free-tier streams — and you keep ripping at 128k believing you're
getting 256k, with no error anywhere. hifi-rip detects this and says so loudly.

It also refuses to *guess*. A Premium account that finds no 256k stream has two
very different explanations: the content isn't eligible (only official YouTube
Music tracks carry 256k — completely normal), or your cookies died. hifi-rip
establishes credential validity from yt-dlp's own diagnostics, independently of
which streams came back, and tells you which one actually happened.

Run `hifi-rip doctor <known-music-url>` to verify credentials on their own.

### On bitrate numbers

Opus is VBR: itag 251's advertised "160k" is a target, not a floor, and a
well-encoded track routinely measures 125–140k. hifi-rip never compares your
stream against nominal figures, because that would fire a quality warning on
nearly every rip — and a warning that always fires is one you learn to ignore.

## Install

Requires `yt-dlp` and `ffmpeg` on PATH:

```sh
brew install yt-dlp ffmpeg        # macOS
```

Then:

```sh
pipx install hifi-rip
```

`yt-dlp` is invoked as a binary, not imported as a library — YouTube breaks
extractors roughly weekly, and `brew upgrade yt-dlp` should fix you immediately
without waiting on a release of this package.

## Use

```sh
hifi-rip rip <url>                    # download, tag, and file it
hifi-rip rip <url> --profile archive  # override the destination profile
hifi-rip rip <url> --dry-run          # show the destination without writing
hifi-rip rip <url> --no-handoff       # place the file, don't touch your library
hifi-rip explain <url>                # the resolved decision, without downloading
hifi-rip doctor <url>                 # verify credentials independently
hifi-rip matrix                       # print the tier x destination table
hifi-rip config --explain             # every setting and which layer set it
```

Uploads whose track boundaries are ranges rather than points — DJ sets, live
concerts, hours-long mixes — will not split without an explicit `--split` or
`--no-split`, and print an explanation of the tradeoff for that specific
upload first. Splitting itself is still being built; `--no-split` works today.

Every rip prints its full decision trace: which stream was chosen, which were
rejected, and why. "Highest quality available" is an unverifiable claim without
it.

## Legal

This is a wrapper around [yt-dlp](https://github.com/yt-dlp/yt-dlp). Respect
copyright and YouTube's Terms of Service. Use it for content you own, content
that is openly licensed, or where you otherwise have the right to a local copy.

## License

MIT
