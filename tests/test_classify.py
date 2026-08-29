"""Tests for the classifier. No network: every input here is a literal taken
from the real corpora, so the suite runs offline and deterministically."""

from __future__ import annotations

import unittest

from judgments.classify import Outcome, classify_high_court, classify_supreme_court
from judgments.taxonomy import Matter, normalize_case_type, normalize_section


class TestHighCourt(unittest.TestCase):
    def test_section_and_case_type_agreeing(self):
        c = classify_high_court("Civil", "FA")
        self.assertIs(c.outcome, Outcome.CIVIL)
        self.assertEqual(c.confidence, 1.0)
        self.assertTrue(c.is_civil)

    def test_criminal(self):
        c = classify_high_court("Criminal", "ABA")
        self.assertIs(c.outcome, Outcome.CRIMINAL)
        self.assertFalse(c.is_civil)

    def test_original_side_is_civil(self):
        # The Original Side states "Original", not "Civil". Treating this as
        # unrecognised left ~31% of a real scan unclassified.
        c = classify_high_court("Original", "TP")
        self.assertIs(c.outcome, Outcome.CIVIL)

    def test_cra_is_civil_revision_not_criminal_appeal(self):
        # Reading CRA as "Criminal Appeal" is the intuitive error; in this
        # corpus every CRA carries a civil section.
        self.assertIs(normalize_case_type("CRA"), Matter.CIVIL)
        self.assertIs(classify_high_court("Civil", "CRA").outcome, Outcome.CIVIL)

    def test_writ_petition_is_settled_by_section_not_code(self):
        # WP splits ~34k civil to ~6k criminal, so the code must not decide.
        self.assertIs(normalize_case_type("WP"), Matter.NEUTRAL)
        self.assertIs(classify_high_court("Civil", "WP").outcome, Outcome.CIVIL)
        self.assertIs(classify_high_court("Criminal", "WP").outcome, Outcome.CRIMINAL)

    def test_neutral_code_does_not_add_confidence(self):
        settled = classify_high_court("Civil", "FA").confidence
        neutral = classify_high_court("Civil", "WP").confidence
        self.assertGreaterEqual(settled, neutral)

    def test_conflicting_signals_are_disputed_not_resolved(self):
        # Real: bail applications tagged with a civil section. Bail is
        # definitionally criminal, so neither side should silently win.
        c = classify_high_court("Civil", "BA")
        self.assertIs(c.outcome, Outcome.DISPUTED)
        self.assertFalse(c.is_civil)
        self.assertIn("disagree", c.rationale)

    def test_junk_section_values_are_unknown(self):
        for junk in ("False", "JUDICIAL SECTION", "", None, "nan"):
            self.assertIs(classify_high_court(junk, None).outcome, Outcome.UNKNOWN)

    def test_unrecognised_section_degrades_rather_than_raising(self):
        c = classify_high_court("Some New Section 2031", None)
        self.assertIs(c.outcome, Outcome.UNKNOWN)

    def test_case_type_alone_still_classifies(self):
        c = classify_high_court(None, "ABA")
        self.assertIs(c.outcome, Outcome.CRIMINAL)
        # Corroborating evidence alone must not reach full confidence.
        self.assertLess(c.confidence, 1.0)

    def test_signals_are_reported_for_audit(self):
        c = classify_high_court("Civil", "FA")
        sources = {s.source for s in c.signals}
        self.assertEqual(sources, {"judicial_section", "case_type"})
        self.assertTrue(all(s.raw for s in c.signals))

    def test_deterministic(self):
        a = classify_high_court("Civil", "FA")
        b = classify_high_court("Civil", "FA")
        self.assertEqual((a.outcome, a.confidence, a.rationale),
                         (b.outcome, b.confidence, b.rationale))


