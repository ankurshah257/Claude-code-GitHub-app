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

from ..classify import Classification, classify_high_court
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
    extra: dict[str, str] = field(default_factory=dict)


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
