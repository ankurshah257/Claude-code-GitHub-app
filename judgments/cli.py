"""Command line interface.

    python -m judgments scan bombay --from 2020 --to 2024
    python -m judgments scan supreme --from 2023 --to 2024
    python -m judgments stats
    python -m judgments civil --final --limit 20
    python -m judgments review
"""

from __future__ import annotations

import argparse
import csv
import sys

from .sources import bombay, supreme
from .store import Store

DEFAULT_DB = "judgments.db"


def _years(args: argparse.Namespace, available: list[str]) -> list[str]:
    lo = args.year_from or int(available[0])
    hi = args.year_to or int(available[-1])
    chosen = [y for y in available if lo <= int(y) <= hi]
    if not chosen:
        sys.exit(
            f"No years in range {lo}-{hi}. Corpus covers {available[0]}-{available[-1]}."
        )
    return chosen


def cmd_scan(args: argparse.Namespace) -> None:
    say = (lambda m: None) if args.quiet else (lambda m: print(m, flush=True))

    with Store(args.db) as store:
        # Imported here so the module is usable without the scan pipeline.
        from .scan import scan_bombay, scan_supreme

        if args.court == "bombay":
            years = _years(args, bombay.available_years())
            say(f"Scanning Bombay High Court, {years[0]}-{years[-1]}")
            result = scan_bombay(
                store, years, resume=not args.no_resume, progress=say
            )
        else:
            years = _years(args, supreme.available_years())
            say(f"Scanning Supreme Court, {years[0]}-{years[-1]}")
            if not args.index_only:
                say("  (fetching one PDF per judgment; --index-only skips this)")
            result = scan_supreme(
                store,
                years,
                resume=not args.no_resume,
                workers=args.workers,
                classify=not args.index_only,
                limit_per_year=args.limit,
                progress=say,
            )

        say(
            f"\n{result.written} documents written across {result.partitions} "
            f"partitions ({result.skipped} already done)."
        )
        _print_stats(store)


def _print_stats(store: Store) -> None:
    for court in (None, "Bombay High Court", "Supreme Court of India"):
        counts = store.counts(court)
        if counts.total == 0:
            continue
        label = court or "ALL"
        pct = (100 * counts.civil / counts.total) if counts.total else 0
        print(f"\n{label}: {counts.total:,} documents")
        print(f"  civil     {counts.civil:>8,}  ({pct:.1f}%)")
        print(f"  criminal  {counts.criminal:>8,}")
        if counts.unknown:
            print(f"  unknown   {counts.unknown:>8,}  (declined to classify)")
        if counts.disputed:
            print(f"  disputed  {counts.disputed:>8,}  (signals conflict)")
        print(f"  final judgments {counts.final:,} (rest are interim orders)")


def cmd_stats(args: argparse.Namespace) -> None:
    with Store(args.db) as store:
        if store.counts().total == 0:
            sys.exit(f"{args.db} is empty. Run a scan first.")
        _print_stats(store)


def cmd_civil(args: argparse.Namespace) -> None:
    with Store(args.db) as store:
        rows = store.civil(
            court=args.court,
            final_only=args.final,
            year_from=args.year_from,
            year_to=args.year_to,
            limit=args.limit,
        )
        if args.csv:
            writer = csv.writer(sys.stdout)
            writer.writerow(
                ["uid", "court", "bench", "case_type", "case_no", "date", "title", "url"]
            )
            for r in rows:
                writer.writerow(
                    [r["uid"], r["court"], r["bench"], r["case_type"], r["case_no"],
                     r["decision_date"], r["title"], r["url"]]
                )
            return

        found = False
        for r in rows:
            found = True
            flag = "" if r["is_final"] else "  [interim]"
            print(f"{r['decision_date']}  {r['case_type'] or '-':<7} {r['case_no'] or '':<22}{flag}")
            print(f"    {r['title'][:96]}")
        if not found:
            print("No civil judgments matched.")


