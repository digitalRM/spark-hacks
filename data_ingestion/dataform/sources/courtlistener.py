"""CourtListener / Free Law Project -> canonical dataform.

Uses the public REST API v4 (https://www.courtlistener.com/api/rest/v4/).
Anonymous access works for /search/, /courts/, and /people/; endpoints like
/dockets/ and /opinions/ (individual detail records) require an auth token
[VERIFY: re-check per-endpoint auth requirements against
courtlistener.com/help/api/rest/ before relying on this in production —
observed live on 2026-08-15].

Covers the case-law core of the CourtListener ERD end-to-end: Document
(opinion), Proceeding (docket), Organization (court), Person (judge),
Citation. Financial disclosures, parties/attorneys, and visualizations are
modeled in dataform.models but not yet wired to a live endpoint here — same
get_json() pattern extends to them (see docs/DATAFORM.md open questions).
"""
from __future__ import annotations

from datetime import date
from typing import Iterator, List, Optional, Tuple

from dataform.config import COURTLISTENER_API_TOKEN, get_json
from dataform.models import (
    Citation,
    CitationTreatment,
    deterministic_id,
    Document,
    DocType,
    Organization,
    OrgType,
    Person,
    ProceedingType,
    Proceeding,
    RecordEnvelope,
    RoleType,
    SourceSystem,
    TextAsset,
)

BASE_URL = "https://www.courtlistener.com/api/rest/v4"


def _headers() -> dict:
    if COURTLISTENER_API_TOKEN:
        return {"Authorization": f"Token {COURTLISTENER_API_TOKEN}"}
    return {}


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def fetch_opinion_pages(
    query: str = "", court: Optional[str] = None, start_url: Optional[str] = None
) -> Iterator[Tuple[List[dict], Optional[str]]]:
    """Page-level generator for resumable/parallel loaders: yields
    (results_in_this_page, next_page_url) so a caller can checkpoint the
    cursor after each page instead of consuming the whole stream at once.
    Pass `start_url` (a previously-seen `next` URL) to resume mid-stream."""
    if start_url:
        url, params = start_url, None
    else:
        url = f"{BASE_URL}/search/"
        params = {"type": "o", "page_size": 20}
        if query:
            params["q"] = query
        if court:
            params["court"] = court
    while url:
        data = get_json(url, params=params, headers=_headers())
        yield data.get("results", []), data.get("next")
        url, params = data.get("next"), None


def fetch_person_pages(start_url: Optional[str] = None) -> Iterator[Tuple[List[dict], Optional[str]]]:
    """Page-level generator for /people/, same resume contract as fetch_opinion_pages."""
    if start_url:
        url, params = start_url, None
    else:
        url = f"{BASE_URL}/people/"
        params = {"page_size": 20}
    while url:
        data = get_json(url, params=params, headers=_headers())
        yield data.get("results", []), data.get("next")
        url, params = data.get("next"), None


def fetch_opinions(query: str = "", court: Optional[str] = None, limit: int = 20) -> Iterator[dict]:
    """Iterate raw search results (type=o, opinions) up to `limit`."""
    params = {"type": "o", "page_size": min(limit, 20)}
    if query:
        params["q"] = query
    if court:
        params["court"] = court
    url = f"{BASE_URL}/search/"
    fetched = 0
    while url and fetched < limit:
        data = get_json(url, params=params if fetched == 0 else None, headers=_headers())
        for result in data.get("results", []):
            yield result
            fetched += 1
            if fetched >= limit:
                break
        url = data.get("next")
        params = None


