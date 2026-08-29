# Indian civil judgments scanner

Scans every judgment of the **Bombay High Court** and the **Supreme Court of
India**, classifies each as civil or criminal, and stores the civil ones in a
queryable SQLite database.

## The act-wise civil digest

The headline output. One row per civil judgment — **the name, the court, the
date, and what was held** — indexed by the Act it turns on, from 1 January 2026.

```bash
pip install -r requirements.txt

python -m judgments digest --since 2026-01-01   # build / refresh (the weekly job)
python -m judgments acts                        # act-wise index
python -m judgments acts --act "Arbitration and Conciliation"
```

```
judgments  act
   11,593  Code of Civil Procedure, 1908
    8,721  Constitution of India
    4,088  Indian Succession Act, 1925
    1,882  Arbitration and Conciliation Act, 1996
```

`.github/workflows/weekly-digest.yml` runs `digest` every Monday. Re-running is
cheap and idempotent: the source exports gain rows as judgments are published,
so the job re-reads them and upserts on judgment id, and Supreme Court PDFs
already digested are never fetched twice.

### What "held" means, and when it is missing

For the **Supreme Court** the holding is quoted **verbatim from the official
eSCR headnote** — the Court's own words, citable as such. The **Bombay High
Court publishes no headnotes**, so its rows carry no holding rather than a
generated summary. Every row records which it is in `held_source`
(`headnote` or `none`), so a quotation can never be mistaken for a paraphrase.

Filling the gap would mean having a model read each judgment. That is a
reading, not a source, and this system does not silently pass one off as the
other; if you want it, it should be added as an explicit, separately labelled
`generated` provenance.

## Full-corpus scanning

```bash
python -m judgments scan bombay --from 2020 --to 2024
python -m judgments scan supreme --from 2023 --to 2024
python -m judgments stats
python -m judgments civil --final --limit 20
python -m judgments civil --csv > civil.csv
python -m judgments review          # what the system refused to classify
```

Scans resume: interrupt one and re-run it, and it picks up at the first
partition it had not finished.

## Where the data comes from

Not from scraping. `sci.gov.in`, `bombayhighcourt.nic.in`, and the eCourts
portal are CAPTCHA-gated and hostile to enumeration. The same judgments are
published as open datasets in two public S3 buckets, already extracted and
partitioned:

| | Bucket | Size |
|---|---|---|
| High Courts | `indian-high-court-judgments` | ~804k judgments, 1963– |
| Supreme Court | `indian-supreme-court-judgments` | ~43k judgments, 1950– |

Bombay is partition `court=27_1`, across seven benches: the Principal Seat
(Original and Appellate Sides), Nagpur, Aurangabad, Goa, and Kolhapur.

## How civil matters are identified

The two courts publish completely different metadata, so they need different
classifiers. Both produce a decision, a confidence, and the **evidence** it
rests on, so any answer can be traced to the field that produced it.

**Bombay High Court** — the export carries `judicial_section`, which is the
court's own civil/criminal label. That is authoritative, so it is the primary
signal; `case_type` corroborates it. No PDF is opened.

**Supreme Court** — the export carries no case type and no section, only a
neutral citation (`2024 INSC 735`) and party names. Nothing in it says whether
a matter is civil. So the judgment PDF is fetched and its head is read for the
jurisdiction header (`CIVIL APPELLATE JURISDICTION`), falling back to the case
designation (`Writ Petition (C) No. 432 of 2023`). This is much slower —
`--index-only` builds the index without it.

### Four outcomes, not two

`CIVIL` and `CRIMINAL` are the settled answers. The other two carry the value:

- **`UNKNOWN`** — no usable signal. The system declines rather than guessing.
- **`DISPUTED`** — signals contradicted each other. Real: five Bombay bail
  applications carry a *civil* judicial section, and bail is definitionally
  criminal. Collapsing that to a verdict would hide a data error.

`civil` queries exclude both, so what you get is only what the system stands
behind. `review` shows what was set aside. Being able to see the residue is the
point — a scan that silently dropped it would look cleaner and be less
trustworthy.

## What the corpus will do to you

Eight traps, each of which produced a wrong answer here before it was fixed.

**`CRA` is not Criminal Appeal.** In the Bombay corpus every one of its 1,327
CRA matters carries a civil section: there it means *Civil Revision
Application*. Hardcoding the intuitive reading silently drops them all. This is
why `case_type` only ever corroborates and never overrides.

