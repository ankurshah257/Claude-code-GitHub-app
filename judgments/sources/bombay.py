"""Bombay High Court adapter.

Reads the partitioned Parquet metadata for ``court=27_1`` and classifies each
matter. No PDF is opened: the court's case-management export already carries a
``judicial_section`` field, which states civil or criminal on the court's own
authority. Parsing judgment text would be slower, lossier, and less defensible
than a field the registry itself populated.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Iterator

import pandas as pd

from ..acts import ActRef, parse as parse_acts
from ..classify import Classification, classify_high_court
from ..holding import Holding, Provenance
from ..taxonomy import BOMBAY_BENCHES, BOMBAY_COURT_CODE
from . import s3

#: Columns this adapter needs. The corpus has 32; reading only these keeps a
#: full-history scan to a size that fits in memory a bench-year at a time.
COLUMNS = [
    "cnr",
    "order_number",
    "is_final",
    "case_type",
    "case_no",
    "judicial_section",
    "decision_date",
    "title",
    "petitioner",
    "respondent",
    "judge",
    "bench_name",
    "disposal_nature",
    "pdf_link",
]


@dataclass(frozen=True)
class Judgment:
    """One judgment, court-agnostic so both adapters yield the same shape."""

    #: Stable unique id for one *document*. A case yields many orders over its
    #: life (about 3.4 on average), so this is the case id plus the order
    #: number; the case id alone would collapse the corpus by two thirds.
    uid: str
    #: Id of the case this document belongs to, for case-level grouping.
    case_id: str
    court: str
    bench: str
    case_type: str
    case_no: str
    title: str
    decision_date: str
    classification: Classification
    year: int
    #: True for a final judgment, False for an interim order. Most documents in
    #: the corpus are interim, so this is usually what "judgment" should mean.
    is_final: bool = False
    petitioner: str = ""
    respondent: str = ""
    judge: str = ""
    disposal: str = ""
    url: str = ""
    #: Statutes the matter turns on, canonicalised. Empty when the source does
    #: not record them.
    acts: list[ActRef] = field(default_factory=list)
    #: What the judgment held, with its provenance. Empty for courts that
    #: publish no headnotes.
    holding: Holding = field(default_factory=lambda: Holding("", Provenance.NONE))
    extra: dict[str, str] = field(default_factory=dict)


def _today() -> str:
    from datetime import date as _date

    return _date.today().isoformat()


def to_iso(date: str) -> str:
    """Normalise a corpus date to ISO ``YYYY-MM-DD``.

    The two exports disagree: the High Court writes ``YYYY-MM-DD`` and the
    Supreme Court writes ``DD-MM-YYYY``. Storing both verbatim would put mixed
    formats in one column, where a string comparison silently excludes every
    Supreme Court judgment from a date window.
    """
    text = (date or "").strip()[:10]
    if len(text) == 10 and text[2] == "-" and text[5] == "-":
        return f"{text[6:]}-{text[3:5]}-{text[:2]}"
    return text


def _text(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def available_years(timeout: int = 60) -> list[str]:
    """Years present in the High Court metadata partition."""
    return s3.list_partition_values(
        s3.HIGH_COURT_BUCKET, "metadata/parquet/", "year",
    )


def available_benches(year: str) -> list[str]:
    """Bombay benches present for a given year."""
    return s3.list_partition_values(
        s3.HIGH_COURT_BUCKET,
        f"metadata/parquet/year={year}/court={BOMBAY_COURT_CODE}/",
        "bench",
    )


def _parquet_key(year: str, bench: str, mobile: bool) -> str:
    name = "metadata-mobile.parquet" if mobile else "metadata.parquet"
    return (
        f"metadata/parquet/year={year}/court={BOMBAY_COURT_CODE}/bench={bench}/{name}"
    )


def scan_bench(year: str, bench: str, mobile: bool = True) -> Iterator[Judgment]:
    """Yield every Bombay judgment for one year and bench, classified.

    ``mobile`` selects the compact export, which carries every field this
    adapter reads at roughly half the bytes of the full one.
    """
    try:
        blob = s3.fetch(s3.HIGH_COURT_BUCKET, _parquet_key(year, bench, mobile))
    except s3.S3Error:
        # A bench that filed nothing in a given year simply has no object.
        # That is ordinary sparsity, not an error worth aborting a scan for.
        return

    frame = pd.read_parquet(io.BytesIO(blob))
    present = [c for c in COLUMNS if c in frame.columns]

    for row in frame[present].itertuples(index=False):
        record = dict(zip(present, row))
        classification = classify_high_court(
            judicial_section=record.get("judicial_section"),
            case_type=record.get("case_type"),
        )

        case_id = _text(record.get("cnr"))
        order_no = _text(record.get("order_number"))
        if not case_id:
            # Without a stable id a record cannot be deduplicated across
            # overlapping partitions, so it is dropped rather than double-counted.
            continue

        yield Judgment(
            uid=f"{case_id}:{order_no}" if order_no else case_id,
            case_id=case_id,
            court="Bombay High Court",
            bench=BOMBAY_BENCHES.get(bench, bench),
            case_type=_text(record.get("case_type")),
            case_no=_text(record.get("case_no")),
            title=_text(record.get("title")),
            decision_date=_text(record.get("decision_date"))[:10],
            classification=classification,
            year=int(year),
            is_final=bool(record.get("is_final")),
            petitioner=_text(record.get("petitioner")),
            respondent=_text(record.get("respondent")),
            judge=_text(record.get("judge")),
            disposal=_text(record.get("disposal_nature")),
            url=_text(record.get("pdf_link")),
            extra={"judicial_section": _text(record.get("judicial_section"))},
        )


def scan_year(year: str, mobile: bool = True) -> Iterator[Judgment]:
    """Yield every Bombay judgment for one year across all benches."""
    for bench in available_benches(year):
        yield from scan_bench(year, bench, mobile=mobile)


# --------------------------------------------------------------------------
# Case-level scan
#
# The metadata partition above is order-level: one row per order, several per
# case. For a database *of judgments* that is the wrong grain — it would list
# the same matter a dozen times as it moved through the court.
#
# The case-details export is one row per case, and it is also the only place
# the registry records which Acts a matter turns on. So act-wise work reads
# this instead. It is not year-partitioned, so callers filter by decision date.
# --------------------------------------------------------------------------

CASE_COLUMNS = [
    "cnr",
    "case_type",
    "case_no",
    "judicial_section",
    "date_of_decision",
    "petitioner",
    "respondent",
    "judge",
    "bench_name",
    "disposal_nature",
    "acts",
]


def _case_details_key(bench: str) -> str:
    return (
        f"metadata/parquet_case_details/court={BOMBAY_COURT_CODE}/"
        f"bench={bench}/case_details-mobile.parquet"
    )


def case_detail_benches() -> list[str]:
    """Benches for which a case-details export exists."""
    return s3.list_partition_values(
        s3.HIGH_COURT_BUCKET,
        f"metadata/parquet_case_details/court={BOMBAY_COURT_CODE}/",
        "bench",
    )


def scan_cases(bench: str, since: str | None = None) -> Iterator[Judgment]:
    """Yield one classified record per *case* for a bench.

    ``since`` is an ISO date; only judgments decided on or after it are yielded.
    """
    try:
        blob = s3.fetch(s3.HIGH_COURT_BUCKET, _case_details_key(bench))
    except s3.S3Error:
        return

    frame = pd.read_parquet(io.BytesIO(blob))
    present = [c for c in CASE_COLUMNS if c in frame.columns]

    for row in frame[present].itertuples(index=False):
        record = dict(zip(present, row))

        decided = _text(record.get("date_of_decision"))[:10]
        # An undecided matter is pending, not a judgment.
        if not decided or (since and decided < since):
            continue
        # A handful of rows carry a decision date in the future, which is a
        # registry data-entry error. Admitting them puts judgments that have
        # not happened at the top of every date-sorted list.
        if decided > _today():
            continue

        uid = _text(record.get("cnr"))
        if not uid:
            continue

        petitioner = _text(record.get("petitioner"))
        respondent = _text(record.get("respondent"))
        name = f"{petitioner} v. {respondent}" if petitioner and respondent else (
            petitioner or respondent or _text(record.get("case_no"))
        )

        yield Judgment(
            uid=uid,
            case_id=uid,
            court="Bombay High Court",
            bench=BOMBAY_BENCHES.get(bench, bench),
            case_type=_text(record.get("case_type")),
            case_no=_text(record.get("case_no")),
            title=name,
            decision_date=decided,
            classification=classify_high_court(
                judicial_section=record.get("judicial_section"),
                case_type=record.get("case_type"),
            ),
            year=int(decided[:4]) if decided[:4].isdigit() else 0,
            # Case-details rows carry a decision date, so each is a decided
            # matter rather than an interim step.
            is_final=True,
            petitioner=petitioner,
            respondent=respondent,
            judge=_text(record.get("judge")),
            disposal=_text(record.get("disposal_nature")),
            acts=parse_acts(record.get("acts")),
            # The Bombay High Court publishes no headnotes, so there is no
            # authoritative statement of the holding to quote.
            holding=Holding("", Provenance.NONE),
            extra={"judicial_section": _text(record.get("judicial_section"))},
        )


# --------------------------------------------------------------------------
# Locating judgment PDFs
#
# The case-details export names no PDF. The objects themselves are keyed
# <CNR>_<order>_<decision date>.pdf, but the year= partition they sit under is
# the *crawl* year, not the decision year -- a 2006 judgment appears under
# year=2026 -- so a key cannot be computed from a case record. It has to be
# looked up, which means listing the partition once and indexing it.
# --------------------------------------------------------------------------


def pdf_index(bench: str, crawl_years: list[str]) -> dict[str, list[str]]:
    """Map CNR to its PDF object keys, newest order last.

    Listing is the expensive part (roughly a request per thousand objects), so
    the caller builds this once per bench and reuses it across every judgment.
    """
    index: dict[str, list[str]] = {}
    for year in crawl_years:
        prefix = f"data/pdf/year={year}/court={BOMBAY_COURT_CODE}/bench={bench}/"
        for obj in s3.list_objects(s3.HIGH_COURT_BUCKET, prefix):
            if isinstance(obj, str) or not obj.key.endswith(".pdf"):
                continue
            name = obj.key.rsplit("/", 1)[-1]
            cnr = name.split("_", 1)[0]
            index.setdefault(cnr, []).append(obj.key)

    # The last order of a case is the one that disposes of it, which is the
    # document a holding should come from.
    for keys in index.values():
        keys.sort()
    return index


class NotAPdf(RuntimeError):
    """The stored object is not a PDF.

    Some ``.pdf`` keys in this bucket hold the court site's own 404 page,
    saved by the crawler when the source link was already dead. They are a
    couple of hundred bytes of HTML and S3 serves them with a 200, so nothing
    upstream flags them -- only the magic bytes distinguish them from a real
    judgment.
    """


def judgment_text(key: str, max_pages: int = 25) -> str:
    """Fetch a Bombay judgment PDF and extract its text.

    These have a real text layer, but the extraction is often character-spaced
    ("IN T H E HIG H CO U R T"), which downstream consumers must tolerate.
    """
    from pypdf import PdfReader

    blob = s3.fetch(s3.HIGH_COURT_BUCKET, key)
    if not blob.startswith(b"%PDF-"):
        raise NotAPdf(f"{key} is not a PDF ({len(blob)} bytes)")
    reader = PdfReader(io.BytesIO(blob))
    parts = []
    for page in reader.pages[:max_pages]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(parts)