def normalize_search_result(result: dict) -> tuple:
    """One CourtListener /search/ (type=o) hit -> (Document, Proceeding, Organization)."""
    court_id = result.get("court_id") or "unknown"
    organization = Organization(
        envelope=RecordEnvelope(
            source_system=SourceSystem.COURTLISTENER,
            source_id=court_id,
            id=deterministic_id(SourceSystem.COURTLISTENER.value, court_id),
            source_url=f"https://www.courtlistener.com/?court={court_id}",
        ),
        org_type=OrgType.COURT,
        name=result.get("court") or court_id,
        short_name=result.get("court_citation_string"),
        jurisdiction=court_id,
    )

    docket_id = str(result.get("docket_id") or result.get("cluster_id"))
    proceeding = Proceeding(
        envelope=RecordEnvelope(
            source_system=SourceSystem.COURTLISTENER,
            source_id=docket_id,
            id=deterministic_id(SourceSystem.COURTLISTENER.value, docket_id),
            source_url=result.get("absolute_url"),
            external_ids={"courtlistener_docket_id": docket_id},
        ),
        proceeding_type=ProceedingType.CASE,
        number=result.get("docketNumber") or "",
        title=result.get("caseName"),
        organization_id=organization.envelope.id,
        date_filed=_parse_date(result.get("dateFiled")),
    )

    cluster_id = str(result.get("cluster_id"))
    opinions = result.get("opinions") or [{}]
    snippet = opinions[0].get("snippet") if opinions else None
    document = Document(
        envelope=RecordEnvelope(
            source_system=SourceSystem.COURTLISTENER,
            source_id=cluster_id,
            id=deterministic_id(SourceSystem.COURTLISTENER.value, cluster_id),
            source_url=f"https://www.courtlistener.com{result.get('absolute_url', '')}",
            external_ids={
                "courtlistener_cluster_id": cluster_id,
                "courtlistener_opinion_id": str(opinions[0].get("id")) if opinions else "",
            },
        ),
        doc_type=DocType.OPINION,
        title=result.get("caseName"),
        citation=(result.get("citation") or [None])[0],
        parallel_citations=(result.get("citation") or [])[1:],
        jurisdiction=court_id,
        issuing_body_id=organization.envelope.id,
        date_issued=_parse_date(result.get("dateFiled")),
        source_pdf_url=opinions[0].get("download_url") if opinions else None,
        proceeding_id=proceeding.envelope.id,
    )
    document.media.text = TextAsset(plain_text=snippet)

    return document, proceeding, organization


def normalize_person(row: dict) -> Person:
    """One /people/ row (live API or a file dump of the same shape) -> Person."""
    person_id = str(row.get("id"))
    return Person(
        envelope=RecordEnvelope(
            source_system=SourceSystem.COURTLISTENER,
            source_id=person_id,
            id=deterministic_id(SourceSystem.COURTLISTENER.value, person_id),
            source_url=row.get("resource_uri"),
            external_ids={"courtlistener_person_id": person_id},
        ),
        role_types=[RoleType.JUDGE],
        name_first=row.get("name_first"),
        name_middle=row.get("name_middle"),
        name_last=row.get("name_last"),
        name_suffix=row.get("name_suffix"),
        date_of_birth=_parse_date(row.get("date_dob")),
        date_of_death=_parse_date(row.get("date_dod")),
        gender=row.get("gender"),
    )


def fetch_judges(limit: int = 20) -> Iterator[Person]:
    url = f"{BASE_URL}/people/"
    params = {"page_size": min(limit, 20)}
    fetched = 0
    while url and fetched < limit:
        data = get_json(url, params=params if fetched == 0 else None, headers=_headers())
        for row in data.get("results", []):
            yield normalize_person(row)
            fetched += 1
            if fetched >= limit:
                break
        url = data.get("next")
        params = None


def fetch_citations_for_result(result: dict, citing_document_id: str) -> Iterator[Citation]:
    """CourtListener search results don't expose resolved citation edges without
    the /opinions-cited/ endpoint (auth-gated); this yields Citation stubs
    from the result's own citation strings as a placeholder pattern."""
    for cite in result.get("citation") or []:
        yield Citation(
            envelope=RecordEnvelope(
                source_system=SourceSystem.COURTLISTENER,
                source_id=f"{result.get('cluster_id')}:{cite}",
                id=deterministic_id(SourceSystem.COURTLISTENER.value, f"{result.get('cluster_id')}:{cite}"),
            ),
            citing_document_id=citing_document_id,
            citation_string=cite,
            treatment=CitationTreatment.CITED,
        )


if __name__ == "__main__":
    for r in fetch_opinions(query="graham v connor", limit=2):
        doc, proc, org = normalize_search_result(r)
        print(doc.title, "|", doc.citation, "|", org.name, "|", proc.number)
