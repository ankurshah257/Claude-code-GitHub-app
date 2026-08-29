"""Tests for act canonicalisation and holding extraction. No network.

The inputs are real spellings taken from the corpus, so these lock in the
distinctions that matter legally rather than only checking string handling.
"""

from __future__ import annotations

import unittest

from judgments.acts import apply_resolution, canonical, parse, resolve_years
from judgments.holding import Provenance, extract_acts, extract_held


def key(raw: str, resolution: dict[str, int] | None = None) -> str:
    ref = canonical(raw)
    assert ref is not None, raw
    if resolution:
        ref = apply_resolution(ref, resolution)
    return ref.key


class TestCanonical(unittest.TestCase):
    def test_rendering_differences_merge(self):
        for a, b in [
            ("TRADE MARKS ACT", "Trade Marks Act"),
            ("Companies Act & Rules  1956", "Companies Act 1956"),
            ("Criminal Procedure Code (Cr.P.C.)", "Criminal Procedure Code"),
        ]:
            self.assertEqual(key(a), key(b), f"{a!r} should equal {b!r}")

    def test_different_statutes_stay_separate(self):
        # Each of these pairs is two distinct enactments. Merging them would
        # pool judgments under law that was not applied.
        for a, b in [
            ("Companies Act 1956", "Companies Act 2013"),
            ("Arbitration Act 1940", "Arbitration and Conciliation Act 1996"),
            ("Indian Succession Act 1925", "Hindu Succession Act 1956"),
            ("Trade Marks Act 1999", "Trade Unions Act 1926"),
        ]:
            self.assertNotEqual(key(a), key(b), f"{a!r} must not equal {b!r}")

    def test_year_is_part_of_identity(self):
        self.assertIsNone(canonical("Companies Act").year)
        self.assertEqual(canonical("Companies Act 2013").year, 2013)

    def test_abbreviations_expand(self):
        self.assertEqual(key("C.P.C.- (Interlocutory Order)"), key("Code of Civil Procedure"))

    def test_dangling_conjunction_removed(self):
        self.assertEqual(canonical("Motor Vehicles Act & Rules 1988").label,
                         "Motor Vehicles Act, 1988")

    def test_junk_returns_none(self):
        for junk in ("", "   ", "nan", "Act", "rules", None):
            self.assertIsNone(canonical(junk))

    def test_label_is_readable(self):
        self.assertEqual(canonical("indian succession act, 1925").label,
                         "Indian Succession Act, 1925")


class TestResolveYears(unittest.TestCase):
    def test_unambiguous_name_adopts_its_year(self):
        refs = [canonical("Indian Succession Act, 1925"), canonical("Indian Succession Act")]
        resolution = resolve_years([r for r in refs if r])
        self.assertEqual(key("Indian Succession Act", resolution),
                         key("Indian Succession Act, 1925"))

    def test_ambiguous_name_is_left_unresolved(self):
        # Companies Act appears with two years, so a year-less mention cannot
        # be assigned to either without guessing.
        refs = [canonical("Companies Act 1956"), canonical("Companies Act 2013"),
                canonical("Companies Act")]
        resolution = resolve_years([r for r in refs if r])
        self.assertNotIn("companies act", resolution)
        self.assertEqual(key("Companies Act", resolution), "companies act|")

    def test_resolution_leaves_dated_refs_untouched(self):
        refs = [canonical("Indian Succession Act, 1925")]
        resolution = resolve_years([r for r in refs if r])
        self.assertEqual(key("Indian Succession Act, 1925", resolution),
                         "indian succession act|1925")


class TestParse(unittest.TestCase):
    def test_parses_act_section_records(self):
        refs = parse([{"act": "Code of Civil Procedure 1908", "section": "Rule 11 (2)"}])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].section, "Rule 11 (2)")
        self.assertEqual(refs[0].year, 1908)

    def test_trailing_punctuation_stripped_from_section(self):
        refs = parse([{"act": "Indian Succession Act, 1925", "section": "276,"}])
        self.assertEqual(refs[0].section, "276")

    def test_duplicates_within_one_record_collapse(self):
        refs = parse([
            {"act": "Indian Succession Act, 1925", "section": "276"},
            {"act": "INDIAN SUCCESSION ACT 1925", "section": "278"},
        ])
        self.assertEqual(len(refs), 1)

    def test_empty_and_malformed_input(self):
        self.assertEqual(parse(None), [])
        self.assertEqual(parse([]), [])
        self.assertEqual(parse(["not a dict"]), [])


HEADNOTE = (
    "Issue for Consideration\nSomething arose.\n"
    "Held: 13 workers entitled to regularisation on parity basis - Workers "
    "entitled to back wages.\n"
    "Case Law Cited\nX v. Y\n"
    "List of Acts\nNational Company Law Tribunal Rules, 2016; National Company \n"
    "Law Appellate Tribunal Rules, 2016; Insolvency and Bankruptcy \nCode, 2016.\n"
    "List of Keywords\nregularisation\n"
)


class TestHolding(unittest.TestCase):
    def test_held_is_extracted_verbatim(self):
        h = extract_held(HEADNOTE)
        self.assertIs(h.provenance, Provenance.HEADNOTE)
        self.assertTrue(h.is_verbatim)
        self.assertTrue(h.text.startswith("13 workers entitled"))

    def test_held_stops_at_the_next_heading(self):
        h = extract_held(HEADNOTE)
        self.assertNotIn("Case Law Cited", h.text)
        self.assertNotIn("List of Acts", h.text)

    def test_missing_held_is_not_invented(self):
        h = extract_held("A judgment with no headnote at all.")
        self.assertIs(h.provenance, Provenance.NONE)
        self.assertEqual(h.text, "")
        self.assertFalse(h.is_verbatim)

    def test_empty_input(self):
        self.assertIs(extract_held("").provenance, Provenance.NONE)

    def test_acts_split_on_semicolons_not_newlines(self):
        # Newlines inside this block are PDF line-wrapping within one name;
        # splitting on them truncates "National Company Law Appellate Tribunal".
        acts = extract_acts(HEADNOTE)
        self.assertIn("National Company Law Appellate Tribunal Rules, 2016", acts)
        self.assertIn("Insolvency and Bankruptcy Code, 2016", acts)

    def test_acts_stop_at_next_heading(self):
        self.assertTrue(all("Keywords" not in a for a in extract_acts(HEADNOTE)))

    def test_running_page_header_stripped_from_act(self):
        from judgments.holding import _tidy
        self.assertNotIn("Supreme Court Reports",
                         _tidy("Penal Code 426 Supreme Court Reports, 1860"))

    def test_no_acts_section(self):
        self.assertEqual(extract_acts("no list here"), [])


if __name__ == "__main__":
    unittest.main()
