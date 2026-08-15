# Consensus and the adjudication protocol

How hifi-rip decides what a recording is, and what it asks you to settle.

## Your role

**Judgement, not fetching.** The tool queries every competent source in
parallel, resolves agreement arithmetically, and escalates only genuine
disagreement. You never call an API; you rule on conflicts.

The brief you receive contains **only unresolved fields**. Fields the
arithmetic settled are omitted deliberately — do not revisit them.

## The three rules

**1. Authority is not a licence.** A value backed by exactly one source is a
*lead*, not a conclusion, however reliable that source is. 1001Tracklists is
weighted 0.90 — joint-highest among tracklist sources — and still cannot
settle a field alone, because it is community-submitted and a single
unreviewed entry should not decide what lands in someone's library.

**2. Agreement is settled without a model.** Sources that agree after
normalisation never reach you. Escalation costs latency and money.

**3. Independence is counted, not assumed.** Last.fm and AcoustID both
resolve against MusicBrainz, so agreeing with it is not corroboration. They
are declared as derived and discounted.

## Source weights

| weight | source | key | applies to |
|---|---|---|---|
| 1.00 | musicbrainz | — | recordings, albums |
| 0.95 | discogs | `DISCOGS_TOKEN` | recordings, albums |
| 0.90 | setlistfm | `SETLISTFM_KEY` | live performances |
| 0.90 | 1001tracklists | — | continuous mixes |
| 0.85 | acoustid | `ACOUSTID_KEY` | all music classes |
| 0.80 | yt_chapters | — | multitrack |
| 0.70 | sponsorblock | — | all |
| 0.60 | yt_description | — | all |
| 0.55 | lastfm | `LASTFM_KEY` | recordings |
| 0.50 | silence | — | discrete albums only |
| 0.40 | yt_comments | — | multitrack |

Weight reflects each source's **track record**, not its confidence about a
particular value.

Routing matters as much as weighting. Release durations are exact for a
discrete album and meaningless for a DJ set, where tracks are mixed in late,
cut out early or looped. Setlist.fm constrains order, never timing. Silence
detection finds nothing in a beatmatched mix. Consulting a source outside its
competence spends a weighted vote on confident noise.

## Statuses

| status | meaning | action |
|---|---|---|
| `agreed` | 2+ independent sources, clear majority | settled |
| `single-source` | one independent source | **needs you** |
| `conflicted` | rivals split the weight | **needs you** |
| `absent` | nobody offered a value | leave unset |

## How to adjudicate

- Prefer the reading with the most **independent** support over the most
  confidently-worded one.
- If the evidence does not settle a field, **say so and leave it unset**. An
  empty field is honest; a wrong one propagates into the user's library under
  a name that is not theirs.
- A field you decline stays unset. It does not fall back to the leading
  candidate — the leading candidate was what we did not trust.

## Normalisation

Comparison happens on a normalised form so trivial differences count as
agreement; the original spelling always survives into the output. Removed for
comparison: case, accents, apostrophes, punctuation, `feat.`/`ft.` variants,
and bracketed upload furniture such as "(Official Video)" or "(4K Remaster)".

**Deliberately preserved**: `(Live)`, `(Acoustic)`, `(Extended Mix)`,
`(Radio Edit)`, remix credits. Those identify a genuinely different
recording, and stripping them would merge two distinct things.

## Tracklist consensus

Harder than field consensus in two ways: values are continuous, so agreement
is proximity rather than equality; and entries must be aligned before they
can be compared, since one source may list an intro the others omit.

Boundaries are therefore **clustered by time**, not zipped by index.
Agreement tolerance varies by content class:

| class | tolerance |
|---|---|
| discrete album | 3s |
| live performance | 12s |
| continuous mix | 15s |
| ambient longform | 20s |

A crossfade is genuinely tens of seconds wide — during it, both tracks are
actually playing — so demanding album-grade precision on a mix would score
correct answers as failures.

The single-source rule applies per track: a boundary proposed by one source
alone is a lead, not a conclusion.
