"""The scan pipeline: enumerate partitions, classify, persist.

Partition granularity differs by court because the corpora differ. A Bombay
partition is one bench-year, which is a single Parquet download and is cheap to
redo. A Supreme Court partition is a year, but classifying it means fetching one
PDF per judgment, so progress is committed in batches inside the year rather
than only at its end — an interrupted 1990 scan should not have to re-download
several hundred PDFs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

from .sources import bombay, supreme
from .sources.bombay import Judgment
from .store import Store

Progress = Callable[[str], None]


@dataclass(frozen=True)
class ScanResult:
    partitions: int
    written: int
    skipped: int


def _noop(_: str) -> None:
    return None


def _batched(items: Iterator[Judgment], size: int) -> Iterator[list[Judgment]]:
    batch: list[Judgment] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def scan_bombay(
    store: Store,
    years: list[str],
    *,
    resume: bool = True,
    mobile: bool = True,
    progress: Progress = _noop,
) -> ScanResult:
    """Scan Bombay High Court judgments for the given years."""
    partitions = written = skipped = 0

    for year in years:
        try:
            benches = bombay.available_benches(year)
        except Exception as err:  # noqa: BLE001
            progress(f"  {year}: could not list benches ({err})")
            continue

        for bench in benches:
            key = f"bombay/{year}/{bench}"
            if resume and store.is_done(key):
                skipped += 1
                continue

            rows = 0
            for batch in _batched(bombay.scan_bench(year, bench, mobile=mobile), 5000):
                rows += store.add(batch)
            store.mark_done(key, rows)
            partitions += 1
            written += rows
            progress(f"  {key}: {rows} documents")

    return ScanResult(partitions, written, skipped)


def scan_supreme(
    store: Store,
    years: list[str],
    *,
    resume: bool = True,
    workers: int = 8,
    classify: bool = True,
    limit_per_year: int | None = None,
    progress: Progress = _noop,
) -> ScanResult:
    """Scan Supreme Court judgments for the given years.

    With ``classify=False`` only the index is built — titles, dates and
    citations, with every outcome UNKNOWN — which is fast because no PDF is
    fetched. That is the right mode for surveying what exists before committing
    to the expensive pass.
    """
    partitions = written = skipped = 0

    for year in years:
        key = f"supreme/{year}" + ("" if classify else "/index")
        if resume and store.is_done(key):
            skipped += 1
            continue

        records: Iterator[Judgment] = supreme.scan_year(year)
        if limit_per_year:
            records = (j for _, j in zip(range(limit_per_year), records))

        rows = 0
        if classify:
            # Batch inside the year so an interruption loses at most one batch
            # of PDF fetches rather than the whole year.
            for batch in _batched(records, 200):
                rows += store.add(supreme.classify_all(batch, workers=workers))
                progress(f"  {key}: {rows} classified...")
        else:
            for batch in _batched(records, 2000):
                rows += store.add(batch)

        store.mark_done(key, rows)
        partitions += 1
        written += rows
        progress(f"  {key}: {rows} judgments")

    return ScanResult(partitions, written, skipped)
