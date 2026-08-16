"""GovInfo (govinfo.gov) -> canonical dataform.

Requires a free api.data.gov key in GOVINFO_API_KEY (sign up at
https://api.data.gov/signup/) — GovInfo returned HTTP 401 without one when
probed live on 2026-08-15, confirming the key requirement; endpoint shapes
below follow the documented pattern at https://api.govinfo.gov/docs
[VERIFY field names against that page once you have a key, since it renders
as a JS app and couldn't be scraped for this pass]:
  GET /collections                                  -> list of collection codes
  GET /collections/{collection}/{startDate}          -> packages changed since startDate
  GET /packages/{packageId}/summary                  -> package metadata + download links
  GET /packages/{packageId}/granules                 -> sub-documents (e.g. bill sections)

GovInfo largely re-publishes content also reachable via eCFR/congress.gov/
CourtListener, so its role in the canonical model is primarily as an
additional `source_system` + scanned-PDF source (ImageAsset), cross-linked
via `external_ids["govinfo_package_id"]` rather than a new entity family.
"""
from __future__ import annotations

from datetime import date
from typing import Iterator, List, Optional, Tuple

from dataform.config import GOVINFO_API_KEY, get_json, require_key
from dataform.models import Document, DocType, RecordEnvelope, SourceSystem, deterministic_id

BASE_URL = "https://api.govinfo.gov"

_COLLECTION_TO_DOCTYPE = {
    "BILLS": DocType.BILL,
    "PLAW": DocType.STATUTE,
    "CFR": DocType.CFR_SECTION,
    "FR": DocType.FEDERAL_REGISTER_NOTICE,
    "CRPT": DocType.COMMITTEE_REPORT,
    "USCOURTS": DocType.OPINION,
}


def _key() -> str:
    return require_key(GOVINFO_API_KEY, "GOVINFO_API_KEY", "GovInfo")


def fetch_collections() -> List[dict]:
    data = get_json(f"{BASE_URL}/collections", params={"api_key": _key()})
    return data.get("collections", [])


def fetch_packages(collection: str, start_date: str, limit: int = 20) -> List[dict]:
    """start_date format: YYYY-MM-DDT00:00:00Z. offsetMark=* starts from the
    beginning of the result set -- required by this endpoint [confirmed live
    2026-08-16; the endpoint 400s without it, contrary to the docs pattern
    assumed at design time]."""
    data = get_json(
        f"{BASE_URL}/collections/{collection}/{start_date}",
        params={"api_key": _key(), "pageSize": min(limit, 100), "offsetMark": "*"},
    )
    return data.get("packages", [])[:limit]


def fetch_package_pages(
    collection: str, start_date: str, start_url: Optional[str] = None
) -> Iterator[Tuple[List[dict], Optional[str]]]:
    """Page-level generator for resumable/session-bounded loaders: yields
    (packages_in_page, next_page_url). Pass a previous `nextPage` as
    `start_url` to resume.

    Like congress.gov, GovInfo's own `nextPage` URL does NOT include api_key
    [confirmed live 2026-08-16 -- following it verbatim 401s] -- api_key must
    be re-attached as a query param on every request, not just the first."""
    url = start_url or f"{BASE_URL}/collections/{collection}/{start_date}"
    params = {"api_key": _key(), "pageSize": 100, "offsetMark": "*"} if not start_url else {"api_key": _key()}
    while url:
        data = get_json(url, params=params)
        yield data.get("packages", []), data.get("nextPage")
        url, params = data.get("nextPage"), {"api_key": _key()}


def fetch_package_summary(package_id: str) -> dict:
    return get_json(f"{BASE_URL}/packages/{package_id}/summary", params={"api_key": _key()})


def normalize_package(summary: dict) -> Document:
    collection_code = summary.get("collectionCode", "")
    doc_type = _COLLECTION_TO_DOCTYPE.get(collection_code, DocType.OTHER)
    package_id = summary.get("packageId", "")
    download = summary.get("download", {})
    doc = Document(
        envelope=RecordEnvelope(
            source_system=SourceSystem.GOVINFO,
            source_id=package_id,
            id=deterministic_id(SourceSystem.GOVINFO.value, package_id),
            source_url=summary.get("detailsLink") or summary.get("packageLink"),
            external_ids={"govinfo_package_id": package_id},
        ),
        doc_type=doc_type,
        title=summary.get("title"),
        date_issued=_parse_date(summary.get("dateIssued")),
        status="published",
        source_pdf_url=download.get("pdfLink") if isinstance(download, dict) else None,
    )
    return doc


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def fetch_documents(collection: str, start_date: str, limit: int = 20) -> Iterator[Document]:
    for pkg in fetch_packages(collection, start_date, limit=limit):
        summary = fetch_package_summary(pkg["packageId"])
        yield normalize_package(summary)


if __name__ == "__main__":
    for doc in fetch_documents("BILLS", "2024-01-01T00:00:00Z", limit=3):
        print(doc.title, "|", doc.doc_type, "|", doc.envelope.external_ids)