def cmd_digest(args: argparse.Namespace) -> None:
    """Build or refresh the act-wise civil digest. This is the weekly job."""
    say = (lambda m: None) if args.quiet else (lambda m: print(m, flush=True))
    from .scan import build_digest

    courts = ("bombay", "supreme") if args.court == "both" else (args.court,)
    with Store(args.db) as store:
        say(f"Building digest from {args.since} for: {', '.join(courts)}")
        added = build_digest(
            store, since=args.since, courts=courts,
            workers=args.workers, progress=say,
        )
        counts = store.digest_counts()
        say(f"\nAdded this run: {added}")
        print(
            f"Digest: {counts['judgments']:,} civil judgments, "
            f"{counts['acts']:,} distinct acts, "
            f"{counts['with_holding']:,} with a verbatim holding."
        )


def cmd_acts(args: argparse.Namespace) -> None:
    """Browse the digest act-wise."""
    with Store(args.db) as store:
        if not args.act:
            rows = store.act_index(since=args.since)
            if not rows:
                sys.exit("Digest is empty. Run: python -m judgments digest")
            print(f"{'judgments':>9}  act")
            for r in rows[: args.limit]:
                print(f"{r['n']:>9,}  {r['act_label']}")
            print(f"\n{len(rows):,} acts. Use --act \"<name>\" to list judgments.")
            return

        # Match on the display label so a user can pass what they just read.
        matches = [
            r for r in store.act_index(since=args.since)
            if args.act.lower() in r["act_label"].lower()
        ]
        if not matches:
            sys.exit(f"No act matching {args.act!r}.")
        if len(matches) > 1 and not args.exact:
            print("Several acts match; narrow with a fuller name:")
            for r in matches[:10]:
                print(f"  {r['n']:>7,}  {r['act_label']}")
            return

        target = matches[0]
        print(f"=== {target['act_label']} ({target['n']:,} judgments) ===\n")
        for r in store.by_act(target["act_key"], since=args.since, limit=args.limit):
            section = f" [s. {r['section']}]" if r["section"] else ""
            print(f"{r['date']}  {r['court']}{section}")
            print(f"  {r['name'][:100]}")
            if r["held"]:
                mark = "held" if r["held_source"] == "headnote" else "summary"
                print(f"  {mark}: {r['held'][:280]}")
            print()


#: Judgments above which a run must be confirmed with --yes. A full backfill
#: costs over a thousand dollars, which is not something a mistyped command
#: should be able to start.
CONFIRM_ABOVE = 200


def cmd_summarize(args: argparse.Namespace) -> None:
    """Generate holdings for Bombay judgments, which have no headnotes."""
    from .scan import generate_holdings
    from .summarize import Usage, credentials_available

    say = (lambda m: None) if args.quiet else (lambda m: print(m, flush=True))

    with Store(args.db) as store:
        pending = store.needing_holdings("Bombay High Court", limit=args.limit)
        if not pending:
            print("Every Bombay judgment in the digest already has a holding.")
            return

        # Only judgments whose PDF is actually in the corpus can be read, and
        # that is a small fraction. Establishing it first keeps the estimate
        # honest instead of quoting a price for work that cannot happen.
        from .scan import pdf_locations

        index = pdf_locations(progress=say)
        reachable = [r for r in pending if r["uid"] in index]
        n = len(reachable)
        print(f"{len(pending):,} judgments lack a holding; "
              f"{n:,} have a retrievable PDF ({len(pending)-n:,} have none).")
        if n == 0:
            print("Nothing can be summarised: no source documents available.")
            return

        # Priced from a measured average: ~5k input and ~300 output tokens on a
        # 35 KB judgment. Real cost is reported from actual usage afterwards.
        estimate = Usage(5000 * n, 300 * n, 0).cost()
        print(f"Estimated cost: ${estimate:,.2f} (~${estimate/n:.4f} each, model {args.model or 'claude-opus-5'})")

        if args.estimate:
            return
        if n > CONFIRM_ABOVE and not args.yes:
            sys.exit(
                f"\nThis run would summarise {n:,} judgments and spend real money.\n"
                f"Re-run with --yes to proceed, or --limit N to do fewer first."
            )
        if not credentials_available():
            sys.exit(
                "No Anthropic credentials found. Set ANTHROPIC_API_KEY, or run "
                "`ant auth login`."
            )

        result = generate_holdings(
            store, limit=args.limit, effort=args.effort,
            model=args.model, workers=args.workers, progress=say,
        )
        usage = result["usage"]
        print(
            f"\nSummarised {result['summarised']:,} | "
            f"decided nothing {result['no_holding']:,} | failed {result['failed']:,} | "
            f"no PDF available {result.get('unavailable', 0):,}"
        )
        print(f"Actual cost: ${usage.cost(str(result['model'])):,.2f} "
              f"({usage.input_tokens:,} in / {usage.output_tokens:,} out)")
        for r in store.holding_breakdown():
            print(f"  {r['court']:26s} {r['held_source']:10s} {r['n']:,}")


