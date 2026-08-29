"""SQLite store for scanned judgments.

A full scan is long enough that it will be interrupted, so the store is built
around resumability: partitions are marked complete only after their rows are
committed, and re-running a scan skips what is already done. Writes are
idempotent on ``uid``, so a partition re-processed after a crash updates rows
rather than duplicating them.

SQLite rather than a server database because the output is a single portable
file that someone can copy, query with any tool, and archive alongside a
citation — which is what legal research actually needs.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .classify import Outcome
from .sources.bombay import Judgment

SCHEMA = """
CREATE TABLE IF NOT EXISTS judgments (
    uid            TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL,
    court          TEXT NOT NULL,
    bench          TEXT,
    year           INTEGER NOT NULL,
    case_type      TEXT,
    case_no        TEXT,
    title          TEXT,
    decision_date  TEXT,
    is_final       INTEGER NOT NULL DEFAULT 0,
    outcome        TEXT NOT NULL,
    confidence     REAL NOT NULL,
    rationale      TEXT,
    signals        TEXT,          -- JSON, so a classification stays auditable
    petitioner     TEXT,
    respondent     TEXT,
    judge          TEXT,
    disposal       TEXT,
    url            TEXT,
    extra          TEXT           -- JSON
);

CREATE INDEX IF NOT EXISTS idx_outcome ON judgments(outcome);
CREATE INDEX IF NOT EXISTS idx_court_year ON judgments(court, year);
CREATE INDEX IF NOT EXISTS idx_date ON judgments(decision_date);
CREATE INDEX IF NOT EXISTS idx_case ON judgments(case_id);

-- The database the digest actually exposes: exactly what was asked for -- the
-- name of the judgment, the court that passed it, the date of passing, and
-- what was held. Kept separate from the scan table above so that the answer to
-- "what is in the database" is a short, readable schema rather than a
-- thirty-column scan record.
CREATE TABLE IF NOT EXISTS digest (
    uid        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,   -- cause title, e.g. "A v. B"
    court      TEXT NOT NULL,   -- the court that passed it
    date       TEXT NOT NULL,   -- date of passing (ISO)
    held       TEXT,            -- what is held
    -- Provenance of `held`: "headnote" is the court's own words and is
    -- citable; "none" means no headnote exists for this court. Recorded so a
    -- quotation is never mistaken for a summary.
    held_source TEXT NOT NULL DEFAULT 'none'
);

CREATE INDEX IF NOT EXISTS idx_digest_date ON digest(date);
CREATE INDEX IF NOT EXISTS idx_digest_court ON digest(court);

-- Act-wise index. Many-to-many: a judgment may turn on several statutes, and
-- the same statute is applied across many judgments.
CREATE TABLE IF NOT EXISTS digest_acts (
    uid       TEXT NOT NULL,
    act_key   TEXT NOT NULL,    -- canonical "name|year", the grouping key
    act_label TEXT NOT NULL,    -- display form
    section   TEXT,
    PRIMARY KEY (uid, act_key)
);

CREATE INDEX IF NOT EXISTS idx_acts_key ON digest_acts(act_key);

