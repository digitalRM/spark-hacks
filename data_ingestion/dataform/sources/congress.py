"""congress.gov -> canonical dataform.

Requires a free api.data.gov key in CONGRESS_API_KEY (sign up at
https://api.data.gov/signup/) — api.congress.gov returned HTTP 403 without
one when probed live on 2026-08-15, confirming the key requirement; endpoint
shapes below follow the documented v3 API pattern
[VERIFY field names against https://api.congress.gov/ once you have a key]:
  GET /v3/bill/{congress}                    -> bills for a congress
  GET /v3/bill/{congress}/{billType}/{number} -> one bill's detail
  GET /v3/member                              -> members of Congress
  GET /v3/committee/{congress}                -> committees

Bills map to Proceeding(proceeding_type=BILL) + Document(doc_type=BILL) for
the latest text version; Members map to Person(role_types=[LEGISLATOR]);
Committees map to Organization(org_type=COMMITTEE) (see models.py's
"Legislation set" convenience-alias notes).
"""
from __future__ import annotations

from datetime import date
from typing import Iterator, List, Optional, Tuple

from dataform.config import CONGRESS_API_KEY, get_json, require_key
from dataform.models import (
    deterministic_id,
    Document,
    DocType,
    Organization,
    OrgType,
    Person,
    Proceeding,
    ProceedingType,
    RecordEnvelope,
    RoleType,
    SourceSystem,
)

BASE_URL = "https://api.congress.gov/v3"


def _key() -> str:
    return require_key(CONGRESS_API_KEY, "CONGRESS_API_KEY", "congress.gov")


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def fetch_bills(congress: int, limit: int = 20) -> List[dict]:
    data = get_json(
        f"{BASE_URL}/bill/{congress}",
        params={"api_key": _key(), "format": "json", "limit": min(limit, 250)},
    )
    return data.get("bills", [])[:limit]


def fetch_bill_pages(congress: int, start_url: Optional[str] = None) -> Iterator[Tuple[List[dict], Optional[str]]]:
    """Page-level generator for resumable/parallel loaders: yields (bills_in_page,
    next_page_url). Pass `start_url` (a previous `pagination.next`) to resume.
    List-level fields (title, introducedDate, latestAction, url) are sufficient
    for normalize_bill() -- no per-bill detail call needed (see docs/DATAFORM.md).

    congress.gov's own `pagination.next` URL does NOT include api_key [confirmed
    live 2026-08-16 -- following it verbatim 403s] -- api_key must be re-attached
    as a query param on every request, not just the first."""
    url = start_url or f"{BASE_URL}/bill/{congress}"
    params = {"api_key": _key(), "format": "json", "limit": 250} if not start_url else {"api_key": _key()}
    while url:
        data = get_json(url, params=params)
        yield data.get("bills", []), (data.get("pagination") or {}).get("next")
        url, params = (data.get("pagination") or {}).get("next"), {"api_key": _key()}


def fetch_member_pages(start_url: Optional[str] = None) -> Iterator[Tuple[List[dict], Optional[str]]]:
    """Page-level generator for /member, same resume contract as fetch_bill_pages."""
    url = start_url or f"{BASE_URL}/member"
    params = {"api_key": _key(), "format": "json", "limit": 250} if not start_url else {"api_key": _key()}
    while url:
        data = get_json(url, params=params)
        yield data.get("members", []), (data.get("pagination") or {}).get("next")
        url, params = (data.get("pagination") or {}).get("next"), {"api_key": _key()}


def fetch_bill_detail(congress: int, bill_type: str, number: str) -> dict:
    data = get_json(
        f"{BASE_URL}/bill/{congress}/{bill_type}/{number}",
        params={"api_key": _key(), "format": "json"},
    )
    return data.get("bill", {})


def fetch_members(limit: int = 20) -> List[dict]:
    data = get_json(
        f"{BASE_URL}/member",
        params={"api_key": _key(), "format": "json", "limit": min(limit, 250)},
    )
    return data.get("members", [])[:limit]


def normalize_bill(bill: dict) -> Tuple[Proceeding, Document]:
    congress = bill.get("congress")
    bill_type = (bill.get("type") or "").lower()
    number = str(bill.get("number", ""))
    bill_id = f"{congress}-{bill_type}-{number}"

    proceeding = Proceeding(
        envelope=RecordEnvelope(
            source_system=SourceSystem.CONGRESS,
            source_id=bill_id,
            id=deterministic_id(SourceSystem.CONGRESS.value, bill_id),
            source_url=(bill.get("url")),
            external_ids={"congress_bill_id": bill_id},
        ),
        proceeding_type=ProceedingType.BILL,
        number=f"{bill_type.upper()} {number}",
        title=bill.get("title"),
        date_filed=_parse_date((bill.get("introducedDate"))),
        status=(bill.get("latestAction") or {}).get("text"),
    )

    document = Document(
        envelope=RecordEnvelope(
            source_system=SourceSystem.CONGRESS,
            source_id=bill_id,
            id=deterministic_id(SourceSystem.CONGRESS.value, bill_id),
            source_url=bill.get("url"),
            external_ids={"congress_bill_id": bill_id},
        ),
        doc_type=DocType.BILL,
        title=bill.get("title"),
        date_issued=_parse_date(bill.get("introducedDate")),
        status=(bill.get("latestAction") or {}).get("text"),
        proceeding_id=proceeding.envelope.id,
    )

    return proceeding, document


def normalize_member(member: dict) -> Person:
    bioguide_id = member.get("bioguideId", "")
    name = member.get("name", "")
    parts = name.split(",")  # congress.gov often returns "Last, First"
    last = parts[0].strip() if parts else None
    first = parts[1].strip() if len(parts) > 1 else None
    return Person(
        envelope=RecordEnvelope(
            source_system=SourceSystem.CONGRESS,
            source_id=bioguide_id,
            id=deterministic_id(SourceSystem.CONGRESS.value, bioguide_id),
            source_url=member.get("url"),
            external_ids={"congress_bioguide_id": bioguide_id},
        ),
        role_types=[RoleType.LEGISLATOR],
        name_first=first,
        name_last=last,
        political_affiliations=[member.get("partyName")] if member.get("partyName") else [],
    )


def normalize_committee(committee: dict) -> Organization:
    system_code = committee.get("systemCode", "")
    return Organization(
        envelope=RecordEnvelope(
            source_system=SourceSystem.CONGRESS,
            source_id=system_code,
            id=deterministic_id(SourceSystem.CONGRESS.value, system_code),
            external_ids={"congress_committee_system_code": system_code},
        ),
        org_type=OrgType.COMMITTEE,
        name=committee.get("name", ""),
        jurisdiction=committee.get("chamber"),
    )


def fetch_and_normalize_bills(congress: int, limit: int = 20) -> Iterator[Tuple[Proceeding, Document]]:
    for bill in fetch_bills(congress, limit=limit):
        detail = fetch_bill_detail(congress, bill["type"].lower(), str(bill["number"]))
        yield normalize_bill(detail or bill)


if __name__ == "__main__":
    for proceeding, document in fetch_and_normalize_bills(118, limit=3):
        print(document.title, "|", proceeding.number, "|", proceeding.status)
