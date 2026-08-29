"""Deciding whether a judgment is a civil matter.

The system exists to answer one question — civil or not — and the whole design
follows from refusing to answer it by guessing. Every classification names the
signals it used and what each one said, so a wrong answer can be traced to the
field that produced it rather than to an opaque call.

Two outcomes matter as much as CIVIL and CRIMINAL:

``UNKNOWN``
    No usable signal. Common in the Supreme Court corpus, where the metadata
    carries no case-type field and OCR sometimes loses the jurisdiction header.

``DISPUTED``
    Signals contradicted each other. Real: 5 Bombay bail applications carry a
    civil judicial section, and bail is definitionally criminal. Collapsing
    that to a verdict would hide a data-quality problem the caller should see.

Neither is a soft "no". A scan that wants completeness reviews them; a scan
that wants precision excludes them. That choice belongs to the caller, which
is why this module never makes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .taxonomy import (
    SC_CASE_DESIGNATIONS,
    SC_JURISDICTION_MARKERS,
    Matter,
    normalize_case_type,
    normalize_section,
)


class Outcome(str, Enum):
    CIVIL = "civil"
    CRIMINAL = "criminal"
    UNKNOWN = "unknown"
    DISPUTED = "disputed"


@dataclass(frozen=True)
class Signal:
    """One piece of evidence, named so a classification can be audited."""

    #: Which field this came from, e.g. "judicial_section".
    source: str
    #: The raw cell value, kept verbatim for tracing back to the corpus.
    raw: str
    #: What it indicates on its own.
    says: Matter
    #: How much this signal is trusted when signals disagree.
    weight: float


@dataclass(frozen=True)
class Classification:
    outcome: Outcome
    confidence: float
    signals: list[Signal] = field(default_factory=list)
    #: One line stating why, derived from the signals rather than narrated.
    rationale: str = ""

    @property
    def is_civil(self) -> bool:
        """True only for a settled civil outcome.

        DISPUTED and UNKNOWN deliberately return False: a caller filtering a
        corpus with ``if c.is_civil`` should get only what the system actually
        stands behind, and must opt in to the uncertain buckets explicitly.
        """
        return self.outcome is Outcome.CIVIL


# The court's own section label is authoritative; a case-type code is a strong
# prior but is court-specific and occasionally wrong. Weights encode exactly
# that ordering, and are the only place the precedence is expressed.
WEIGHT_SECTION = 1.0
WEIGHT_CASE_TYPE = 0.6
WEIGHT_SC_JURISDICTION = 1.0
WEIGHT_SC_DESIGNATION = 0.7


def classify_high_court(
    judicial_section: object = None,
    case_type: object = None,
) -> Classification:
    """Classify a High Court judgment from its structured metadata.

    Both fields are optional because coverage varies by bench and year; the
    result degrades to UNKNOWN rather than failing when neither is present.
    """
    signals: list[Signal] = []

    section = normalize_section(judicial_section)
    if section is not Matter.UNKNOWN:
        signals.append(
            Signal("judicial_section", str(judicial_section), section, WEIGHT_SECTION)
        )

    ctype = normalize_case_type(case_type)
    # A NEUTRAL code (WP, IA) is recorded as having been read but carries no
    # weight, so it neither supports nor contradicts the section.
    if ctype not in (Matter.UNKNOWN, Matter.NEUTRAL):
        signals.append(Signal("case_type", str(case_type), ctype, WEIGHT_CASE_TYPE))
    elif ctype is Matter.NEUTRAL:
        signals.append(Signal("case_type", str(case_type), Matter.NEUTRAL, 0.0))

    return _resolve(signals)


#: The jurisdiction header as it appears in Supreme Court Reports text.
#:
#: Whitespace is allowed to be any run because these documents are OCR'd from
#: print, and the SCR page layout drops single margin letters (A through H)
#: into the middle of lines — "Special Leave Petition D (Civil) No. 8094" is
#: real corpus text, not a typo.
_SC_HEADER = re.compile(
    r"\b(civil|criminal)\s+(appellate|original|review)\s+jurisdiction\b",
    re.IGNORECASE,
)

_SC_DESIGNATION = re.compile(
    r"\b(special\s+leave\s+petition|writ\s+petition|transfer\s+petition|"
    r"civil\s+miscellaneous\s+petition|civil\s+appeal|criminal\s+appeal)\b"
    r"(?:\s+[A-H])?"  # stray SCR margin letter
    # SCR abbreviates the qualifier: "(C)" for civil, "(Crl)" for criminal, as
    # in "Writ Petition (C) No. 432 of 2023". Alternatives are ordered
    # longest-first so "criminal" is not matched as a bare "c".
    r"\s*(?:\(\s*(criminal|civil|crl|c)\s*\))?",
    re.IGNORECASE,
)


def classify_supreme_court(text: str) -> Classification:
    """Classify a Supreme Court judgment from its text.

    The SC metadata has no case-type or section field — only a neutral citation
    and party names — so the text is the only place the nature of the matter is
    stated. Reading is confined to the head of the document, where the
    jurisdiction header sits; scanning the body would pick up every passing
    reference to a criminal case in a civil judgment's citations.
    """
    signals: list[Signal] = []
    if not text:
        return Classification(Outcome.UNKNOWN, 0.0, [], "No text to classify.")

    head = text[:20_000]

    for match in _SC_HEADER.finditer(head):
        phrase = re.sub(r"\s+", " ", match.group(0)).lower()
        matter = SC_JURISDICTION_MARKERS.get(phrase)
        if matter:
            signals.append(
                Signal("jurisdiction_header", match.group(0), matter, WEIGHT_SC_JURISDICTION)
            )

    for match in _SC_DESIGNATION.finditer(head):
        matter = _designation_matter(match)
        if matter:
            signals.append(
                Signal("case_designation", match.group(0).strip(), matter, WEIGHT_SC_DESIGNATION)
            )

    return _resolve(_dedupe(signals))


def _designation_matter(match: re.Match[str]) -> Matter | None:
    """Resolve a case designation, preferring its explicit (Civil)/(Crl) tag."""
    base = re.sub(r"\s+", " ", match.group(1)).strip().lower()
    qualifier = (match.group(2) or "").lower()

    if qualifier:
        return Matter.CIVIL if qualifier in {"civil", "c"} else Matter.CRIMINAL

    # "Civil Appeal" / "Criminal Appeal" carry their nature in the name; the
    # petition forms do not, and an unqualified one says nothing.
    return SC_CASE_DESIGNATIONS.get(base)


def _dedupe(signals: list[Signal]) -> list[Signal]:
    """Collapse repeats so a phrase appearing twice does not count twice.

    Without this, a judgment that repeats its header in a running page title
    would look like independent corroboration and inflate confidence.
    """
    seen: set[tuple[str, Matter]] = set()
    out: list[Signal] = []
    for s in signals:
        key = (s.source, s.says)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _resolve(signals: list[Signal]) -> Classification:
    """Combine signals into an outcome. Pure and deterministic."""
    weighted = [s for s in signals if s.weight > 0]

    if not weighted:
        read = ", ".join(s.source for s in signals)
        return Classification(
            Outcome.UNKNOWN,
            0.0,
            signals,
            f"No signal carried information ({read})." if read else "No signals found.",
        )

    civil = sum(s.weight for s in weighted if s.says is Matter.CIVIL)
    criminal = sum(s.weight for s in weighted if s.says is Matter.CRIMINAL)

    if civil > 0 and criminal > 0:
        # Report the conflict rather than letting the heavier side win quietly.
        for_ = _describe(weighted, Matter.CIVIL)
        against = _describe(weighted, Matter.CRIMINAL)
        return Classification(
            Outcome.DISPUTED,
            0.0,
            signals,
            f"Signals disagree: {for_} indicates civil, {against} indicates criminal.",
        )

    total = civil + criminal
    if total == 0:
        return Classification(
            Outcome.UNKNOWN, 0.0, signals, "Signals were read but none was decisive."
        )

    outcome = Outcome.CIVIL if civil > criminal else Outcome.CRIMINAL
    winning = civil if outcome is Outcome.CIVIL else criminal

    # Confidence is the agreeing weight capped at 1, so a lone corroborating
    # signal cannot reach the certainty of the authoritative one.
    confidence = min(1.0, winning)
    return Classification(
        outcome,
        confidence,
        signals,
        f"{_describe(weighted, Matter.CIVIL if outcome is Outcome.CIVIL else Matter.CRIMINAL)}"
        f" indicates {outcome.value}.",
    )


def _describe(signals: list[Signal], matter: Matter) -> str:
    names = [f"{s.source}={s.raw!r}" for s in signals if s.says is matter]
    return " and ".join(names) if names else "nothing"
