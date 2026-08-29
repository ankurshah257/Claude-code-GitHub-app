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

#: The digest starts here, per the brief.
DEFAULT_SINCE = "2026-01-01"

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


# --------------------------------------------------------------------------
# The weekly digest build
# --------------------------------------------------------------------------


def build_digest(
    store: Store,
    since: str = DEFAULT_SINCE,
    *,
    courts: tuple[str, ...] = ("bombay", "supreme"),
    workers: int = 8,
    progress: Progress = _noop,
) -> dict[str, int]:
    """Bring the act-wise civil digest up to date.

    Designed to be re-run on a schedule. Partition resume is deliberately *not*
    used: the source exports are living files that gain rows as judgments are
    published, so skipping a bench because it was seen last week would mean
    never seeing anything new. Instead every write is an upsert keyed on the
    judgment id, which makes a repeat run cheap and idempotent rather than
    duplicative.
    """
    added = {"bombay": 0, "supreme": 0}

    if "bombay" in courts:
        for bench in bombay.case_detail_benches():
            count = 0
            for batch in _batched(bombay.scan_cases(bench, since=since), 2000):
                store.add(batch)
                count += store.add_digest(batch)
            added["bombay"] += count
            progress(f"  bombay/{bench}: {count} civil judgments")

    if "supreme" in courts:
        # Only years the window can touch.
        start_year = int(since[:4])
        years = [y for y in supreme.available_years() if int(y) >= start_year]

        # Reading a Supreme Court judgment costs a PDF fetch, so skip the ones
        # already digested rather than paying for them again every week.
        seen = store.digest_uids()

        for year in years:
            fresh = [
                j for j in supreme.scan_year(year)
                if j.uid not in seen and j.decision_date and j.decision_date >= since
            ]
            if not fresh:
                progress(f"  supreme/{year}: nothing new")
                continue

            count = 0
            for batch in _batched(iter(fresh), 100):
                done = list(supreme.classify_all(batch, workers=workers))
                store.add(done)
                count += store.add_digest(done)
                progress(f"  supreme/{year}: {count} civil judgments so far...")
            added["supreme"] += count
            progress(f"  supreme/{year}: {count} civil judgments")

    merged = store.consolidate_acts()
    if merged:
        progress(f"  consolidated {merged} year-less act entries")

    return added


# --------------------------------------------------------------------------
# Generating holdings for the court that publishes none
# --------------------------------------------------------------------------


def generate_holdings(
    store: Store,
    *,
    limit: int | None = None,
    effort: str = "high",
    model: str | None = None,
    workers: int = 4,
    progress: Progress = _noop,
) -> dict[str, object]:
    """Read Bombay judgments and record a generated holding for each.

    Resumable by construction: only rows still lacking a holding are selected,
    so an interrupted run costs nothing to repeat and a weekly run only pays
    for that week's new judgments.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .holding import Provenance
    from .sources import bombay
    from .summarize import Summariser, Usage

    if not model:
        from .summarize import DEFAULT_MODEL

        model = DEFAULT_MODEL

    pending = store.needing_holdings("Bombay High Court", limit=limit)
    if not pending:
        return {"summarised": 0, "no_holding": 0, "failed": 0, "unavailable": 0,
                "usage": Usage(), "model": model}

    index = pdf_locations(progress=progress)

    # Most registry matters have no judgment document in the open corpus, so
    # the reachable set is found before any money is spent rather than by
    # paying for a fetch that turns out to have nothing to read.
    reachable = [r for r in pending if r["uid"] in index]
    unavailable = len(pending) - len(reachable)
    progress(
        f"  {len(reachable):,} of {len(pending):,} have a retrievable judgment PDF"
        f" ({unavailable:,} have none in the corpus)"
    )
    pending = reachable
    if not pending:
        return {"summarised": 0, "no_holding": 0, "failed": 0,
                "unavailable": unavailable, "usage": Usage(), "model": model}

    summariser = Summariser(model=model, effort=effort)
    counts = {"summarised": 0, "no_holding": 0, "failed": 0}
    total = Usage()

    def work(row: object) -> tuple[str, object, object, str]:
        uid, name = row["uid"], row["name"]
        keys = index[uid]
        try:
            text = bombay.judgment_text(keys[-1])
        except Exception as err:  # noqa: BLE001
            return uid, None, Usage(), f"unreadable: {err}"
        try:
            result = summariser.summarise(text, title=name)
        except Exception as err:  # noqa: BLE001
            return uid, None, Usage(), f"summarise failed: {err}"
        return uid, result, result.usage, ""

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (uid, result, usage, error) in enumerate(pool.map(work, pending), 1):
            total = total + usage
            if error or result is None:
                counts["failed"] += 1
            elif not result.states_a_holding:
                # Recorded, not discarded: "this order decides nothing" is a
                # useful answer, and storing it stops the next run re-reading
                # the same document to reach it again.
                store.set_holding(uid, result.holding.text, Provenance.GENERATED.value)
                counts["no_holding"] += 1
            else:
                store.set_holding(uid, result.holding.text, Provenance.GENERATED.value)
                counts["summarised"] += 1

            if i % 25 == 0:
                progress(
                    f"  {i}/{len(pending)} read, ${total.cost(model):.2f} spent so far"
                )

    return {**counts, "unavailable": unavailable, "usage": total, "model": model}


def pdf_locations(
    years: tuple[str, ...] = ("2026",), progress: Progress = _noop
) -> dict[str, list[str]]:
    """Index every Bombay judgment PDF available for the given decision years.

    Listing costs roughly one request per thousand objects, so this is built
    once and shared across a whole run.
    """
    from .sources import bombay

    progress("  indexing available judgment PDFs...")
    index: dict[str, list[str]] = {}
    for bench in bombay.case_detail_benches():
        for cnr, keys in bombay.pdf_index(bench, list(years)).items():
            index.setdefault(cnr, []).extend(keys)
    progress(f"  {len(index):,} judgments have a PDF in the corpus")
    return index