-- One row per completed partition, so an interrupted scan resumes instead of
-- re-downloading everything it already processed.
CREATE TABLE IF NOT EXISTS partitions (
    key        TEXT PRIMARY KEY,
    rows       INTEGER NOT NULL,
    finished_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Counts:
    total: int
    civil: int
    criminal: int
    unknown: int
    disputed: int
    final: int


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps reads working while a long scan is still writing.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def is_done(self, key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM partitions WHERE key = ?", (key,)
        ).fetchone()
        return row is not None

    def mark_done(self, key: str, rows: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO partitions(key, rows, finished_at) "
            "VALUES (?, ?, datetime('now'))",
            (key, rows),
        )
        self._conn.commit()

    def add(self, judgments: Iterable[Judgment]) -> int:
        """Insert or update judgments. Returns the number written."""
        rows = []
        for j in judgments:
            c = j.classification
            rows.append(
                (
                    j.uid,
                    j.case_id,
                    j.court,
                    j.bench,
                    j.year,
                    j.case_type,
                    j.case_no,
                    j.title,
                    j.decision_date,
                    int(j.is_final),
                    c.outcome.value,
                    c.confidence,
                    c.rationale,
                    json.dumps(
                        [
                            {"source": s.source, "raw": s.raw, "says": s.says.value,
                             "weight": s.weight}
                            for s in c.signals
                        ]
                    ),
                    j.petitioner,
                    j.respondent,
                    j.judge,
                    j.disposal,
                    j.url,
                    json.dumps(j.extra),
                )
            )
        if not rows:
            return 0

        with closing(self._conn.cursor()) as cur:
            cur.executemany(
                "INSERT OR REPLACE INTO judgments VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        self._conn.commit()
        return len(rows)

    def add_digest(self, judgments: Iterable[Judgment]) -> int:
        """Record civil judgments in the digest and index them by act.

        Only settled-civil judgments are admitted. DISPUTED and UNKNOWN are the
        scan's business, not the digest's: this table is meant to be relied on.
        """
        rows = []
        act_rows = []
        for j in judgments:
            if not j.classification.is_civil:
                continue
            rows.append(
                (j.uid, j.title, j.court, j.decision_date,
                 j.holding.text, j.holding.provenance.value)
            )
            for ref in j.acts:
                act_rows.append((j.uid, ref.key, ref.label, ref.section))

        if rows:
            with closing(self._conn.cursor()) as cur:
                cur.executemany(
                    "INSERT OR REPLACE INTO digest VALUES (?,?,?,?,?,?)", rows
                )
                if act_rows:
                    cur.executemany(
                        "INSERT OR REPLACE INTO digest_acts VALUES (?,?,?,?)", act_rows
                    )
            self._conn.commit()
        return len(rows)

    def consolidate_acts(self) -> int:
        """Merge year-less act entries into their dated counterparts.

        This has to run over the finished table, not per record: deciding that
        "Indian Succession Act" means the 1925 Act requires seeing every year
        the corpus ever pairs that name with. Done per record, a name would
        stay split from its own dated form -- which is exactly what happened
        before this pass existed, leaving the Code of Civil Procedure as two
        separate entries of 7,883 and 3,789 judgments.

        Names carrying more than one year (Companies Act 1956 and 2013) are
        left alone: those are different statutes and merging them would pool
        judgments under an enactment that was not applied.
        """
        from .acts import ActRef, resolve_years

        rows = list(self._conn.execute(
            "SELECT DISTINCT act_key, act_label FROM digest_acts"
        ))
        refs = []
        for r in rows:
            name, _, year = r["act_key"].rpartition("|")
            refs.append(ActRef(name, int(year) if year else None, r["act_label"]))

        resolution = resolve_years(refs)

        updates = []
        for ref in refs:
            if ref.year is not None or ref.name not in resolution:
                continue
            year = resolution[ref.name]
            target = f"{ref.name}|{year}"
            label = next(
                (x.label for x in refs if x.key == target), f"{ref.label}, {year}"
            )
            updates.append((target, label, ref.key))

        if updates:
            with closing(self._conn.cursor()) as cur:
                # OR REPLACE: a judgment may already be indexed under the dated
                # key, in which case the merge collapses the duplicate pair.
                cur.executemany(
                    "UPDATE OR REPLACE digest_acts SET act_key=?, act_label=? "
                    "WHERE act_key=?",
                    updates,
                )
            self._conn.commit()
        return len(updates)

    def needing_holdings(self, court: str, limit: int | None = None) -> list[sqlite3.Row]:
        """Digest rows for a court that have no holding yet.

        Ordered newest-first so a capped run summarises the most recent
        judgments, which are the ones a researcher is most likely to want.
        """
        sql = (
            "SELECT uid, name, date FROM digest "
            "WHERE court = ? AND held_source = 'none' ORDER BY date DESC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self._conn.execute(sql, (court,)))

    def set_holding(self, uid: str, text: str, source: str) -> None:
        """Attach a holding to a digest row."""
        self._conn.execute(
            "UPDATE digest SET held = ?, held_source = ? WHERE uid = ?",
            (text, source, uid),
        )
        self._conn.commit()

    def holding_breakdown(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT court, held_source, COUNT(*) n FROM digest "
            "GROUP BY court, held_source ORDER BY court, held_source"
        ))

    def digest_uids(self) -> set[str]:
        """Ids already in the digest.

        The weekly run uses this to avoid re-fetching a Supreme Court PDF it
        has already read: the corpus grows, but what is already digested does
        not change.
        """
        return {r[0] for r in self._conn.execute("SELECT uid FROM digest")}

    def act_index(self, since: str | None = None) -> list[sqlite3.Row]:
        """Acts represented in the digest, most-litigated first."""
        where = "WHERE d.date >= ?" if since else ""
        params = (since,) if since else ()
        return list(self._conn.execute(
            f"""SELECT a.act_key, a.act_label, COUNT(DISTINCT a.uid) AS n
                FROM digest_acts a JOIN digest d ON d.uid = a.uid
                {where}
                GROUP BY a.act_key, a.act_label
                ORDER BY n DESC, a.act_label""",
            params,
        ))

    def by_act(
        self, act_key: str, since: str | None = None, limit: int | None = None
    ) -> list[sqlite3.Row]:
        """Judgments decided under one act."""
        sql = """SELECT d.name, d.court, d.date, d.held, d.held_source, a.section
                 FROM digest d JOIN digest_acts a ON d.uid = a.uid
                 WHERE a.act_key = ?"""
        params: list[object] = [act_key]
        if since:
            sql += " AND d.date >= ?"
            params.append(since)
        sql += " ORDER BY d.date DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self._conn.execute(sql, params))

    def digest_counts(self) -> dict[str, int]:
        row = self._conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(held_source='headnote') AS with_held,
                      (SELECT COUNT(DISTINCT act_key) FROM digest_acts) AS acts
               FROM digest"""
        ).fetchone()
        return {
            "judgments": row["n"] or 0,
            "with_holding": row["with_held"] or 0,
            "acts": row["acts"] or 0,
        }

    def counts(self, court: str | None = None) -> Counts:
        where, params = ("WHERE court = ?", (court,)) if court else ("", ())
        row = self._conn.execute(
            f"""SELECT COUNT(*) AS total,
                   SUM(outcome='civil')    AS civil,
                   SUM(outcome='criminal') AS criminal,
                   SUM(outcome='unknown')  AS unknown,
                   SUM(outcome='disputed') AS disputed,
                   SUM(is_final)           AS final
                FROM judgments {where}""",
            params,
        ).fetchone()
        return Counts(
            total=row["total"] or 0,
            civil=row["civil"] or 0,
            criminal=row["criminal"] or 0,
            unknown=row["unknown"] or 0,
            disputed=row["disputed"] or 0,
            final=row["final"] or 0,
        )

    def civil(
        self,
        *,
        court: str | None = None,
        final_only: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int | None = None,
    ) -> Iterator[sqlite3.Row]:
        """Query settled-civil judgments.

        DISPUTED and UNKNOWN are excluded here by construction — this is the
        precision path. Use :meth:`needs_review` to see what was set aside.
        """
        clauses = ["outcome = 'civil'"]
        params: list[object] = []
        if court:
            clauses.append("court = ?")
            params.append(court)
        if final_only:
            clauses.append("is_final = 1")
        if year_from is not None:
            clauses.append("year >= ?")
            params.append(year_from)
        if year_to is not None:
            clauses.append("year <= ?")
            params.append(year_to)

        sql = (
            f"SELECT * FROM judgments WHERE {' AND '.join(clauses)} "
            "ORDER BY decision_date DESC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        yield from self._conn.execute(sql, params)

    def needs_review(self, limit: int = 100) -> Iterator[sqlite3.Row]:
        """Judgments the system declined to classify, worst-first.

        Surfacing these is the point: a scan that silently dropped them would
        look cleaner and be less trustworthy.
        """
        yield from self._conn.execute(
            "SELECT * FROM judgments WHERE outcome IN ('disputed','unknown') "
            "ORDER BY outcome, decision_date DESC LIMIT ?",
            (limit,),
        )
