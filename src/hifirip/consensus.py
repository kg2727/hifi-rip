"""Reconciling metadata claims from independent sources.

Sources disagree constantly. MusicBrainz and Discogs spell featured artists
differently, uploaders write their own titles, and community tracklists carry
whatever the submitter typed. So values are not taken from a "best" source;
they are corroborated across several, weighted by how reliable each has
proven to be.

Three rules shape this:

**Weight is not a licence.** A high-legitimacy source is more persuasive, but
never sufficient alone. A value backed by exactly one source is not consensus
regardless of that source's authority -- 1001Tracklists at 0.9 and a random
comment at 0.4 both produce a *lead* rather than a conclusion, and are
reported as such. This is what stops a single well-regarded database from
silently deciding every field.

**Agreement is resolved without a model.** Sources that agree after
normalisation are settled arithmetically. Escalation costs latency and money,
so it is spent only on genuine disagreement.

**Independence is counted, not assumed.** Two sources that copied from each
other are one source wearing two hats, which is why `independent_of` exists:
Last.fm largely mirrors MusicBrainz, and counting them as corroboration would
manufacture agreement that was never there.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

#: Values backed by fewer than this many independent sources are leads, not
#: conclusions, however authoritative the source.
MIN_INDEPENDENT_SOURCES = 2

#: The winning value must hold at least this share of total weight, or the
#: field is treated as contested and escalated.
DOMINANCE_THRESHOLD = 0.6


class Status(Enum):
    AGREED = "agreed"
    SINGLE_SOURCE = "single-source"
    CONFLICTED = "conflicted"
    ABSENT = "absent"


@dataclass(frozen=True)
class Claim:
    """One source's assertion about one field."""

    field: str
    value: str
    source: str
    weight: float
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.source} ({self.weight:.2f}): {self.value!r}"


# ---------------------------------------------------------------------------
# Normalisation
#
# Comparison happens on a normalised form so that trivial spelling differences
# count as agreement. The original text is always preserved for output -- we
# normalise to *compare*, never to decide what the user sees.
# ---------------------------------------------------------------------------

#: Words that describe the *upload* rather than the recording. Deliberately
#: excludes "live", "acoustic", "remix", "extended" and "radio edit": those
#: identify a genuinely different recording, and stripping them would merge
#: two distinct things into one.
NOISE_WORD = (
    r"(?:official|music|video|audio|lyrics?|hd|hq|uhd|[48]k|remaster(?:ed)?"
    r"|visuali[sz]er|mv|explicit|clean|full\s*album|free\s*download|\d{4})"
)

#: Bracketed furniture, allowing several noise words in one bracket so that
#: "(4K Remaster)" and "(Official Music Video HD)" come off whole. Matching a
#: single word per bracket left the rest behind, and a release database holds
#: no recording by that name.
NOISE = re.compile(
    r"\s*[\(\[\{]\s*" + NOISE_WORD + r"(?:[\s\-,]+" + NOISE_WORD + r")*\s*[\)\]\}]",
    re.IGNORECASE,
)

FEATURING = re.compile(r"\b(feat\.?|ft\.?|featuring|w/)\s+", re.IGNORECASE)
#: Apostrophes are removed rather than replaced with a space: they sit
#: *inside* words, so spacing them turns "Don't" into "don t", which no
#: longer matches the equally common "Dont". Every other punctuation mark
#: separates words and becomes a space.
APOSTROPHE = re.compile(r"['’ʼ´`]")
PUNCTUATION = re.compile(r"[^\w\s]")
WHITESPACE = re.compile(r"\s+")


def normalise(value: str) -> str:
    """Reduce a value to a comparable form.

    Unicode is folded first: a curly apostrophe and a straight one are the
    same character to a human, and sources are wildly inconsistent about it.
    """
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = NOISE.sub(" ", text)
    text = FEATURING.sub("feat ", text)
    text = APOSTROPHE.sub("", text)
    text = PUNCTUATION.sub(" ", text)
    text = WHITESPACE.sub(" ", text)
    return text.strip().lower()


def strip_noise(value: str) -> str:
    """Remove uploader furniture while keeping the value human-readable.

    `normalise` is for comparison and destroys case and punctuation, which
    makes it useless as a search term. This keeps the title intact and only
    drops the parts that describe the *upload* rather than the recording --
    "(Official Video)", "[4K Remaster]" and so on. Searching a release
    database for those verbatim reliably matches nothing.
    """
    return WHITESPACE.sub(" ", NOISE.sub(" ", value or "")).strip(" -–—|")


def equivalent(left: str, right: str) -> bool:
    return normalise(left) == normalise(right)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    """What the sources collectively support for one field."""

    field: str
    value: str | None
    status: Status
    confidence: float = 0.0
    supporting: list[Claim] = field(default_factory=list)
    dissenting: list[Claim] = field(default_factory=list)

    @property
    def independent_sources(self) -> int:
        return len({claim.source for claim in self.supporting})

    @property
    def needs_judgement(self) -> bool:
        """Whether a host agent has to settle this."""
        return self.status in (Status.CONFLICTED, Status.SINGLE_SOURCE)

    @property
    def settled(self) -> bool:
        return self.status is Status.AGREED

    def describe(self) -> str:
        if self.status is Status.ABSENT:
            return f"{self.field}: no source offered a value"
        backing = ", ".join(claim.source for claim in self.supporting)
        line = f"{self.field}: {self.value!r} [{self.status.value}, {backing}]"
        if self.dissenting:
            rival = ", ".join(f"{c.source}={c.value!r}" for c in self.dissenting)
            line += f" -- contested by {rival}"
        return line


