"""Canonicalising the statutes a judgment turns on.

The corpus records acts as free text, and the same statute appears many ways:
``Indian Succession Act, 1925``, ``Indian Succession Act``, ``HINDU SUCCESSION
ACT``, ``Companies Act & Rules  1956``. Grouping judgments by act means
reconciling roughly a thousand raw spellings per bench.

The temptation is to strip the year and match on the name. That is wrong, and
dangerously so for legal research: the year is often the only thing separating
two different statutes. ``Companies Act 1956`` and ``Companies Act 2013`` are
distinct enactments, as are ``Arbitration Act 1940`` and ``Arbitration and
Conciliation Act 1996`` — the latter repealed the former. Merging them would
silently pool judgments decided under repealed law with current law.

So this module normalises *rendering* only — case, punctuation, spacing,
abbreviations — and keeps the year as part of the identity. A year-less variant
is merged into a dated one only when the corpus offers exactly one candidate;
where it is ambiguous the variant is left standing on its own rather than
being assigned to a guess.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ActRef:
    """One statute reference attached to a judgment."""

    #: Canonical name without the year, e.g. "code of civil procedure".
    name: str
    #: Enactment year when the source stated one. Part of the identity.
    year: int | None
    #: Display form, e.g. "Code of Civil Procedure, 1908".
    label: str
    #: Section or rule as recorded, lightly cleaned. May be empty.
    section: str = ""
    #: The original string, kept so a grouping can be audited.
    raw: str = ""

    @property
    def key(self) -> str:
        """Stable grouping key."""
        return f"{self.name}|{self.year or ''}"


#: Abbreviations that appear instead of the full name. Deliberately short: each
#: entry asserts two spellings are the same statute, which is a claim about law,
#: not formatting, so only unambiguous ones belong here.
_ABBREVIATIONS = {
    "cpc": "code of civil procedure",
    "c p c": "code of civil procedure",
    "crpc": "code of criminal procedure",
    "c r p c": "code of criminal procedure",
    "ipc": "indian penal code",
    "i p c": "indian penal code",
    "mv act": "motor vehicles act",
    "n i act": "negotiable instruments act",
    "ni act": "negotiable instruments act",
}

#: Noise the registry appends that does not change which statute is meant.
# No leading \b: a word boundary cannot match before "(", since a space and an
# opening paren are both non-word characters, which silently disabled the
# parenthetical rules until it was removed.
_NOISE = re.compile(
    r"(\band\s+rules\b|&\s*rules\b|\bread\s+with\b.*$|"
    r"\(\s*interlocutory\s+order\s*\)|\(.*?amendment.*?\)\s*$)",
    re.IGNORECASE,
)

_YEAR = re.compile(r"\b(1[6-9]\d{2}|20\d{2})\b")


def _strip_year(text: str) -> tuple[str, int | None]:
    """Pull the enactment year out, if the string states one."""
    years = _YEAR.findall(text)
    if not years:
        return text, None
    # The last year is the enactment in forms like
    # "Companies (… Section 274(1)(g) of the Companies Act 1956) Rules".
    year = int(years[-1])
    return _YEAR.sub(" ", text), year


def canonical(raw: str) -> ActRef | None:
    """Normalise one raw act name. Returns None for empty or junk input."""
    if not raw:
        return None

    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None

    working = text.lower()
    working = working.replace("&", " and ")
    working = re.sub(r"[.,;:]+", " ", working)

    working, year = _strip_year(working)
    working = _NOISE.sub(" ", working)
    working = re.sub(r"[^a-z0-9()\s-]", " ", working)
    working = re.sub(r"\s+", " ", working).strip()
    # A trailing parenthetical that is only an abbreviation of the name just
    # restates it: "criminal procedure code (cr p c)". Dropping it keeps one
    # entry per statute instead of one per way of abbreviating it.
    # Trailing space inside the parens is normal once "Cr.P.C." has had its
    # dots replaced, so allow it rather than letting the rule miss.
    working = re.sub(r"\(\s*(?:[a-z]{1,3}\s+){1,4}[a-z]{1,3}\s*\)\s*$", "", working).strip()
    working = working.strip(" -()")
    # A dangling conjunction is left behind when "Act & Rules 1988" has its
    # "rules" half removed, giving "motor vehicles act and".
    working = re.sub(r"\s+(and|or|the|of|with)$", "", working).strip()
    working = re.sub(r"\s+", " ", working)

    working = _ABBREVIATIONS.get(working, working)
    for short, full in _ABBREVIATIONS.items():
        if re.fullmatch(rf"{re.escape(short)}\s*(act)?", working):
            working = full
            break

    # A bare "act" or a fragment carries no identity.
    if not working or working in {"act", "rules", "the act"}:
        return None

    return ActRef(name=working, year=year, label=_label(working, year), raw=text)


def _label(name: str, year: int | None) -> str:
    """Human-readable display form, title-cased with small words left alone."""
    small = {"of", "and", "the", "in", "for", "to", "under", "or"}
    words = []
    for i, word in enumerate(name.split()):
        if i > 0 and word in small:
            words.append(word)
        else:
            words.append(word.capitalize())
    display = " ".join(words)
    return f"{display}, {year}" if year else display


def parse(acts_field: object, ) -> list[ActRef]:
    """Parse the corpus ``acts`` field, an array of ``{act, section}`` records."""
    if acts_field is None:
        return []
    out: list[ActRef] = []
    seen: set[str] = set()
    for entry in acts_field:
        if not isinstance(entry, dict):
            continue
        ref = canonical(entry.get("act", ""))
        if ref is None:
            continue
        section = re.sub(r"\s+", " ", str(entry.get("section", "") or "")).strip(" ,;")
        ref = ActRef(ref.name, ref.year, ref.label, section, ref.raw)
        if ref.key in seen:
            continue
        seen.add(ref.key)
        out.append(ref)
    return out


def resolve_years(refs: Iterable[ActRef]) -> dict[str, int]:
    """Work out which year-less act names can safely adopt a year.

    Given the whole corpus, a name seen both with and without a year is the same
    statute — provided only one year ever appears with it. ``Indian Succession
    Act`` resolves to 1925 because that is the only year it is ever given.
    ``Companies Act`` does not resolve, because the corpus carries both 1956 and
    2013 and choosing either would misfile judgments under a statute that was
    not applied.

    Returns a mapping of name to the year it should adopt, containing only the
    names where the answer is unambiguous.
    """
    years: dict[str, set[int]] = defaultdict(set)
    for ref in refs:
        if ref.year is not None:
            years[ref.name].add(ref.year)
    return {name: next(iter(ys)) for name, ys in years.items() if len(ys) == 1}


def apply_resolution(ref: ActRef, resolution: dict[str, int]) -> ActRef:
    """Attach a resolved year to a year-less reference, if one was found."""
    if ref.year is not None or ref.name not in resolution:
        return ref
    year = resolution[ref.name]
    return ActRef(ref.name, year, _label(ref.name, year), ref.section, ref.raw)