class TestSupremeCourt(unittest.TestCase):
    def test_jurisdiction_header(self):
        c = classify_supreme_court("CIVIL APPELLATE JURISDICTION: Civil Appeal No. 1 of 2020")
        self.assertIs(c.outcome, Outcome.CIVIL)
        self.assertEqual(c.confidence, 1.0)

    def test_criminal_header(self):
        c = classify_supreme_court(
            "CRIMINAL APPELLATE JURISDICTION: Criminal Appeal No. 1031 of 2015"
        )
        self.assertIs(c.outcome, Outcome.CRIMINAL)

    def test_scr_margin_letter_between_words(self):
        # Verbatim from the corpus: the SCR page margin letters (A-H) land
        # inside the case designation after OCR.
        c = classify_supreme_court(
            "CIVIL APPELLATE JURISDICTION: Special Leave Petition D (Civil) No. 8094 of 1988."
        )
        self.assertIs(c.outcome, Outcome.CIVIL)

    def test_abbreviated_civil_qualifier(self):
        # "(C)" is the standard SCR abbreviation; missing it dropped a large
        # share of Supreme Court writ petitions to UNKNOWN.
        c = classify_supreme_court("Ravikumar v. High Court of Gujarat (Writ Petition (c) No. 432 of 2023)")
        self.assertIs(c.outcome, Outcome.CIVIL)

    def test_abbreviated_criminal_qualifier(self):
        c = classify_supreme_court("XYZ (Special Leave Petition (Crl) No. 99 of 2020)")
        self.assertIs(c.outcome, Outcome.CRIMINAL)

    def test_crl_is_not_read_as_bare_c(self):
        # Alternation order matters: "Crl" must not match the civil "c" branch.
        self.assertIs(
            classify_supreme_court("Writ Petition (Crl) No. 1 of 2020").outcome,
            Outcome.CRIMINAL,
        )

    def test_designation_alone_has_lower_confidence_than_header(self):
        header = classify_supreme_court("CIVIL APPELLATE JURISDICTION: x")
        designation = classify_supreme_court("(Writ Petition (C) No. 1 of 2020)")
        self.assertGreater(header.confidence, designation.confidence)

    def test_repeated_header_does_not_inflate_confidence(self):
        once = classify_supreme_court("CIVIL APPELLATE JURISDICTION")
        thrice = classify_supreme_court("CIVIL APPELLATE JURISDICTION " * 3)
        self.assertEqual(once.confidence, thrice.confidence)

    def test_incidental_use_of_the_word_jurisdiction_is_not_a_header(self):
        # Real text: "invoked the jurisdiction under Article 32". Matching this
        # would fabricate a classification out of ordinary prose.
        c = classify_supreme_court("the petitioners have invoked the jurisdiction under Article 32")
        self.assertIs(c.outcome, Outcome.UNKNOWN)

    def test_empty_text_is_unknown_not_civil(self):
        self.assertIs(classify_supreme_court("").outcome, Outcome.UNKNOWN)

    def test_contradictory_text_is_disputed(self):
        c = classify_supreme_court(
            "CIVIL APPELLATE JURISDICTION ... CRIMINAL APPELLATE JURISDICTION"
        )
        self.assertIs(c.outcome, Outcome.DISPUTED)

    def test_only_the_head_is_read(self):
        # A civil judgment citing a criminal appeal 40k characters in must not
        # be reclassified by that citation.
        text = (
            "CIVIL APPELLATE JURISDICTION "
            + ("filler " * 8_000)
            + " CRIMINAL APPELLATE JURISDICTION"
        )
        self.assertIs(classify_supreme_court(text).outcome, Outcome.CIVIL)


class TestNormalisation(unittest.TestCase):
    def test_section_is_case_and_space_insensitive(self):
        for v in ("Civil", "civil", "  CIVIL  "):
            self.assertIs(normalize_section(v), Matter.CIVIL)

    def test_case_type_is_upper_cased(self):
        self.assertIs(normalize_case_type("fa"), Matter.CIVIL)

    def test_unknown_inputs(self):
        self.assertIs(normalize_section(None), Matter.UNKNOWN)
        self.assertIs(normalize_case_type("ZZZ"), Matter.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
