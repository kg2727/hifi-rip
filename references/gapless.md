# Gapless splitting

How tracks are cut, what survives the round trip, and what the user must do
in their player.

## What the splitter does

Every cut is `ffmpeg -c copy`. The audio in each track is the same compressed
data the source held; nothing is re-encoded and no silence is inserted. The
usual reason split albums gain gaps is that a tool re-encoded each piece and
every encode contributed its own priming silence.

## Boundaries are snapped to packet edges

Audio packets carry roughly 20ms (Opus) or 23ms (AAC at 44.1kHz), and a copy
cannot subdivide one. hifi-rip snaps each requested boundary onto a real
packet timestamp first, then uses that snapped time as **both** the end of one
track and the start of the next, so the two sides cannot disagree.

This matters. Cutting each segment independently makes ffmpeg round every
boundary outward on its own, so neighbours disagree about the seam and a few
milliseconds are duplicated at each cut — audible as a stutter at exactly the
track transitions gapless playback exists to protect.

Measured on real files:

| | cut independently | snapped |
|---|---|---|
| AAC / m4a | 19ms drift | **byte-exact, 0ms** |
| Opus / ogg | 46ms drift | audio byte-exact, ~13ms container drift |
| Matroska / mka | — | **3007ms — unusable** |

## Audio integrity vs container bookkeeping

These are different questions and hifi-rip reports them separately.

Ogg Opus writes a fresh **pre-skip header** into every stream it creates, so
three tracks carry three of them. The audio packets still rejoin byte for
byte; only the container's duration accounting moves, by around 13ms across a
handful of cuts. That is bookkeeping, not lost sound.

Round-trip exactness is **file-dependent** — it holds for YouTube's AAC but
not for every encoder's output — so `verify_concatenation` measures each file
rather than assuming, and reports what it found.

**Never use Matroska for split output.**

## Continuous mixes have no correct cut point

Across an 8-to-32 bar crossfade both tracks are genuinely playing. Any
boundary is a choice about which track keeps the transition, not an
approximation of a true one. Each split file will start and end with its
neighbour audible — that is not an error, it is what the audio contains.

This is why hifi-rip asks before splitting a mix rather than deciding, and
why keeping one file with embedded chapter markers is often the better
answer: skippable in any modern player, preserves the transitions the artist
built, and cannot cut in the wrong place.

## The other half: the player

Gapless playback needs the *player* configured too. Splitting perfectly and
then playing through a crossfade setting reintroduces exactly the gap the
splitter avoided.

- **Apple Music.app** — disable *Crossfade Songs* in Settings → Playback.
- **iOS Music** — Settings → Music → disable *Crossfade*.
- **Plex** — disable crossfade/gapless-transition in the player client, not
  the server.
- **Navidrome / Subsonic clients** — varies by client; look for "crossfade"
  or "fade between tracks".
- **VLC** — Preferences → Audio → set crossfade duration to 0.
- **foobar2000 / mpv** — gapless by default; no action needed.

Note that Apple's crossfade applies between *any* two tracks, so it will
affect an album ripped perfectly just as much as one ripped badly.

## Guard rails

- Cut points closer than 5 seconds are rejected as bad boundary data. The
  offending **cut point** is dropped, not the segment — dropping a segment
  would leave the span it covered in no track at all, a silent hole.
- A stub final track is folded back into its predecessor.
- Track one always starts at 0; a gap at the very beginning is lead-in, not a
  boundary.
