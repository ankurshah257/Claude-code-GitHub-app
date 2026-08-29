"""Indian Kanoon as a source for judgments the open corpus does not carry.

The open-data buckets hold the registry's metadata for far more matters than
they hold documents: about 94% of the 2026 Bombay civil digest has no judgment
PDF. Indian Kanoon has many of those texts.

This talks to their **official paid API**, not their website. Scraping the site
is what their terms prohibit, and the API exists precisely so that bulk use has
a sanctioned route; it needs a token from
https://api.indiankanoon.org and bills per call, so every method here is
counted and nothing is fetched speculatively.

The contract implemented below is taken from Indian Kanoon's own client
(github.com/sushant354/IKAPI), not inferred: requests are **POST** with their
parameters in the query string, authenticated with an ``Authorization: Token``
header.

Matching is the risk this module manages. A judgment is identified by a party
name and a date, and putting the wrong judgment's holding into the database
would be worse than leaving the row empty. So a candidate is accepted only when
the court and the decision date both agree; anything else is reported as no
match rather than taken as close enough.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Iterator

DEFAULT_HOST = "api.indiankanoon.org"

#: Indian Kanoon's own court codes, used in the ``doctypes:`` search filter.
#: These are their identifiers, not ours, and are not verifiable from the
#: published client — so they are overridable, and every result is checked
#: against the court name the API returns rather than trusted from the filter.
DOCTYPES = {
    "Bombay High Court": "bombay",
    "Supreme Court of India": "supremecourt",
}

#: Substrings that confirm the court of a returned document, matched against
#: the ``docsource`` field. This is the check that actually protects the data.
COURT_MARKERS = {
    "Bombay High Court": ("bombay",),
    "Supreme Court of India": ("supreme court",),
}


class KanoonError(RuntimeError):
    pass


class AuthError(KanoonError):
    pass


@dataclass
class Calls:
    """Billed API calls, tracked because this endpoint is not free."""

    searches: int = 0
    documents: int = 0

    @property
    def total(self) -> int:
        return self.searches + self.documents


@dataclass(frozen=True)
class Hit:
    docid: int
    title: str
    court: str
    date: str
    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.docid} {self.date} {self.title[:60]}"


@dataclass
class Match:
    """The outcome of looking for one judgment."""

    hit: Hit | None = None
    #: Why no match, when there is none. Recorded so a gap is explainable.
    reason: str = ""
    considered: list[Hit] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.hit is not None


class KanoonClient:
    """Minimal client for the endpoints this system needs."""

    def __init__(
        self,
        token: str,
        host: str = DEFAULT_HOST,
        *,
        min_interval: float = 0.2,
        timeout: int = 60,
    ):
        if not token:
            raise AuthError(
                "An Indian Kanoon API token is required. Get one at "
                "https://api.indiankanoon.org and pass it as INDIANKANOON_TOKEN."
            )
        self.host = host
        self.timeout = timeout
        # Paced rather than hammered: this is someone else's paid service and a
        # bulk backfill is a lot of requests.
        self.min_interval = min_interval
        self.calls = Calls()
        self._headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        }
        self._last = 0.0

    def _post(self, path: str, retries: int = 3) -> dict:
        for attempt in range(retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)

            conn = http.client.HTTPSConnection(self.host, timeout=self.timeout)
            try:
                # Their API is POST with arguments in the query string and no
                # body — unusual, but it is what the published client sends.
                conn.request("POST", path, headers=self._headers)
                response = conn.getresponse()
                body = response.read()
                status = response.status
            except Exception as err:  # noqa: BLE001
                if attempt == retries - 1:
                    raise KanoonError(f"{path}: {err}") from err
                time.sleep(2**attempt)
                continue
            finally:
                self._last = time.monotonic()
                conn.close()

            if status in (401, 403):
                raise AuthError(f"Indian Kanoon rejected the token ({status}).")
            if status == 429 or status >= 500:
                if attempt == retries - 1:
                    raise KanoonError(f"{path}: HTTP {status}")
                time.sleep(2**attempt)
                continue
            if status != 200:
                raise KanoonError(f"{path}: HTTP {status}: {body[:200]!r}")

            try:
                data = json.loads(body)
            except ValueError as err:
                raise KanoonError(f"{path}: response was not JSON") from err
            if isinstance(data, dict) and data.get("errmsg"):
                raise KanoonError(f"{path}: {data['errmsg']}")
            return data
        raise KanoonError(f"{path}: exhausted retries")

    def search(self, query: str, pagenum: int = 0) -> tuple[list[Hit], int]:
        """Run a search. Returns the page's hits and the total found."""
        path = f"/search/?formInput={urllib.parse.quote(query)}&pagenum={pagenum}"
        data = self._post(path)
        self.calls.searches += 1

        hits = [
            Hit(
                docid=int(d["tid"]),
                title=str(d.get("title", "")),
                court=str(d.get("docsource", "")),
                date=str(d.get("publishdate", "")),
            )
            for d in data.get("docs", [])
            if d.get("tid") is not None
        ]
        return hits, int(data.get("found", 0) or 0)

    def document(self, docid: int) -> str:
        """Fetch a judgment's text, with markup stripped."""
        data = self._post(f"/doc/{int(docid)}/")
        self.calls.documents += 1
        return _strip_markup(str(data.get("doc", "")))


