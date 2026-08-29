"""Tests for the Indian Kanoon source. No network.

The HTTP layer is faked. What these lock down is the part that protects the
data: a judgment is accepted only when the court, the date and the parties all
agree, because attaching the wrong judgment's holding to a row is worse than
leaving the row empty.
"""

from __future__ import annotations

import unittest

from judgments.sources import kanoon
from judgments.sources.kanoon import (
    AuthError,
    Hit,
    KanoonClient,
    build_query,
    fetch_texts,
    find_judgment,
    _strip_markup,
    _to_ik_date,
)


class FakeClient:
    """Stands in for KanoonClient, recording what it was asked."""

    def __init__(self, hits=(), doc="JUDGMENT TEXT"):
        self._hits = list(hits)
        self._doc = doc
        self.queries: list[str] = []
        self.fetched: list[int] = []
        self.calls = kanoon.Calls()

    def search(self, query, pagenum=0):
        self.queries.append(query)
        self.calls.searches += 1
        return list(self._hits), len(self._hits)

    def document(self, docid):
        self.fetched.append(docid)
        self.calls.documents += 1
        # The real client strips markup inside document(), so the fake must
        # too, or callers get HTML the production path would never hand them.
        return _strip_markup(self._doc)


BOMBAY = "Bombay High Court"


def hit(title="ABC Traders v. State of Maharashtra",
        court="Bombay High Court", date="2026-03-11", docid=101):
    return Hit(docid=docid, title=title, court=court, date=date)


class TestQuery(unittest.TestCase):
    def test_date_format_is_theirs_not_iso(self):
        self.assertEqual(_to_ik_date("2026-03-11"), "11-03-2026")

    def test_query_pins_court_and_both_ends_of_the_date(self):
        # Pinning both ends to the decision date is what makes matching safe:
        # it reduces candidates to that court's judgments on that one day.
        q = build_query("ABC v. XYZ", BOMBAY, "2026-03-11")
        self.assertIn("doctypes: bombay", q)
        self.assertIn("fromdate: 11-03-2026", q)
        self.assertIn("todate: 11-03-2026", q)

    def test_unknown_court_omits_the_doctype_filter(self):
        self.assertNotIn("doctypes:", build_query("A v. B", "Some Tribunal", "2026-01-01"))


class TestMatching(unittest.TestCase):
    def test_accepts_a_judgment_that_agrees_on_all_three(self):
        c = FakeClient([hit()])
        m = find_judgment(c, "ABC Traders v. State of Maharashtra", BOMBAY, "2026-03-11")
        self.assertTrue(m.found)
        self.assertEqual(m.hit.docid, 101)

    def test_rejects_a_different_date(self):
        # Same parties, wrong day: a different order in the same matter.
        c = FakeClient([hit(date="2026-03-12")])
        m = find_judgment(c, "ABC Traders v. State of Maharashtra", BOMBAY, "2026-03-11")
        self.assertFalse(m.found)

    def test_rejects_a_different_court(self):
        # The doctypes filter is their taxonomy; the returned court is checked
        # directly so a wrong or changed code cannot admit another court.
        c = FakeClient([hit(court="Delhi High Court")])
        m = find_judgment(c, "ABC Traders v. State of Maharashtra", BOMBAY, "2026-03-11")
        self.assertFalse(m.found)

    def test_rejects_unrelated_parties(self):
        c = FakeClient([hit(title="Zeta Industries v. Union of India")])
        m = find_judgment(c, "ABC Traders v. State of Maharashtra", BOMBAY, "2026-03-11")
        self.assertFalse(m.found)
        self.assertIn("none matched", m.reason)

    def test_tolerates_different_renderings_of_the_same_name(self):
        # "M/s", "Ors", honorifics and case differ between the registry and IK.
        c = FakeClient([hit(title="M/S ABC TRADERS vs STATE OF MAHARASHTRA AND ORS")])
        m = find_judgment(c, "ABC Traders v. State of Maharashtra", BOMBAY, "2026-03-11")
        self.assertTrue(m.found)

    def test_picks_the_best_of_several_candidates(self):
        c = FakeClient([
            hit(title="ABC Traders v. Someone Else", docid=1),
            hit(title="ABC Traders v. State of Maharashtra", docid=2),
        ])
        m = find_judgment(c, "ABC Traders v. State of Maharashtra", BOMBAY, "2026-03-11")
        self.assertEqual(m.hit.docid, 2)

    def test_no_results_is_reported_not_raised(self):
        m = find_judgment(FakeClient([]), "A v. B", BOMBAY, "2026-03-11")
        self.assertFalse(m.found)
        self.assertIn("no results", m.reason)

    def test_empty_name_makes_no_call(self):
        c = FakeClient([hit()])
        m = find_judgment(c, "   ", BOMBAY, "2026-03-11")
        self.assertFalse(m.found)
        self.assertEqual(c.queries, [])


class TestFetchTexts(unittest.TestCase):
    def test_document_is_only_fetched_after_a_match(self):
        # A failed lookup should cost one search, not a wasted document call.
        c = FakeClient([hit(title="Totally Different Parties")])
        out = list(fetch_texts(c, iter([("u1", "ABC Traders v. State", BOMBAY, "2026-03-11")])))
        self.assertEqual(out[0][1], "")
        self.assertEqual(c.fetched, [])

    def test_returns_text_on_a_match(self):
        c = FakeClient([hit()], doc="<p>Held: appeal <b>allowed</b>.</p>")
        out = list(fetch_texts(c, iter([("u1", "ABC Traders v. State of Maharashtra",
                                         BOMBAY, "2026-03-11")])))
        uid, text, match = out[0]
        self.assertEqual(uid, "u1")
        self.assertTrue(match.found)
        self.assertIn("Held: appeal allowed", text)


class TestClient(unittest.TestCase):
    def test_token_is_required(self):
        with self.assertRaises(AuthError):
            KanoonClient("")

    def test_auth_header_shape(self):
        c = KanoonClient("abc123")
        self.assertEqual(c._headers["Authorization"], "Token abc123")
        self.assertEqual(c._headers["Accept"], "application/json")


class TestStripMarkup(unittest.TestCase):
    def test_tags_removed_and_breaks_preserved(self):
        self.assertEqual(_strip_markup("<p>One</p><br>Two"), "One\n\nTwo")

    def test_entities_decoded(self):
        self.assertIn("A & B", _strip_markup("A &amp; B"))

    def test_script_and_style_dropped_entirely(self):
        out = _strip_markup("<style>x{}</style><script>bad()</script><p>Real</p>")
        self.assertNotIn("bad()", out)
        self.assertIn("Real", out)

    def test_empty(self):
        self.assertEqual(_strip_markup(""), "")


if __name__ == "__main__":
    unittest.main()