def reconcile(
    claims: list[Claim],
    *,
    min_sources: int = MIN_INDEPENDENT_SOURCES,
    dominance: float = DOMINANCE_THRESHOLD,
    independent_of: dict[str, set[str]] | None = None,
) -> Resolution:
    """Resolve one field's claims into a value, or defer.

    `independent_of` maps a source to the sources it derives from. Claims
    from a derived source do not count towards the independent-source tally
    when its upstream already voted, because agreement between a database and
    a mirror of that database is not corroboration.
    """
    if not claims:
        return Resolution(field="unknown", value=None, status=Status.ABSENT)

    field_name = claims[0].field
    groups: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        if claim.value and claim.value.strip():
            groups[normalise(claim.value)].append(claim)

    if not groups:
        return Resolution(field=field_name, value=None, status=Status.ABSENT)

    scored = sorted(
        groups.items(),
        key=lambda item: (sum(c.weight for c in item[1]), len(item[1])),
        reverse=True,
    )
    _, winners = scored[0]
    losers = [claim for _, group in scored[1:] for claim in group]

    total = sum(claim.weight for claim in claims if claim.value)
    winning_weight = sum(claim.weight for claim in winners)
    share = winning_weight / total if total else 0.0

    # Pick the most authoritative spelling from the winning group rather than
    # the normalised form, which is lossy by design.
    best = max(winners, key=lambda claim: claim.weight)

    effective = _independent_count(winners, independent_of or {})

    # Order matters. Active disagreement is tested first: when rivals exist,
    # the winning group naturally holds few sources, and checking the source
    # count first would report a two-way dispute as "only one source offered
    # a value" -- telling the host the opposite of what is happening.
    if losers and share < dominance:
        status = Status.CONFLICTED
    elif effective < min_sources:
        status = Status.SINGLE_SOURCE
    else:
        status = Status.AGREED

    return Resolution(
        field=field_name,
        value=best.value,
        status=status,
        confidence=round(share, 3),
        supporting=winners,
        dissenting=losers,
    )


def _independent_count(claims: list[Claim], derives_from: dict[str, set[str]]) -> int:
    """Count sources that are not restatements of one another."""
    present = {claim.source for claim in claims}
    counted = set()
    for source in present:
        upstream = derives_from.get(source, set())
        if upstream & present:
            # This source's data came from another that already voted.
            continue
        counted.add(source)
    return len(counted) or (1 if present else 0)


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

@dataclass
class ConflictReport:
    """Everything a host agent needs to adjudicate, and nothing else."""

    resolutions: list[Resolution] = field(default_factory=list)

    @property
    def unresolved(self) -> list[Resolution]:
        return [r for r in self.resolutions if r.needs_judgement]

    @property
    def settled(self) -> list[Resolution]:
        return [r for r in self.resolutions if r.settled]

    @property
    def needs_host(self) -> bool:
        return bool(self.unresolved)

    def brief(self) -> str:
        """Render the adjudication request.

        Only unresolved fields appear. Handing a model everything, including
        the fields the arithmetic already settled, invites it to relitigate
        settled questions and costs tokens for no gain.
        """
        if not self.unresolved:
            return "All fields resolved by source agreement; no judgement needed."

        lines = [
            f"{len(self.unresolved)} field(s) could not be resolved by source "
            f"agreement. Settled fields are omitted deliberately -- do not "
            f"revisit them.",
            "",
        ]
        for resolution in self.unresolved:
            lines.append(f"FIELD: {resolution.field}")
            if resolution.status is Status.SINGLE_SOURCE:
                lines.append(
                    "  Only one independent source offered a value. That is a "
                    "lead, not a consensus, however reliable the source. Accept "
                    "it only if it is independently plausible."
                )
            else:
                lines.append(
                    f"  Sources disagree; the leading value holds only "
                    f"{resolution.confidence:.0%} of the weight."
                )
            lines.append("  Candidates:")
            for claim in resolution.supporting:
                lines.append(f"    - {claim}" + (f"  // {claim.detail}" if claim.detail else ""))
            for claim in resolution.dissenting:
                lines.append(f"    - {claim}" + (f"  // {claim.detail}" if claim.detail else ""))
            lines.append("")

        lines.append(
            "Prefer the reading supported by the most *independent* sources "
            "over the most confidently-worded one. Weights reflect each "
            "source's track record, not its certainty about this particular "
            "value. If the evidence does not settle a field, say so and leave "
            "it unset rather than choosing the least-bad option -- an empty "
            "field is honest, a wrong one propagates into the user's library."
        )
        return "\n".join(lines)


def reconcile_all(
    claims: list[Claim],
    *,
    min_sources: int = MIN_INDEPENDENT_SOURCES,
    dominance: float = DOMINANCE_THRESHOLD,
    independent_of: dict[str, set[str]] | None = None,
) -> ConflictReport:
    """Reconcile every field present in `claims`."""
    by_field: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        by_field[claim.field].append(claim)

    return ConflictReport(resolutions=[
        reconcile(
            group, min_sources=min_sources, dominance=dominance,
            independent_of=independent_of,
        )
        for _, group in sorted(by_field.items())
    ])


def apply_judgement(
    report: ConflictReport, decisions: dict[str, str | None]
) -> ConflictReport:
    """Fold a host agent's rulings back into the report.

    A field the host declined to settle stays unset rather than falling back
    to the leading candidate: the whole point of asking was that the leading
    candidate was not trustworthy.
    """
    for resolution in report.resolutions:
        if resolution.field not in decisions:
            continue
        chosen = decisions[resolution.field]
        resolution.value = chosen
        resolution.status = Status.AGREED if chosen else Status.ABSENT
        resolution.confidence = 1.0 if chosen else 0.0
    return report