def _strip_markup(html: str) -> str:
    """Reduce the API's HTML document body to plain text."""
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
        ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
    ):
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _to_ik_date(iso: str) -> str:
    """ISO ``YYYY-MM-DD`` to the ``DD-MM-YYYY`` their filters expect."""
    parts = iso.strip()[:10].split("-")
    return f"{parts[2]}-{parts[1]}-{parts[0]}" if len(parts) == 3 else iso


def _norm(text: str) -> set[str]:
    """Significant words of a party name, for comparing two renderings of it."""
    stop = {
        "v", "vs", "versus", "and", "ors", "anr", "others", "another", "the",
        "of", "in", "ltd", "limited", "pvt", "private", "co", "company",
        "state", "shri", "smt", "mr", "mrs", "m/s", "thr", "through", "its",
    }
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in stop}


def build_query(name: str, court: str, date: str, doctypes: dict[str, str] | None = None) -> str:
    """Build a search restricted to one court and one day.

    Pinning both ends of the date range to the decision date is what makes the
    match safe: it collapses the candidate set to the handful of judgments that
    court delivered that day.
    """
    codes = doctypes or DOCTYPES
    parts = [name.strip()]
    code = codes.get(court)
    if code:
        parts.append(f"doctypes: {code}")
    if date:
        ik = _to_ik_date(date)
        parts += [f"fromdate: {ik}", f"todate: {ik}"]
    return " ".join(parts)


def find_judgment(
    client: KanoonClient,
    name: str,
    court: str,
    date: str,
    *,
    min_overlap: float = 0.5,
    doctypes: dict[str, str] | None = None,
) -> Match:
    """Locate one judgment on Indian Kanoon, or explain why it was not found.

    Three things must agree before a candidate is accepted: the court, the
    decision date, and enough of the party names. A near-miss is rejected —
    attaching the wrong judgment's holding to a row would be a worse outcome
    than the empty cell it replaces.
    """
    if not name.strip():
        return Match(reason="no case name to search on")

    hits, _ = client.search(build_query(name, court, date, doctypes))
    if not hits:
        return Match(reason="no results for that name on that date")

    markers = COURT_MARKERS.get(court, ())
    wanted = _norm(name)
    best: tuple[float, Hit] | None = None

    for hit in hits:
        # The doctypes filter is their taxonomy, so it is not trusted on its
        # own; the returned court name is checked directly.
        if markers and not any(m in hit.court.lower() for m in markers):
            continue
        if date and hit.date[:10] and hit.date[:10] != date[:10]:
            continue

        overlap = _overlap(wanted, _norm(hit.title))
        if overlap >= min_overlap and (best is None or overlap > best[0]):
            best = (overlap, hit)

    if best is None:
        return Match(
            reason="results found, but none matched the court, date and parties",
            considered=hits[:5],
        )
    return Match(hit=best[1], considered=hits[:5])


def _overlap(wanted: set[str], got: set[str]) -> float:
    """Share of the expected party words present in a candidate's title."""
    if not wanted:
        return 0.0
    return len(wanted & got) / len(wanted)


def fetch_texts(
    client: KanoonClient,
    rows: Iterator[tuple[str, str, str, str]],
    *,
    doctypes: dict[str, str] | None = None,
) -> Iterator[tuple[str, str, Match]]:
    """For each ``(uid, name, court, date)``, yield ``(uid, text, match)``.

    The document is only fetched once a match has been verified, so a failed
    lookup costs one search rather than a search plus a wasted document call.
    """
    for uid, name, court, date in rows:
        try:
            match = find_judgment(client, name, court, date, doctypes=doctypes)
        except KanoonError as err:
            yield uid, "", Match(reason=f"search failed: {err}")
            continue
        if not match.found:
            yield uid, "", match
            continue
        try:
            text = client.document(match.hit.docid)
        except KanoonError as err:
            yield uid, "", Match(reason=f"document fetch failed: {err}")
            continue
        yield uid, text, match
