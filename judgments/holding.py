"""Extracting what a judgment held.

For the Supreme Court this is genuinely solved rather than approximated. Every
reported judgment carries an official eSCR headnote whose ``Held:`` section
states the ratio in the Court's own words, followed by a ``List of Acts``
naming the statutes applied. Both are lifted verbatim.

That distinction matters for legal research: a holding quoted from the official
headnote can be relied on and cited, whereas a model's paraphrase of a judgment
is a reading of it. Anything this module returns is marked with its provenance
so the two can never be confused downstream, and the expensive, fallible option
is never used where the authoritative one exists.

The Bombay High Court publishes no headnotes, so its holdings are not available
this way — see :func:`holding_unavailable`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Provenance(str, Enum):
    #: Quoted verbatim from the court's own published headnote. Citable.
    HEADNOTE = "headnote"
    #: Produced by a language model reading the judgment. A reading, not a source.
    GENERATED = "generated"
    #: No holding could be obtained.
    NONE = "none"


@dataclass(frozen=True)
class Holding:
    text: str
    provenance: Provenance

    @property
    def is_verbatim(self) -> bool:
        return self.provenance is Provenance.HEADNOTE


#: Headings that follow the ``Held:`` block in the eSCR headnote layout.
#: The first one to appear ends the holding.
_TERMINATORS = (
    "Case Law Cited",
    "List of Acts",
    "List of Keywords",
    "Case Arising From",
    "Appearances for Parties",
    "CIVIL APPELLATE JURISDICTION",
    "CRIMINAL APPELLATE JURISDICTION",
    "CIVIL ORIGINAL JURISDICTION",
    "CRIMINAL ORIGINAL JURISDICTION",
)

#: Cap for a holding whose terminator is missing — some headnotes run long or
#: lose their next heading to OCR. Cutting at a sentence boundary below this is
#: better than emitting several pages of judgment body as a "holding".
_MAX_CHARS = 4000

_HELD = re.compile(r"\bHeld\s*:\s*", re.IGNORECASE)


def extract_held(text: str) -> Holding:
    """Pull the verbatim ``Held:`` section out of an eSCR headnote."""
    if not text:
        return Holding("", Provenance.NONE)

    match = _HELD.search(text)
    if not match:
        return Holding("", Provenance.NONE)

    body = text[match.end():]

    cuts = [body.find(t) for t in _TERMINATORS]
    cuts = [c for c in cuts if c > 0]
    end = min(cuts) if cuts else _MAX_CHARS

    held = body[:end]
    if not cuts and len(held) >= _MAX_CHARS:
        # Prefer a clean sentence end over a mid-word truncation.
        last = max(held.rfind(". "), held.rfind(".\n"))
        if last > _MAX_CHARS // 2:
            held = held[: last + 1]

    held = _tidy(held)
    if len(held) < 20:
        # Too short to be a real holding; a stray "held:" in running prose.
        return Holding("", Provenance.NONE)
    return Holding(held, Provenance.HEADNOTE)


def extract_acts(text: str) -> list[str]:
    """Pull the ``List of Acts`` section out of an eSCR headnote.

    This is the only structured statute signal the Supreme Court corpus offers —
    its Parquet metadata has no acts field — so act-wise grouping for that court
    depends on it.
    """
    if not text:
        return []
    start = text.find("List of Acts")
    if start < 0:
        return []

    body = text[start + len("List of Acts"):]
    after = [body.find(t) for t in ("List of Keywords", "Case Arising From",
                                    "Appearances for Parties", "Case Law Cited")]
    after = [c for c in after if c > 0]
    body = body[: min(after)] if after else body[:1500]

    # Entries are separated by semicolons only. Newlines inside this block are
    # PDF line-wrapping *within* a single name -- "National Company \nLaw
    # Appellate Tribunal Rules" is one statute -- so the text is unwrapped
    # before splitting; splitting on newlines truncates names instead.
    parts = _tidy(body).split(";")
    out: list[str] = []
    for part in parts:
        cleaned = part.strip(" .,:")
        # Require a statute-ish word so stray fragments do not become "acts".
        if len(cleaned) > 6 and re.search(
            r"\b(act|code|constitution|rules|regulation|sanhita|adhiniyam)\b",
            cleaned,
            re.IGNORECASE,
        ):
            out.append(cleaned)
    return out


def _tidy(text: str) -> str:
    """Collapse the whitespace and page furniture that PDF extraction leaves."""
    cleaned = text.replace("–", "-").replace("—", "-")
    # Page numbers and running headers stranded on their own lines.
    cleaned = re.sub(r"\n\s*\d{1,4}\s*\n", "\n", cleaned)
    cleaned = re.sub(r"\[\d{4}\]\s*\d+\s*S\.?C\.?R\.?\s*\d*", " ", cleaned)
    # Running page header, e.g. "426 Supreme Court Reports", which PDF
    # extraction interleaves into the text and which otherwise ends up glued
    # into a statute name.
    cleaned = re.sub(r"\d*\s*Supreme Court Reports\s*", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def holding_unavailable(court: str) -> Holding:
    """The honest result for a court that publishes no headnotes.

    Returned rather than a generated summary so that an empty holding is
    visibly empty. A caller that wants a model's reading must ask for one.
    """
    return Holding("", Provenance.NONE)
