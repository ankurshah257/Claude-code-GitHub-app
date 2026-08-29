"""Indian court case-type and judicial-section vocabulary.

Every mapping here was checked against the real corpus rather than reasoned
from the abbreviation, because the obvious reading of a code is sometimes the
wrong one. The clearest example is ``CRA``: read as "Criminal Appeal" it looks
obviously criminal, but in the Bombay High Court corpus all 1,327 CRA matters
carry a civil judicial section — there it means *Civil Revision Application*.
Hardcoding the obvious guess would silently drop those from a civil scan.

That is why ``case_type`` is only ever a corroborating signal in this system.
The court's own ``judicial_section`` field is the authority; this table exists
to catch the cases where that field is missing, junk, or contradicted.
"""

from __future__ import annotations

from enum import Enum


class Matter(str, Enum):
    """What kind of matter a judgment decides."""

    CIVIL = "civil"
    CRIMINAL = "criminal"
    #: Genuinely both or neither — writ jurisdiction, interim applications, and
    #: other procedural vehicles that inherit their nature from a parent case.
    NEUTRAL = "neutral"
    #: No basis to say. Never guessed at.
    UNKNOWN = "unknown"


#: Normalization of the Bombay High Court ``judicial_section`` field.
#:
#: Observed values are not a clean two-value enum: the field carries some
#: case-type names ("First Appeal"), some sub-sections ("Civil Writ"), and the
#: literal string "False", which is a boolean that leaked into a text column.
JUDICIAL_SECTION: dict[str, Matter] = {
    "civil": Matter.CIVIL,
    "civil writ": Matter.CIVIL,
    # The Bombay High Court Original Side exercises ordinary *original civil*
    # jurisdiction — suits, commercial, arbitration, company, testamentary. Its
    # section field says "Original" rather than "Civil", which left a third of a
    # full scan unclassified until it was mapped. Verified before trusting it:
    # across 71 distinct case types appearing under this value, none is a known
    # criminal type, and the Original Side benches carry no "Criminal" section
    # at all.
    "original": Matter.CIVIL,
    "suit": Matter.CIVIL,
    "appeal from order": Matter.CIVIL,
    "first appeal": Matter.CIVIL,
    "second appeal": Matter.CIVIL,
    "criminal": Matter.CRIMINAL,
    "criminal application": Matter.CRIMINAL,
    "criminal writ": Matter.CRIMINAL,
    # Junk, not sections. Mapped explicitly so they are dropped as unusable
    # rather than falling through as unrecognised vocabulary: "False" is a
    # boolean that leaked into a text column, and "JUDICIAL SECTION" is a
    # header row that leaked into the data.
    "false": Matter.UNKNOWN,
    "judicial section": Matter.UNKNOWN,
}


#: Case-type codes whose nature is unambiguous within the High Court corpus.
#:
#: Codes absent from this table are not errors — they are types this system
#: declines to judge from the code alone, which is the honest default given how
#: much these vocabularies vary between courts and even between benches.
CASE_TYPE: dict[str, Matter] = {
    # --- Civil ------------------------------------------------------------
    "FA": Matter.CIVIL,  # First Appeal
    "SA": Matter.CIVIL,  # Second Appeal
    "AO": Matter.CIVIL,  # Appeal from Order
    "CRA": Matter.CIVIL,  # Civil Revision Application (NOT Criminal Appeal)
    "CP": Matter.CIVIL,  # Company Petition
    "ARP": Matter.CIVIL,  # Arbitration Petition
    "ARA": Matter.CIVIL,  # Arbitration Application
    "MCA": Matter.CIVIL,  # Misc. Civil Application
    "FCA": Matter.CIVIL,  # Family Court Appeal
    "COMAP": Matter.CIVIL,  # Commercial Appeal
    "COMAO": Matter.CIVIL,  # Commercial Appeal from Order
    "COARP": Matter.CIVIL,  # Commercial Arbitration Petition
    "COARA": Matter.CIVIL,  # Commercial Arbitration Application
    "LPA": Matter.CIVIL,  # Letters Patent Appeal
    "TS": Matter.CIVIL,  # Testamentary Suit
    "CS": Matter.CIVIL,  # Civil Suit
    "EP": Matter.CIVIL,  # Execution Petition
    "NOM": Matter.CIVIL,  # Notice of Motion
    "FEMA": Matter.CIVIL,  # Foreign Exchange Management Act appeal
    # Original Side civil vehicles. Types seen there whose expansion is not
    # certain (CRR, JOT, GP, APP) are deliberately left out: absent from this
    # table means "no signal", which is safer than a guess that could
    # manufacture a false conflict with the section field.
    "TP": Matter.CIVIL,  # Testamentary Petition
    "ARBP": Matter.CIVIL,  # Arbitration Petition
    "ARBAP": Matter.CIVIL,  # Arbitration Appeal
    "CARBP": Matter.CIVIL,  # Commercial Arbitration Petition
    "CARAP": Matter.CIVIL,  # Commercial Arbitration Appeal
    "COMIP": Matter.CIVIL,  # Commercial IP Suit
    "COMMP": Matter.CIVIL,  # Commercial Miscellaneous Petition
    "COMAS": Matter.CIVIL,  # Commercial Appeal from Summary Suit
    "EXA": Matter.CIVIL,  # Execution Application
    "OLR": Matter.CIVIL,  # Official Liquidator Report
    "MPT": Matter.CIVIL,  # Miscellaneous Petition
    # --- Criminal ---------------------------------------------------------
    "BA": Matter.CRIMINAL,  # Bail Application
    "ABA": Matter.CRIMINAL,  # Anticipatory Bail Application
    "APEAL": Matter.CRIMINAL,  # Criminal Appeal
    "APL": Matter.CRIMINAL,  # Criminal Application
    "ALP": Matter.CRIMINAL,  # Criminal Application (leave to appeal)
    "CRLA": Matter.CRIMINAL,
    "CRLREV": Matter.CRIMINAL,
    "CRWP": Matter.CRIMINAL,
    "WPCRL": Matter.CRIMINAL,
    # --- Genuinely ambiguous ---------------------------------------------
    # WP splits roughly 34k civil to 6k criminal in a single bench-year, and IA
    # splits about 15k to 9k. Both are procedural vehicles, so the code carries
    # no information about the matter and only judicial_section can settle it.
    "WP": Matter.NEUTRAL,  # Writ Petition
    "IA": Matter.NEUTRAL,  # Interim Application
    "PIL": Matter.NEUTRAL,  # Public Interest Litigation
    "REVN": Matter.NEUTRAL,  # Revision — civil or criminal depending on origin
    "APPLN": Matter.NEUTRAL,  # Application
}


