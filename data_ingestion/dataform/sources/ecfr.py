"""eCFR (ecfr.gov) -> canonical dataform.

Uses the public eCFR REST API (no key required). Endpoints confirmed live
on 2026-08-15:
  GET /api/versioner/v1/titles.json                       -> all titles + amendment dates
  GET /api/versioner/v1/structure/{date}/title-{n}.json    -> nested hierarchy
  GET /api/admin/v1/agencies.json                          -> agencies + cfr_references
Full section text is served as XML at
  /api/versioner/v1/full/{date}/title-{n}.xml?part={part}  [VERIFY against
  ecfr.gov/developers before relying on the exact query params — text
  extraction here keeps only structural metadata, not parsed section body,
  since XML parsing is source-specific and out of scope for this pass].
"""
from __future__ import annotations

from typing import Iterator, List, Optional

from dataform.config import get_json
from dataform.models import (
    deterministic_id,
    Document,
    DocType,
    Organization,
    OrgType,
    RecordEnvelope,
    SourceSystem,
)

BASE_URL = "https://www.ecfr.gov/api"


def fetch_titles() -> List[dict]:
    data = get_json(f"{BASE_URL}/versioner/v1/titles.json")
    return data.get("titles", [])


def fetch_agencies() -> List[dict]:
    data = get_json(f"{BASE_URL}/admin/v1/agencies.json")
    return data.get("agencies", [])


def normalize_agency(row: dict, parent_id: Optional[str] = None) -> Organization:
    return Organization(
        envelope=RecordEnvelope(
            source_system=SourceSystem.ECFR,
            source_id=row.get("slug", row.get("short_name", row.get("name", ""))),
            id=deterministic_id(SourceSystem.ECFR.value, row.get("slug", row.get("short_name", row.get("name", "")))),
            external_ids={"ecfr_slug": row.get("slug", "")},
        ),
        org_type=OrgType.AGENCY,
        name=row.get("name", ""),
        short_name=row.get("short_name"),
        parent_org_id=parent_id,
    )


def iter_structure_nodes(node: dict, ancestors: Optional[List[str]] = None) -> Iterator[tuple]:
    """Walk a title's structure tree, yielding (node, hierarchy_path) for
    every leaf-ish node (section/part), depth-first."""
    ancestors = ancestors or []
    label = node.get("label_level") or node.get("label") or node.get("identifier", "")
    path = ancestors + [label]
    children = node.get("children") or []
    if node.get("type") in ("section", "part") or not children:
        yield node, " > ".join(path)
    for child in children:
        yield from iter_structure_nodes(child, path)


def fetch_title_structure(title_number: int, as_of: str) -> dict:
    return get_json(f"{BASE_URL}/versioner/v1/structure/{as_of}/title-{title_number}.json")


def normalize_structure_node(node: dict, title_number: int, hierarchy_path: str) -> Document:
    node_type = node.get("type", "section")
    doc_type = DocType.CFR_SECTION if node_type == "section" else DocType.OTHER
    identifier = node.get("identifier", "")
    return Document(
        envelope=RecordEnvelope(
            source_system=SourceSystem.ECFR,
            source_id=f"title-{title_number}-{node_type}-{identifier}",
            id=deterministic_id(SourceSystem.ECFR.value, f"title-{title_number}-{node_type}-{identifier}"),
            external_ids={"ecfr_title": str(title_number), "ecfr_identifier": identifier},
        ),
        doc_type=doc_type,
        title=node.get("label_description") or node.get("label"),
        citation=f"{title_number} CFR {identifier}" if node_type == "section" else None,
        jurisdiction="federal",
        status="published",
        hierarchy_path=hierarchy_path,
    )


def fetch_cfr_sections(title_number: int, as_of: str, limit: int = 20) -> Iterator[Document]:
    structure = fetch_title_structure(title_number, as_of)
    count = 0
    for node, path in iter_structure_nodes(structure):
        if node.get("type") != "section":
            continue
        yield normalize_structure_node(node, title_number, path)
        count += 1
        if count >= limit:
            return


if __name__ == "__main__":
    titles = fetch_titles()
    print(f"{len(titles)} titles")
    t1 = titles[0]
    for doc in fetch_cfr_sections(t1["number"], t1["up_to_date_as_of"], limit=3):
        print(doc.citation, "|", doc.title, "|", doc.hierarchy_path)
