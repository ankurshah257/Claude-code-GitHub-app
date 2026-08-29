"""Reading the open judgment datasets from S3.

The court websites (sci.gov.in, bombayhighcourt.nic.in, the eCourts portal) are
not used. They are CAPTCHA-gated, rate-limited, and hostile to enumeration. The
same judgments are published as open datasets in two public S3 buckets, already
extracted and partitioned, which makes a complete scan a matter of listing keys
rather than of defeating a scraper.

Anonymous HTTP is used rather than boto3: these buckets allow unauthenticated
ListObjectsV2, so the dependency would buy nothing but credential resolution
that would then need to be disabled.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterator

HIGH_COURT_BUCKET = "https://s3.ap-south-1.amazonaws.com/indian-high-court-judgments"
SUPREME_COURT_BUCKET = "https://s3.ap-south-1.amazonaws.com/indian-supreme-court-judgments"

_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


class S3Error(RuntimeError):
    pass


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int


def _get(url: str, timeout: int, retries: int = 4) -> bytes:
    """Fetch a URL, retrying on transient failure with exponential backoff.

    A scan makes thousands of these calls, so a single blip must not abort it.
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except Exception as err:  # noqa: BLE001 - retried and re-raised below
            last = err
            if attempt < retries - 1:
                time.sleep(2**attempt)
    raise S3Error(f"Could not fetch {url}: {last}")


def list_objects(
    bucket: str, prefix: str, delimiter: str = "", timeout: int = 60
) -> Iterator[S3Object | str]:
    """Page through ListObjectsV2, yielding objects, or prefixes when delimited.

    Continuation is followed to exhaustion. A partition with more than 1000
    keys is normal here, and stopping at the first page would silently return a
    fraction of a bench-year while looking like a complete scan.
    """
    token: str | None = None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token

        body = _get(f"{bucket}/?{urllib.parse.urlencode(params)}", timeout)
        root = ET.fromstring(body)

        if delimiter:
            for node in root.findall("s3:CommonPrefixes/s3:Prefix", _NS):
                if node.text:
                    yield node.text
        for node in root.findall("s3:Contents", _NS):
            key = node.findtext("s3:Key", namespaces=_NS)
            size = node.findtext("s3:Size", default="0", namespaces=_NS)
            if key:
                yield S3Object(key, int(size or 0))

        if root.findtext("s3:IsTruncated", namespaces=_NS) != "true":
            return
        token = root.findtext("s3:NextContinuationToken", namespaces=_NS)
        if not token:
            return


def fetch(bucket: str, key: str, timeout: int = 300) -> bytes:
    """Download one object."""
    return _get(f"{bucket}/{urllib.parse.quote(key)}", timeout)


def list_partition_values(bucket: str, prefix: str, field: str) -> list[str]:
    """List the values of a Hive-style partition field directly under `prefix`.

    e.g. ``list_partition_values(HIGH_COURT_BUCKET, "metadata/parquet/", "year")``
    returns every year the corpus covers, read from the bucket rather than
    assumed from a hardcoded range.
    """
    values: list[str] = []
    for item in list_objects(bucket, prefix, delimiter="/"):
        if not isinstance(item, str):
            continue
        tail = item.rstrip("/").rsplit("/", 1)[-1]
        if tail.startswith(f"{field}="):
            values.append(tail.split("=", 1)[1])
    return sorted(values)
