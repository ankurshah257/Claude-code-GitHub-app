"""Tests for the store and its resume behaviour. No network."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from judgments.classify import Outcome, classify_high_court
from judgments.sources.bombay import Judgment
from judgments.store import Store


def make(uid: str, section: str = "Civil", case_type: str = "FA", **kw) -> Judgment:
    defaults = dict(
        case_id=uid.split(":")[0],
        court="Bombay High Court",
        bench="Principal Seat, Bombay (Appellate Side)",
        case_type=case_type,
        case_no="FA/1/2024",
        title="A versus B",
        decision_date="2024-05-01",
        classification=classify_high_court(section, case_type),
        year=2024,
        is_final=True,
    )
    defaults.update(kw)
    return Judgment(uid=uid, **defaults)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.db"

    def tearDown(self):
        self.dir.cleanup()

    def test_add_and_count(self):
        with Store(self.path) as s:
            s.add([make("a:1"), make("b:1", "Criminal", "ABA")])
            c = s.counts()
            self.assertEqual((c.total, c.civil, c.criminal), (2, 1, 1))

    def test_writes_are_idempotent_on_uid(self):
        # A partition reprocessed after a crash must update, not duplicate.
        with Store(self.path) as s:
            s.add([make("a:1")])
            s.add([make("a:1")])
            self.assertEqual(s.counts().total, 1)

    def test_order_number_distinguishes_documents_of_one_case(self):
        # cnr alone is a *case* id; a case averages ~3.4 orders.
        with Store(self.path) as s:
            s.add([make("HCBM01:1"), make("HCBM01:2"), make("HCBM01:3")])
            self.assertEqual(s.counts().total, 3)

    def test_resume_marks_partitions(self):
        with Store(self.path) as s:
            self.assertFalse(s.is_done("bombay/2024/newas"))
            s.mark_done("bombay/2024/newas", 10)
            self.assertTrue(s.is_done("bombay/2024/newas"))

    def test_resume_state_survives_reopen(self):
        with Store(self.path) as s:
            s.add([make("a:1")])
            s.mark_done("p", 1)
        with Store(self.path) as s:
            self.assertTrue(s.is_done("p"))
            self.assertEqual(s.counts().total, 1)

    def test_civil_query_excludes_disputed_and_unknown(self):
        with Store(self.path) as s:
            s.add([
                make("civil:1"),
                make("disputed:1", "Civil", "BA"),      # conflicting signals
                make("unknown:1", "False", "ZZZ"),      # junk section
            ])
            uids = {r["uid"] for r in s.civil()}
            self.assertEqual(uids, {"civil:1"})

    def test_needs_review_surfaces_what_was_set_aside(self):
        with Store(self.path) as s:
            s.add([make("civil:1"), make("disputed:1", "Civil", "BA")])
            outcomes = {r["outcome"] for r in s.needs_review()}
            self.assertEqual(outcomes, {"disputed"})

    def test_final_only_filter(self):
        with Store(self.path) as s:
            s.add([make("f:1", is_final=True), make("i:1", is_final=False)])
            self.assertEqual({r["uid"] for r in s.civil(final_only=True)}, {"f:1"})
            self.assertEqual(len(list(s.civil())), 2)

    def test_year_range_filter(self):
        with Store(self.path) as s:
            s.add([make("a:1", year=2020), make("b:1", year=2024)])
            got = {r["uid"] for r in s.civil(year_from=2022)}
            self.assertEqual(got, {"b:1"})

    def test_signals_are_persisted_for_audit(self):
        with Store(self.path) as s:
            s.add([make("a:1")])
            row = next(iter(s.civil()))
            self.assertIn("judicial_section", row["signals"])


if __name__ == "__main__":
    unittest.main()