#: Supreme Court jurisdiction headers, which is where the SC corpus states the
#: nature of a matter. The SC metadata carries no case-type field at all, so
#: these strings — matched against judgment text — are the only structured
#: signal available for that court.
SC_JURISDICTION_MARKERS: dict[str, Matter] = {
    "civil appellate jurisdiction": Matter.CIVIL,
    "civil original jurisdiction": Matter.CIVIL,
    "civil review jurisdiction": Matter.CIVIL,
    "criminal appellate jurisdiction": Matter.CRIMINAL,
    "criminal original jurisdiction": Matter.CRIMINAL,
    "criminal review jurisdiction": Matter.CRIMINAL,
}


#: Supreme Court case designations, used to corroborate the jurisdiction header
#: and to classify judgments whose header did not survive OCR.
SC_CASE_DESIGNATIONS: dict[str, Matter] = {
    "special leave petition (civil)": Matter.CIVIL,
    "civil appeal": Matter.CIVIL,
    "writ petition (civil)": Matter.CIVIL,
    "transfer petition (civil)": Matter.CIVIL,
    "civil miscellaneous petition": Matter.CIVIL,
    "special leave petition (criminal)": Matter.CRIMINAL,
    "criminal appeal": Matter.CRIMINAL,
    "writ petition (criminal)": Matter.CRIMINAL,
    "transfer petition (criminal)": Matter.CRIMINAL,
}


#: eCourts court code for the Bombay High Court, used as the S3 partition key.
BOMBAY_COURT_CODE = "27_1"

#: Bombay High Court benches as they appear in the S3 ``bench=`` partition.
#: ``testcase`` is not a test fixture despite the name — it holds real Nagpur
#: bench judgments, so excluding it would silently lose that seat.
BOMBAY_BENCHES: dict[str, str] = {
    "newos": "Principal Seat, Bombay (Original Side)",
    "newas": "Principal Seat, Bombay (Appellate Side)",
    "newos_spl": "Principal Seat, Bombay (Original Side, Special)",
    "hcaurdb": "Aurangabad Bench",
    "hcbgoa": "Goa Bench (Panaji)",
    "kolhcdb": "Kolhapur Bench",
    "testcase": "Nagpur Bench",
}

#: Bench tokens in Bombay neutral citations, e.g. ``2025:BHC-AUG:15099``.
NEUTRAL_CITATION_BENCH: dict[str, str] = {
    "OS": "Principal Seat, Bombay (Original Side)",
    "AS": "Principal Seat, Bombay (Appellate Side)",
    "AUG": "Aurangabad Bench",
    "NAG": "Nagpur Bench",
    "GOA": "Goa Bench (Panaji)",
    "KOL": "Kolhapur Bench",
}


def normalize_section(value: object) -> Matter:
    """Map a raw ``judicial_section`` cell to a :class:`Matter`.

    Unrecognised non-empty values return ``UNKNOWN`` rather than raising: court
    vocabularies grow, and a new section name should reduce confidence in one
    record, not abort a scan of hundreds of thousands.
    """
    if value is None:
        return Matter.UNKNOWN
    text = str(value).strip().lower()
    if not text or text in {"nan", "none", "<null>"}:
        return Matter.UNKNOWN
    return JUDICIAL_SECTION.get(text, Matter.UNKNOWN)


def normalize_case_type(value: object) -> Matter:
    """Map a raw ``case_type`` cell to a :class:`Matter`."""
    if value is None:
        return Matter.UNKNOWN
    text = str(value).strip().upper()
    if not text or text in {"NAN", "NONE"}:
        return Matter.UNKNOWN
    return CASE_TYPE.get(text, Matter.UNKNOWN)
