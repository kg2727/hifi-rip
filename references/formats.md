# Formats, itags, and device compatibility

Reference detail behind the rule in `SKILL.md`: never transcode.

## Why transcoding is never the answer

YouTube does not derive its audio streams from one another. It publishes
several **independent encodes of the same master** — Opus in WebM, AAC in
MP4. Picking an output format means picking the right *source stream* and
remuxing with `ffmpeg -c copy`.

- **Opus → AAC or MP3** stacks a second lossy generation on the first.
- **Anything → FLAC or ALAC** produces a ~5× larger file with provably
  identical information content.
- **Neural "upscaling"** generates high frequencies rather than recovering
  them. See `docs/no-upscaling.md`.

## itag reference

Annotation only. hifi-rip classifies on the codec and bitrate yt-dlp
actually reports, never on itag numbers, because YouTube adds, retires, and
silently re-encodes them.

| itag | codec | nominal | container | notes |
|---|---|---|---|---|
| 139 | AAC HE-AACv1 | ~48k | mp4 | Low-bandwidth fallback |
| 140 | AAC-LC | 128k | mp4 | The free-tier AAC baseline |
| 141 | AAC | 256k | mp4 | **Requires YouTube Music / Premium** |
| 171 | Vorbis | 128k | webm | Legacy |
| 172 | Vorbis | 256k | webm | Legacy |
| 249 | Opus | ~50k | webm | |
| 250 | Opus | ~70k | webm | |
| 251 | Opus | ~160k | webm | The free-tier Opus ceiling |
| 774 | Opus | ~256k | webm | **Requires YouTube Music / Premium** |

### Bitrate numbers are nominal

Opus is VBR: itag 251's advertised 160k is a *target*, not a floor, and a
well-encoded track routinely measures 125–140k. itag 140's "128k" regularly
reports as 130k. Never compare a measured stream against these figures and
warn on the difference — it would fire on nearly every rip.

## Two axes decide the stream

The **destination** pins the codec family. The **account tier** picks the
bitrate within it.

| destination | free | Premium / YT Music |
|---|---|---|
| Apple (Music.app, iOS, CarPlay) | AAC 128k → `.m4a` | AAC 256k → `.m4a` |
| Windows | AAC 128k → `.m4a` | AAC 256k → `.m4a` |
| Sonos | AAC 128k → `.m4a` | AAC 256k → `.m4a` |
| Archive / Plex / Android | Opus ~160k → `.opus` | Opus ~256k → `.opus` |

Premium raises bitrate **within** a family; it never changes the container.
Never "upgrade" an Apple rip to a higher-bitrate Opus stream — an unplayable
file is not a higher-quality file.

## Device compatibility

| target | Opus | AAC |
|---|---|---|
| Apple Music.app | ✗ | ✓ |
| iOS / CarPlay | ✗ (containers only, not Music) | ✓ |
| Sonos | ✗ | ✓ |
| Android | ✓ | ✓ |
| Plex / Navidrome (direct play) | ✓ | ✓ |
| Browsers | ✓ | ✓ |
| Windows 11 Media Player | ✓ | ✓ |
| Older Windows players, car head units | ✗ | ✓ |

## Codec choice at equal bitrate

The `archive` profile prefers Opus even when an AAC stream reports a
*slightly higher* bitrate, because Opus beats AAC at equal bitrate in blind
listening tests. A nominally larger AAC number is not more quality. This is a
codec-quality preference, not a compatibility sacrifice, and `explain()`
reports it as such — conflating the two trains users to distrust correct
decisions.

## Content gating

256k is gated on **content as well as account**. Official YouTube Music
tracks carry itags 141/774; ordinary uploads usually do not, even for an
entitled account. Across a 50-link sample of DJ mixes, live sets and
independent uploads, only 5 offered 256k.

So a Premium account finding no 256k stream is *usually normal*. Never
diagnose cookie failure from a missing stream — hifi-rip establishes
credential validity from yt-dlp's own diagnostics, independently of which
streams returned. Report what it determined.
