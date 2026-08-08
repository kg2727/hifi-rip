# Why there is no "AI upscaling" in hifi-rip

This question comes up constantly, so here is the reasoning in one place.

## The short version

Neural audio super-resolution **generates** high-frequency content. It does not
recover it. There is no information in a 20 kHz-limited Opus stream from which
the missing octave can be derived, so any model producing one is inventing
plausible detail — and plausible is not the same as correct.

For a music archive, that is precisely the wrong tradeoff. The point of an
archive is that the file matches the source. A file that sounds subjectively
brighter while measurably differing from the master is a worse archival copy,
even when listeners prefer it in a blind test.

## The state of the art

The field is real and moving quickly:

- **AudioSR** (2023) — diffusion-based, upsamples inputs anywhere in the
  2–16 kHz band to 48 kHz across speech, music, and effects.
- **FlashSR** (2025) — one-step diffusion distillation, dramatically faster.
- **UniverSR** (2025) — vocoder-free flow matching over complex spectral
  coefficients; state of the art across speech, music, and environmental audio.

The 2026 survey literature on bandwidth extension documents hallucination as a
core, unsolved failure mode of the generative approaches. Without sufficient
context these models guess, and they guess confidently.

## Why it's an especially poor fit here

Bandwidth extension earns its keep on genuinely bandwidth-starved audio — 8 kHz
telephony, degraded archival recordings, low-bitrate speech. That's not what
comes out of YouTube.

YouTube's Opus streams are already ~20 kHz-limited at a 48 kHz sample rate. That
is close to the limit of adult human hearing, and the content above it that a
model would synthesize is the content listeners are least able to evaluate. So
these models operate here at their weakest: minimal genuine benefit, maximum
unverifiability.

## If you want it anyway

It lives in a separate repository, deliberately. Keeping it out of the core
install means the heavy ML dependency stack never touches a normal install, and
the separation makes the distinction explicit: hifi-rip produces archival copies,
the enhancer produces derived listening copies.

```sh
hifi-rip rip <url> --enhance
```

The flag prints a loud warning, confirms you understand the output is generated
rather than recovered, and offers to install the companion package. Enhanced
output is always written to a **separate file**; the archival copy is never
overwritten or modified.