def cmd_review(args: argparse.Namespace) -> None:
    """Show what the system refused to classify."""
    with Store(args.db) as store:
        found = False
        for r in store.needs_review(limit=args.limit):
            found = True
            print(f"[{r['outcome']}] {r['court']} {r['case_type'] or ''} {r['case_no'] or ''}")
            print(f"    {r['title'][:88]}")
            print(f"    {r['rationale']}")
        if not found:
            print("Nothing needs review.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="judgments",
        description="Scan Bombay High Court and Supreme Court judgments for civil matters.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan a court into the database")
    scan.add_argument("court", choices=["bombay", "supreme"])
    scan.add_argument("--from", dest="year_from", type=int, help="first year")
    scan.add_argument("--to", dest="year_to", type=int, help="last year")
    scan.add_argument("--workers", type=int, default=8, help="concurrent PDF fetches (supreme)")
    scan.add_argument("--index-only", action="store_true",
                      help="supreme: build the index without fetching PDFs")
    scan.add_argument("--limit", type=int, help="supreme: cap judgments per year")
    scan.add_argument("--no-resume", action="store_true", help="re-scan completed partitions")
    scan.add_argument("--quiet", action="store_true")
    scan.set_defaults(func=cmd_scan)

    stats = sub.add_parser("stats", help="summarise the database")
    stats.set_defaults(func=cmd_stats)

    civil = sub.add_parser("civil", help="list civil judgments")
    civil.add_argument("--court")
    civil.add_argument("--final", action="store_true", help="final judgments only, no interim orders")
    civil.add_argument("--from", dest="year_from", type=int)
    civil.add_argument("--to", dest="year_to", type=int)
    civil.add_argument("--limit", type=int, default=50)
    civil.add_argument("--csv", action="store_true")
    civil.set_defaults(func=cmd_civil)

    digest = sub.add_parser(
        "digest", help="build/refresh the act-wise civil digest (the weekly job)"
    )
    digest.add_argument("--since", default="2026-01-01", help="ISO start date")
    digest.add_argument("--court", choices=["both", "bombay", "supreme"], default="both")
    digest.add_argument("--workers", type=int, default=8)
    digest.add_argument("--quiet", action="store_true")
    digest.set_defaults(func=cmd_digest)

    acts = sub.add_parser("acts", help="browse the digest act-wise")
    acts.add_argument("--act", help="show judgments under this act")
    acts.add_argument("--since", help="ISO start date")
    acts.add_argument("--exact", action="store_true", help="take the first match")
    acts.add_argument("--limit", type=int, default=40)
    acts.set_defaults(func=cmd_acts)

    summ = sub.add_parser(
        "summarize",
        help="generate holdings for Bombay judgments (costs money; see --estimate)",
    )
    summ.add_argument("--limit", type=int, help="only this many, newest first")
    summ.add_argument("--effort", default="high",
                      choices=["low", "medium", "high", "xhigh", "max"])
    summ.add_argument("--model", help="default claude-opus-5")
    summ.add_argument("--workers", type=int, default=4)
    summ.add_argument("--estimate", action="store_true", help="price it, do not run")
    summ.add_argument("--yes", action="store_true", help="confirm a large run")
    summ.add_argument("--quiet", action="store_true")
    summ.set_defaults(func=cmd_summarize)

    review = sub.add_parser("review", help="show judgments the system declined to classify")
    review.add_argument("--limit", type=int, default=50)
    review.set_defaults(func=cmd_review)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