**The Original Side says `Original`, not `Civil`.** The Bombay Original Side
exercises ordinary original civil jurisdiction — suits, commercial, arbitration,
company, testamentary — but labels its section `Original`. Treating that as
unrecognised left **31% of a real scan unclassified**; mapping it recovered
64,812 civil judgments. Verified before trusting: across the 71 distinct case
types under that value, none is a known criminal type, and Original Side benches
carry no `Criminal` section at all.

**`cnr` is a case id, not a document id.** A case averages ~3.4 orders. Keying
on `cnr` collapses 112,594 documents to 32,848. The document key is
`cnr + order_number`.

**Most documents are not judgments.** In one bench-year, 95,121 of 112,594
documents are interim orders and only 17,473 are final. Use `--final` when you
mean judgments.

**`(C)` means civil.** Supreme Court citations abbreviate the qualifier —
`Writ Petition (C)`, `SLP (Crl)`. Matching only the full words drops a large
share of SC writ petitions to `UNKNOWN`.

**A statute's year is part of its identity.** Normalising act names by
stripping the year merges `Companies Act 1956` with `Companies Act 2013`, and
`Arbitration Act 1940` with the 1996 Act that repealed it — pooling judgments
decided under repealed law with current law. Rendering is normalised; the year
is not. A year-less variant is merged only where the corpus offers exactly one
candidate, so `Indian Succession Act` resolves to 1925 while `Companies Act`
stays separate. That merge has to run over the finished index, not per record:
done per record it left the Code of Civil Procedure split into two entries of
7,883 and 3,789 judgments.

**The two courts date things differently.** The High Court writes
`YYYY-MM-DD`, the Supreme Court `DD-MM-YYYY`. Comparing them as strings without
normalising drops every Supreme Court judgment from a date window. Dates are
normalised at the source so one column never holds both.

**Some judgments are dated in the future.** A registry data-entry error, small
in number, but it puts judgments that have not happened at the top of every
date-sorted list. They are rejected on ingest.

A further one, for anyone using the Legal Data Hunter index of the same corpus rather
than S3: its `date` field is the **crawl** date, not the decision date. A 1992
second appeal is dated 2026 there, which is what produces a `max_year` of 2917.
This scanner reads `decision_date` from the Parquet metadata instead.

## Layout

```
judgments/
  taxonomy.py        case-type and section vocabulary, checked against the corpus
  classify.py        the classifier — evidence, confidence, UNKNOWN, DISPUTED
  acts.py            statute canonicalisation for act-wise grouping
  holding.py         verbatim "Held:" and "List of Acts" from eSCR headnotes
  sources/s3.py      anonymous S3 listing and fetch, paginated and retried
  sources/bombay.py  Bombay adapter (Parquet metadata, no PDFs)
  sources/supreme.py Supreme Court adapter (per-judgment PDF text)
  store.py           SQLite, idempotent writes, resumable partitions
  scan.py            the pipeline
  cli.py             command line
tests/               68 tests, no network
```

`python -m unittest discover -s tests`

## Limits

- **Bombay classification is only as good as `judicial_section`.** It is the
  registry's own field, and the ~0.4% it leaves as UNKNOWN or DISPUTED is
  reported rather than papered over.
- **Supreme Court coverage is the reported corpus** (SCR/eSCR), not every order
  the Court has passed.
- **A full Supreme Court scan is slow**: one PDF fetch and parse per judgment,
  ~43k judgments. Scoping by year is usually what you want.
- **Scanned-image judgments have no text layer.** Older SC PDFs may extract
  nothing; those surface as `UNKNOWN` rather than being guessed at. OCR is not
  attempted.
- **Act coverage differs by court.** Bombay records statutes in a structured
  registry field. The Supreme Court has no such field, so its acts come from
  the headnote's `List of Acts` — present in reported judgments, absent if the
  headnote is missing.
- **Roughly 860 distinct acts** survive canonicalisation from about a thousand
  raw spellings per bench. Registry entries such as `Other Act` are kept as
  recorded rather than being reinterpreted.
- **Indian Kanoon is deliberately not used.** It is a private aggregator whose
  terms do not permit bulk scraping, and it sells an API for that purpose. The
  judgments here come from the courts' own open-data mirrors, where Indian
  court judgments are public records under §52(1)(q) of the Copyright Act.
- **Nothing here is legal advice**, and a classification is a research filter,
  not a determination. Verify against the official record before relying on it.
