"""Supreme Court of India adapter.

This court needs a different strategy from the High Court, and it is worth
being explicit about why. The Supreme Court metadata export carries no
``case_type`` and no ``judicial_section`` — only a neutral citation
(``2024 INSC 735``), an SCR citation, and party names. Nothing in it says
whether a matter is civil or criminal.

The judgment text does say so, in a jurisdiction header near the top
(``CIVIL APPELLATE JURISDICTION``). So this adapter pairs each metadata row
with its PDF and reads the head of the document. That is far more expensive
than the High Court path — a network fetch and a PDF parse per judgment
instead of one Parquet file per bench-year — which is why the metadata scan
and the classification pass are separate calls here: listing the corpus is
cheap and should not require downloading it.
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Iterable, Iterator

import pandas as pd

from ..acts import canonical
from ..classify import Classification, Outcome, classify_supreme_court
from ..holding import Holding, Provenance, extract_acts, extract_held
from .bombay import Judgment, _text, to_iso
from . import s3

#: Pages read per judgment. The jurisdiction header and case designation sit in
#: the first page or two; reading further costs time and risks matching a
#: criminal case cited inside a civil judgment.
HEAD_PAGES = 3


def available_years() -> list[str]:
    """Years present in the Supreme Court metadata partition."""
    return s3.list_partition_values(
        s3.SUPREME_COURT_BUCKET, "metadata/parquet/", "year"
    )


def scan_year(year: str) -> Iterator[Judgment]:
    """Yield every Supreme Court judgment for a year, *unclassified*.

    Classification is deliberately not done here: it requires downloading each
    PDF. Callers that only need the index (counts, titles, dates) should not
    pay for that, and callers that do need it pass the result to
    :func:`classify_all`.
    """
    try:
        blob = s3.fetch(
            s3.SUPREME_COURT_BUCKET, f"metadata/parquet/year={year}/metadata.parquet"
        )
    except s3.S3Error:
        return

    frame = pd.read_parquet(io.BytesIO(blob))
    for row in frame.itertuples(index=False):
        record = row._asdict()
        path = _text(record.get("path"))
        uid = _text(record.get("case_id")) or path
        if not uid:
            continue

        yield Judgment(
            uid=uid,
            case_id=uid,
            court="Supreme Court of India",
            bench="",
            case_type="",
            case_no=_text(record.get("citation")),
            title=_text(record.get("title")),
            decision_date=to_iso(_text(record.get("decision_date"))),
            # Nothing has been read yet, so the honest state is UNKNOWN rather
            # than a default that a caller might mistake for a finding.
            classification=Classification(
                Outcome.UNKNOWN, 0.0, [], "Not yet classified (text not read)."
            ),
            year=int(year),
            # Every document in this corpus is a reported judgment, not an
            # interim order — unlike the High Court export.
            is_final=True,
            petitioner=_text(record.get("petitioner")),
            respondent=_text(record.get("respondent")),
            judge=_text(record.get("judge")),
            disposal=_text(record.get("disposal_nature")),
            url=_pdf_url(year, path),
            extra={"path": path, "citation": _text(record.get("citation"))},
        )


def _pdf_key(year: str, path: str) -> str:
    return f"data/pdf/year={year}/english/{path}_EN.pdf"


def _pdf_url(year: str, path: str) -> str:
    return f"{s3.SUPREME_COURT_BUCKET}/{_pdf_key(year, path)}" if path else ""


def head_text(year: str, path: str, pages: int = HEAD_PAGES) -> str:
    """Fetch a judgment PDF and extract text from its first pages."""
    # Imported lazily so that listing the corpus, and the High Court path,
    # do not require a PDF library to be installed at all.
    from pypdf import PdfReader

    blob = s3.fetch(s3.SUPREME_COURT_BUCKET, _pdf_key(year, path))
    reader = PdfReader(io.BytesIO(blob))
    parts = []
    for page in reader.pages[:pages]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            # A single malformed page should not lose the rest of the document;
            # the jurisdiction header is usually on page one regardless.
            continue
    return "\n".join(parts)


def classify_one(judgment: Judgment) -> Judgment:
    """Return a copy of `judgment` with its text read and classified."""
    path = judgment.extra.get("path", "")
    if not path:
        return replace(
            judgment,
            classification=Classification(
                Outcome.UNKNOWN, 0.0, [], "No PDF path in metadata."
            ),
        )
    try:
        text = head_text(str(judgment.year), path)
    except Exception as err:  # noqa: BLE001
        return replace(
            judgment,
            classification=Classification(
                Outcome.UNKNOWN, 0.0, [], f"Could not read PDF: {err}"
            ),
        )

    # One fetch, three answers: the PDF is expensive to get, so the
    # classification, the holding and the statutes are all taken from it here
    # rather than by reading it again later.
    refs = []
    for name in extract_acts(text):
        ref = canonical(name)
        if ref is not None:
            refs.append(ref)

    return replace(
        judgment,
        classification=classify_supreme_court(text),
        holding=extract_held(text),
        acts=refs,
    )


def classify_all(
    judgments: Iterable[Judgment], workers: int = 8
) -> Iterator[Judgment]:
    """Classify judgments concurrently.

    These are small independent HTTPS fetches, so throughput is bounded by
    latency rather than CPU; a modest pool removes most of the wall time
    without putting meaningful load on a public bucket.
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(classify_one, judgments)
