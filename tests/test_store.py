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



class TestDigest(unittest.TestCase):
    """The lean act-wise database: name, court, date, held."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "d.db"

    def tearDown(self):
        self.dir.cleanup()

    def _judgment(self, uid, acts, section="Civil", held=""):
        from judgments.acts import canonical
        from judgments.holding import Holding, Provenance
        from dataclasses import replace
        j = make(uid, section)
        refs = [canonical(a) for a in acts]
        return replace(
            j,
            acts=[r for r in refs if r],
            holding=Holding(held, Provenance.HEADNOTE if held else Provenance.NONE),
        )

    def test_only_civil_enters_the_digest(self):
        with Store(self.path) as s:
            s.add_digest([
                self._judgment("civil:1", ["Companies Act 2013"]),
                self._judgment("crim:1", ["Companies Act 2013"], section="Criminal"),
            ])
            self.assertEqual(s.digest_counts()["judgments"], 1)

    def test_act_index_counts_judgments(self):
        with Store(self.path) as s:
            s.add_digest([
                self._judgment("a:1", ["Companies Act 2013"]),
                self._judgment("b:1", ["Companies Act 2013"]),
                self._judgment("c:1", ["Indian Succession Act, 1925"]),
            ])
            index = {r["act_label"]: r["n"] for r in s.act_index()}
            self.assertEqual(index["Companies Act, 2013"], 2)
            self.assertEqual(index["Indian Succession Act, 1925"], 1)

    def test_one_judgment_can_sit_under_several_acts(self):
        with Store(self.path) as s:
            s.add_digest([self._judgment("a:1", ["Companies Act 2013", "Constitution of India"])])
            self.assertEqual(len(s.act_index()), 2)

    def test_consolidate_merges_yearless_into_dated(self):
        with Store(self.path) as s:
            s.add_digest([
                self._judgment("a:1", ["Indian Succession Act, 1925"]),
                self._judgment("b:1", ["Indian Succession Act"]),
            ])
            self.assertEqual(len(s.act_index()), 2)   # split before
            s.consolidate_acts()
            index = s.act_index()
            self.assertEqual(len(index), 1)           # merged after
            self.assertEqual(index[0]["n"], 2)

    def test_consolidate_leaves_ambiguous_names_split(self):
        # Two years for one name: merging would misfile under a statute that
        # was not applied.
        with Store(self.path) as s:
            s.add_digest([
                self._judgment("a:1", ["Companies Act 1956"]),
                self._judgment("b:1", ["Companies Act 2013"]),
                self._judgment("c:1", ["Companies Act"]),
            ])
            s.consolidate_acts()
            self.assertEqual(len(s.act_index()), 3)

    def test_by_act_returns_the_four_requested_fields(self):
        with Store(self.path) as s:
            s.add_digest([self._judgment("a:1", ["Companies Act 2013"], held="X is held.")])
            row = s.by_act("companies act|2013")[0]
            self.assertEqual(row["name"], "A versus B")
            self.assertEqual(row["court"], "Bombay High Court")
            self.assertEqual(row["date"], "2024-05-01")
            self.assertEqual(row["held"], "X is held.")
            self.assertEqual(row["held_source"], "headnote")

    def test_holding_provenance_is_recorded(self):
        with Store(self.path) as s:
            s.add_digest([self._judgment("a:1", ["Companies Act 2013"])])
            self.assertEqual(s.by_act("companies act|2013")[0]["held_source"], "none")

    def test_digest_is_idempotent(self):
        with Store(self.path) as s:
            for _ in range(3):
                s.add_digest([self._judgment("a:1", ["Companies Act 2013"])])
            self.assertEqual(s.digest_counts()["judgments"], 1)
            self.assertEqual(len(s.act_index()), 1)

    def test_since_filter(self):
        with Store(self.path) as s:
            s.add_digest([self._judgment("a:1", ["Companies Act 2013"])])
            self.assertEqual(len(s.act_index(since="2020-01-01")), 1)
            self.assertEqual(len(s.act_index(since="2030-01-01")), 0)


class TestHoldingsPersistence(unittest.TestCase):
    """Holdings must outlive the database: they cost money, the digest does not."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "p.db"

    def tearDown(self):
        self.dir.cleanup()

    def _seed(self, store):
        from dataclasses import replace
        from judgments.acts import canonical
        from judgments.holding import Holding, Provenance
        j = make("a:1")
        store.add_digest([replace(
            j, acts=[canonical("Companies Act 2013")],
            holding=Holding("The appeal is allowed.", Provenance.GENERATED),
        )])

    def test_round_trip_restores_a_lost_holding(self):
        with Store(self.path) as s:
            self._seed(s)
            exported = s.export_holdings()
            # Simulate the digest being rebuilt from scratch.
            s._conn.execute("UPDATE digest SET held='', held_source='none'")
            s._conn.commit()
            self.assertEqual(s.digest_counts()["with_holding"], 0)
            self.assertEqual(s.import_holdings(exported), 1)
            row = s.by_act("companies act|2013")[0]
            self.assertEqual(row["held"], "The appeal is allowed.")
            self.assertEqual(row["held_source"], "generated")

    def test_export_skips_rows_without_a_holding(self):
        with Store(self.path) as s:
            self._seed(s)
            s.add_digest([make("b:1")])          # no holding
            self.assertEqual([r[0] for r in s.export_holdings()], ["a:1"])

    def test_import_never_overwrites_an_existing_holding(self):
        # A restore must not clobber a newer holding with a stale one.
        with Store(self.path) as s:
            self._seed(s)
            s.import_holdings([("a:1", "STALE", "generated")])
            self.assertEqual(s.by_act("companies act|2013")[0]["held"],
                             "The appeal is allowed.")

    def test_import_of_nothing_is_harmless(self):
        with Store(self.path) as s:
            self._seed(s)
            self.assertEqual(s.import_holdings([]), 0)


class TestPendingSelection(unittest.TestCase):
    """The holdings cap must be applied after reachability, not before."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "q.db"

    def tearDown(self):
        self.dir.cleanup()

    def test_unlimited_query_returns_every_pending_row(self):
        # generate_holdings needs the full set so it can filter to judgments
        # whose text is retrievable and only then apply the cap. Capping the
        # query first made a --limit 20 run draw 20 rows of which none had a
        # document, and summarise nothing.
        with Store(self.path) as s:
            for i in range(30):
                s.add_digest([make(f"u{i}:1")])
            self.assertEqual(len(s.needing_holdings("Bombay High Court")), 30)
            self.assertEqual(len(s.needing_holdings("Bombay High Court", limit=5)), 5)

    def test_pending_excludes_rows_that_already_have_a_holding(self):
        from dataclasses import replace
        from judgments.holding import Holding, Provenance
        with Store(self.path) as s:
            s.add_digest([make("done:1")])
            s.add_digest([replace(make("todo:1"),
                                  holding=Holding("", Provenance.NONE))])
            s.set_holding("done:1", "Already held.", "generated")
            uids = {r["uid"] for r in s.needing_holdings("Bombay High Court")}
            self.assertEqual(uids, {"todo:1"})

if __name__ == "__main__":
    unittest.main()
