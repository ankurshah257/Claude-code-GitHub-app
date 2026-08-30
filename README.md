# Indian civil judgments scanner

Scans every judgment of the **Bombay High Court** and the **Supreme Court of
India**, classifies each as civil or criminal, and stores the civil ones in a
queryable SQLite database.

## The act-wise civil digest

The headline output. One row per civil judgment — **the name, the court, the
date, and what was held** — indexed by the Act it turns on, from 1 January 2026.

```bash
pip install -r requirements.txt

python -m judgments digest --since 2026-01-01   # build / refresh the digest
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

### Running it on demand

`.github/workflows/weekly-digest.yml` refreshes the digest and generates
holdings for what is new. **It runs only when you start it** — Actions → *Judgments
digest (manual)* → *Run workflow*. There is deliberately no schedule: the
summarise step spends real money, and nothing here should spend it unattended.

Re-running is cheap and idempotent: source exports gain rows as judgments are
published, so the job re-reads and upserts on judgment id, and documents already
read are never fetched twice.

Add two repository secrets:

| Secret | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | generating holdings — without it the job still builds the digest and skips summarising |
| `INDIANKANOON_TOKEN` | judgments missing from the open corpus |

Two rules bound what any single run can spend:

- **Every run is capped** by `summarize_limit` (default 500, about $16). The
  40k backfill costs over a thousand dollars, so it only happens if you type a
  limit that large.
- **Indian Kanoon is opt-in per run** (`use_kanoon`). Leave it unticked and the
  run stays on the free open corpus, billing them nothing.

**Holdings are committed to `data/holdings.csv`.** The digest can be rebuilt
from the open corpus for nothing, but holdings cannot — so they are restored
into the database *before* each summarise step. A cache eviction then costs
rebuild time instead of silently re-buying work already paid for. The file is
plain text sorted by id, so git delta-compresses it and a run adds
kilobytes.

The cap is applied *after* filtering to judgments whose text can actually be
retrieved. Applied before, a `--limit 20` run sampled 20 rows of which ~6% were
reachable and summarised nothing — which is exactly what the first real run
did.

### What "held" means, and when it is missing

For the **Supreme Court** the holding is quoted **verbatim from the official
eSCR headnote** — the Court's own words, citable as such. The **Bombay High
Court publishes no headnotes**, so its rows carry no holding rather than a
generated summary. Every row records which it is in `held_source`
(`headnote` or `none`), so a quotation can never be mistaken for a paraphrase.

### Generated holdings, and the ceiling on them

`summarize` fills the Bombay gap by having Claude read the judgment and write
the holding the court did not publish. It is stored as
`held_source='generated'` and never merged with headnote text — a headnote is
citable, a reading is not.

```bash
python -m judgments summarize --estimate      # coverage and price, spends nothing
python -m judgments summarize --limit 50      # try a batch
python -m judgments summarize --yes           # the lot
```

**Most Bombay judgments cannot be summarised, because the judgment itself is
not published.** The registry metadata covers far more matters than the PDF
corpus does:

```
42,646 judgments lack a holding; 2,560 have a retrievable PDF (40,086 have none).
Estimated cost: $83.20 (~$0.0325 each, model claude-opus-5)
```

That 6% is a property of the open corpus, not of this tool — sampled judgments
missing from the 2026 partition are absent from 2023–2025 as well.

### Filling the rest from Indian Kanoon

The remaining 40,086 are sourced from Indian Kanoon's **official API**, which
needs a token from https://api.indiankanoon.org and bills per call:

```bash
export INDIANKANOON_TOKEN=...
python -m judgments summarize --estimate     # now reports full coverage
python -m judgments summarize --yes
```

```
42,646 judgments lack a holding; 2,560 have a PDF in the open corpus.
40,086 will be looked up on Indian Kanoon (their paid API bills ~2 calls each).
Estimated cost: $1,385.99   (model spend; Indian Kanoon bills separately)
```

This is their API, not their website. Scraping the site is what their terms
prohibit; the API is the sanctioned route for bulk use. The request shape —
`POST` with parameters in the query string, `Authorization: Token` — is taken
from their own published client rather than inferred.

**A match must agree on court, date and parties before it is used.** The search
pins `fromdate` and `todate` to the decision date, which narrows candidates to
what that court delivered that day; the court is then re-checked against the
`docsource` the API returns rather than trusted from the `doctypes:` filter,
and party names are compared after normalising away `M/s`, `Ors`, honorifics
and case. A near-miss is recorded as not found: attaching the wrong judgment's
holding to a row is worse than the empty cell it would replace. The document is
only fetched once a match is confirmed, so a failed lookup costs one billed
call instead of two.

The run establishes what is reachable *before* spending anything, prices it
from measured token usage, and refuses to start a run over 200 judgments
without `--yes`. Procedural orders that decide nothing are recorded as such
rather than given an invented holding.

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
  summarize.py       generated holdings where no headnote exists, with costing
  sources/s3.py      anonymous S3 listing and fetch, paginated and retried
  sources/bombay.py  Bombay adapter (Parquet metadata, no PDFs)
  sources/supreme.py Supreme Court adapter (per-judgment PDF text)
  store.py           SQLite, idempotent writes, resumable partitions
  scan.py            the pipeline
  cli.py             command line
tests/               86 tests, no network
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
- **Judgment PDFs exist for only ~6% of Bombay registry matters** in the open
  corpus. The case-details export lists every matter the registry recorded; the
  document corpus holds far fewer. Indian Kanoon covers the rest, at their
  per-call price.
- **Indian Kanoon's court codes are unverified here.** `DOCTYPES` holds their
  identifiers, which could not be confirmed against the live API from this
  environment. They are overridable, and because every result is re-checked
  against the returned court name, a wrong code yields no matches rather than
  wrong judgments.
- **Generated holdings are a reading, not a source.** They carry
  `held_source='generated'`, and should not be quoted as the court's words.
- **Some `.pdf` objects are not PDFs.** A few hold the court site's own 404
  page, saved by the crawler and served by S3 with a 200. They are detected by
  magic bytes and skipped.
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
