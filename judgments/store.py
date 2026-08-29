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
