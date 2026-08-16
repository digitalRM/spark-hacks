"""CourtListener bulk CSV data -> canonical dataform.

The REST API caps anonymous access at 50 requests/hour (confirmed live
2026-08-16 via the API's own error message) -- effectively unusable for real
volume. CourtListener also publishes free, no-account-required quarterly
snapshots of their full database as bz2-compressed CSVs on a public S3
bucket (https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/,
documented at wiki.free.law/c/courtlistener/help/api/bulk-data/bulk-legal-data)
-- a plain file download has no such rate limit.

Streams directly from HTTP through bz2 decompression into a CSV reader --
never buffers the (multi-GB uncompressed) file to disk. Cross-references
(Document.proceeding_id, Proceeding.organization_id) are computed via
deterministic_id() from the raw foreign-key column, so no in-memory join or
second pass over another file is needed.
"""
from __future__ import annotations

import bz2
import csv
import sys
from datetime import date
from typing import Iterator, Optional

import requests

# CourtListener's bulk CSVs are PostgreSQL `COPY ... CSV ESCAPE '\'` output --
# quotes inside a quoted field are backslash-escaped (\"), not RFC4180-doubled
# (""), which is what Python's csv module assumes by default. Left unfixed,
# any row containing an escaped quote (very common: the "headmatter" field on
# opinion-clusters is raw HTML, "case_name_full" contains quoted names, etc.)
# gets its row boundary detected at the wrong place -- confirmed live
# 2026-08-16: 64.5% of opinion-cluster rows were misaligned this way, silently
# writing garbage source_ids (e.g. "    -1559.", a fragment of some other
# column) instead of failing loudly. doublequote=False + escapechar='\\' below
# is the fix; field_size_limit must also be raised since the same headmatter
# field is large enough to exceed Python's default 131072-char cap once it's
# being parsed as one field instead of being split apart by the bug.
csv.field_size_limit(sys.maxsize)

from dataform.models import (
    Document,
    DocType,
    FinancialDisclosure,
    ImageAsset,
    MediaBundle,
    Organization,
    OrgType,
    Person,
    Proceeding,
    ProceedingType,
    RecordEnvelope,
    RoleType,
    SourceSystem,
    TextAsset,
    deterministic_id,
)

BUCKET = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/bulk-data"

# financial-disclosures.csv's thumbnail/filepath columns are relative paths
# (e.g. "us/federal/judicial/financial-disclosures/2084/...-thumbnail_1.png")
# -- confirmed live 2026-08-16 that storage.courtlistener.com serves these
# directly, same host as the opinion-cluster PDF links above.
DISCLOSURE_BASE_URL = "https://storage.courtlistener.com"
SNAPSHOT_DATE = "2026-06-30"  # latest quarterly snapshot confirmed live 2026-08-16

# opinion-clusters.csv's filepath_pdf_harvard column is a relative path, not a
# full URL (e.g. "harvard_pdf/7290305.pdf") -- confirmed live 2026-08-16 that
# storage.courtlistener.com serves these directly (200, application/pdf) and
# is populated for ~64% of rows in a 2,000-row sample.
PDF_BASE_URL = "https://storage.courtlistener.com"


def _bulk_url(name: str) -> str:
    return f"{BUCKET}/{name}-{SNAPSHOT_DATE}.csv.bz2"


def stream_csv_rows(url: str, limit: Optional[int] = None, skip: int = 0) -> Iterator[dict]:
    """Stream a bz2-compressed CSV straight from HTTP -> decompression ->
    csv.DictReader, never touching disk with the (huge) decompressed content.

    `skip` re-parses (but doesn't yield) the first `skip` rows -- there's no
    HTTP range / bz2 block-seek shortcut here, so a resume still pays the full
    download+decompress cost up to that point, but it avoids re-doing the
    downstream normalize+DB-write work for rows already saved (see load_file's
    retry loop, which needs this after a connection drop mid-stream)."""
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with bz2.open(resp.raw, mode="rt", encoding="utf-8", errors="replace") as text_stream:
        reader = csv.DictReader(text_stream, doublequote=False, escapechar="\\")
        for i, row in enumerate(reader):
            if i < skip:
                continue
            if limit is not None and i >= limit:
                return
            yield row
    resp.close()


def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _require_numeric_id(row: dict, label: str) -> str:
    """Defense-in-depth against the CSV-misalignment bug documented above: a
    genuinely misaligned row still parses without error, it just puts the
    wrong *value* in the id column -- this catches that case explicitly
    rather than silently writing a document with a garbage source_id, which
    is what happened before this check existed (confirmed live 2026-08-16)."""
    raw = (row.get("id") or "").strip()
    if not raw.isdigit():
        raise ValueError(f"{label}: 'id' column isn't numeric ({raw!r}) -- row is likely misaligned")
    return raw


def normalize_court_row(row: dict) -> Organization:
    court_id = row["id"]
    return Organization(
        envelope=RecordEnvelope(
            source_system=SourceSystem.COURTLISTENER,
            source_id=court_id,
            id=deterministic_id(SourceSystem.COURTLISTENER.value, court_id),
            source_url=row.get("url") or None,
            external_ids={"courtlistener_court_id": court_id},
        ),
        org_type=OrgType.COURT,
        name=row.get("full_name") or court_id,
        short_name=row.get("short_name") or None,
        jurisdiction=row.get("jurisdiction") or None,
        url=row.get("url") or None,
    )


def normalize_person_row(row: dict) -> Person:
    person_id = _require_numeric_id(row, "person")
    return Person(
        envelope=RecordEnvelope(
            source_system=SourceSystem.COURTLISTENER,
            source_id=person_id,
            id=deterministic_id(SourceSystem.COURTLISTENER.value, person_id),
            external_ids={"courtlistener_person_id": person_id},
        ),
        role_types=[RoleType.JUDGE],
        name_first=row.get("name_first") or None,
        name_middle=row.get("name_middle") or None,
        name_last=row.get("name_last") or None,
        name_suffix=row.get("name_suffix") or None,
        date_of_birth=_parse_date(row.get("date_dob", "")),
        date_of_death=_parse_date(row.get("date_dod", "")),
        gender=row.get("gender") or None,
    )


def normalize_docket_row(row: dict) -> Proceeding:
    docket_id = _require_numeric_id(row, "docket")
    court_id = row.get("court_id") or ""
    return Proceeding(
        envelope=RecordEnvelope(
            source_system=SourceSystem.COURTLISTENER,
            source_id=docket_id,
            id=deterministic_id(SourceSystem.COURTLISTENER.value, docket_id),
            external_ids={"courtlistener_docket_id": docket_id},
        ),
        proceeding_type=ProceedingType.CASE,
        number=row.get("docket_number") or "",
        title=row.get("case_name") or row.get("case_name_short") or None,
        # computed, not joined -- same deterministic formula normalize_court_row used
        organization_id=deterministic_id(SourceSystem.COURTLISTENER.value, court_id) if court_id else None,
        date_filed=_parse_date(row.get("date_filed", "")),
        date_terminated=_parse_date(row.get("date_terminated", "")),
        nature_of_suit=row.get("nature_of_suit") or None,
    )


def normalize_opinion_cluster_row(row: dict) -> Document:
    cluster_id = _require_numeric_id(row, "opinion-cluster")
    docket_id = row.get("docket_id") or ""
    pdf_path = row.get("filepath_pdf_harvard") or ""
    return Document(
        envelope=RecordEnvelope(
            source_system=SourceSystem.COURTLISTENER,
            source_id=cluster_id,
            id=deterministic_id(SourceSystem.COURTLISTENER.value, cluster_id),
            external_ids={"courtlistener_cluster_id": cluster_id},
        ),
        doc_type=DocType.OPINION,
        title=row.get("case_name") or row.get("case_name_short") or None,
        date_issued=_parse_date(row.get("date_filed", "")),
        status=row.get("precedential_status") or None,
        summary=row.get("summary") or None,
        source_pdf_url=f"{PDF_BASE_URL}/{pdf_path}" if pdf_path else None,
        # computed, not joined -- same deterministic formula normalize_docket_row used
        proceeding_id=deterministic_id(SourceSystem.COURTLISTENER.value, docket_id) if docket_id else None,
    )


def normalize_financial_disclosure_row(row: dict) -> FinancialDisclosure:
    disclosure_id = _require_numeric_id(row, "financial-disclosure")
    person_id = row.get("person_id") or ""
    thumbnail_path = row.get("thumbnail") or ""
    year_raw = row.get("year") or ""
    page_count_raw = row.get("page_count") or ""
    images = []
    if thumbnail_path:
        images.append(
            ImageAsset(
                page_number=1,
                image_ref=f"{DISCLOSURE_BASE_URL}/{thumbnail_path}",
                caption=f"{year_raw} financial disclosure cover page" if year_raw else "financial disclosure cover page",
            )
        )
    return FinancialDisclosure(
        envelope=RecordEnvelope(
            source_system=SourceSystem.COURTLISTENER,
            source_id=disclosure_id,
            id=deterministic_id(SourceSystem.COURTLISTENER.value, disclosure_id),
            external_ids={"courtlistener_disclosure_id": disclosure_id},
        ),
        person_id=deterministic_id(SourceSystem.COURTLISTENER.value, person_id) if person_id else "",
        year=int(year_raw) if year_raw.isdigit() else None,
        # download_filepath is already a full URL, unlike thumbnail/filepath
        filepath=row.get("download_filepath") or None,
        page_count=int(page_count_raw) if page_count_raw.isdigit() else None,
        media=MediaBundle(images=images),
        is_amended=(row.get("is_amended") or "").strip().lower() == "t",
    )


if __name__ == "__main__":
    print("courts (first 3):")
    for row in stream_csv_rows(_bulk_url("courts"), limit=3):
        print(" ", normalize_court_row(row).name)
    print("opinion-clusters (first 3):")
    for row in stream_csv_rows(_bulk_url("opinion-clusters"), limit=3):
        doc = normalize_opinion_cluster_row(row)
        print(" ", doc.title, "|", doc.date_issued, "|", doc.status)
