"""One-shot enrichment re-pass over CourtListener opinion clusters + main-db indexes.

Why: the first bulk load left every opinion Document with jurisdiction=None,
issuing_body_id=None, citation=None and a one-line summary — so every court-scoped
("Ninth Circuit", "Texas") or citation-scoped ("citing Graham") query returned 0 by
construction. Rather than three separate 5M-row JSON rewrites, this re-streams the
opinion-clusters CSV ONCE and writes each Document with all of:

  jurisdiction        CourtListener court id of the docket's court ("ca9", "scotus", "texapp")
  issuing_body_id     Organization id of that court (deterministic_id("courtlistener", court_id))
  citation            first reporter cite for the cluster ("490 U.S. 386"), from cl_reporter_citation
  parallel_citations  the rest of the cluster's cites
  summary             syllabus | headnotes | posture | procedural_history | summary | disposition
                      (labelled sections; the executor's text fallback until opinion_text lands)

Inputs: proceeding + organization tables in the MAIN db (docket → court), and the
cl_reporter_citation staging table in the sibling api db (built by bulk_load_citations phase A).
Then adds VIRTUAL generated columns + indexes on the main db so the executor's exact filters
and joins are index lookups, not JSON scans:

  document:   jurisdiction, issuing_body_id, proceeding_id, citation, cl_cluster_id
  proceeding: organization_id, date_filed, proceeding_type
  citation:   citing_document_id, cited_document_id, citation_string

MUST be the only writer on the main db while it runs (~30 min). Resumable via the
courtlistener:bulk:opinion-clusters checkpoint (which it RESETS to 0 on --reset-checkpoint).

  python -m dataform.repass_clusters --api-db ~/amicus-dataform/data/dataform_api.db [--indexes-only]
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dataform.models import Document, SourceSystem, deterministic_id
from dataform.sources import courtlistener_bulk as clb
from dataform.store import Store

# --------------------------------------------------------------------------- #
# Enrichment maps consulted by the patched normalizer
# --------------------------------------------------------------------------- #
DOCKET_COURT: Dict[str, Tuple[str, str]] = {}   # docket_id -> (court_id, organization_id)
CLUSTER_CITES: Dict[str, List[str]] = {}         # cluster_id -> [cite, ...]

_SUMMARY_FIELDS = (("syllabus", "Syllabus"), ("headnotes", "Headnotes"), ("posture", "Posture"),
                   ("procedural_history", "Procedural history"), ("summary", "Summary"),
                   ("disposition", "Disposition"))
SUMMARY_MAX_CHARS = 6000


def build_summary(row: dict) -> Optional[str]:
    """Labelled concatenation of the cluster's descriptive fields, ≤ SUMMARY_MAX_CHARS. Free."""
    parts = []
    for col, label in _SUMMARY_FIELDS:
        v = (row.get(col) or "").strip()
        if v:
            parts.append(f"{label}: {v}" if len(parts) or col != "summary" else v)
    if not parts:
        return None
    return "\n\n".join(parts)[:SUMMARY_MAX_CHARS]


_orig_normalize = clb.normalize_opinion_cluster_row


def normalize_enriched(row: dict) -> Document:
    doc = _orig_normalize(row)
    docket_id = (row.get("docket_id") or "").strip()
    cluster_id = doc.envelope.source_id
    upd: Dict[str, object] = {"summary": build_summary(row)}
    court = DOCKET_COURT.get(docket_id)
    if court:
        upd["jurisdiction"], upd["issuing_body_id"] = court
    cites = CLUSTER_CITES.get(cluster_id)
    if cites:
        upd["citation"] = cites[0]
        upd["parallel_citations"] = cites[1:]
    return doc.model_copy(update=upd)


# --------------------------------------------------------------------------- #
# Maps
# --------------------------------------------------------------------------- #
def load_docket_court_map(conn: sqlite3.Connection) -> None:
    """proceeding.source_id (docket id) → (court_id, org id) via organization.source_id.
    Cost: one scan of proceeding (~72M rows ≈ 4–6 min) + a dict of the same size (~8 GB)."""
    t0 = time.time()
    orgs = {r[0]: r[1] for r in conn.execute(
        "SELECT id, source_id FROM organization WHERE source_system='courtlistener'")}
    print(f"[maps] {len(orgs):,} courts")
    n = 0
    interned: Dict[str, Tuple[str, str]] = {oid: (cid, oid) for oid, cid in orgs.items()}  # one tuple per court
    cur = conn.execute("SELECT source_id, json_extract(data,'$.organization_id') FROM proceeding "
                       "WHERE source_system='courtlistener'")
    while True:
        rows = cur.fetchmany(200_000)
        if not rows:
            break
        for did, oid in rows:
            t = interned.get(oid) if oid else None
            if did and t:
                DOCKET_COURT[did] = t
        n += len(rows)
        if n % 10_000_000 == 0:
            print(f"[maps] {n:,} dockets scanned ({time.time()-t0:.0f}s)")
    print(f"[maps] docket→court: {len(DOCKET_COURT):,} of {n:,} dockets in {time.time()-t0:.0f}s")


def load_cluster_cites(api_db: Path) -> None:
    t0 = time.time()
    c = sqlite3.connect(str(api_db))
    for cid, cite in c.execute("SELECT cluster_id, citation_string FROM cl_reporter_citation ORDER BY rowid"):
        CLUSTER_CITES.setdefault(cid, []).append(cite)
    c.close()
    print(f"[maps] cluster→cites: {len(CLUSTER_CITES):,} clusters in {time.time()-t0:.0f}s")


# --------------------------------------------------------------------------- #
# Indexes (generated columns over the JSON blobs)
# --------------------------------------------------------------------------- #
GENERATED = [
    ("document", "jurisdiction", "$.jurisdiction"),
    ("document", "issuing_body_id", "$.issuing_body_id"),
    ("document", "proceeding_id", "$.proceeding_id"),
    ("document", "citation", "$.citation"),
    ("document", "cl_cluster_id", "$.envelope.external_ids.courtlistener_cluster_id"),
    ("proceeding", "organization_id", "$.organization_id"),
    ("proceeding", "date_filed", "$.date_filed"),
    ("proceeding", "proceeding_type", "$.proceeding_type"),
    ("citation", "citing_document_id", "$.citing_document_id"),
    ("citation", "cited_document_id", "$.cited_document_id"),
    ("citation", "citation_string", "$.citation_string"),
]


def add_indexes(conn: sqlite3.Connection) -> None:
    """VIRTUAL generated columns (no data rewrite) + indexes. Idempotent. Cost: one index build
    per column (document ≈ 1–2 min each, proceeding ≈ 5–10 min each on 72M rows)."""
    for table, col, path in GENERATED:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})")}
        if col not in cols:
            t0 = time.time()
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT GENERATED ALWAYS AS "
                         f"(json_extract(data, '{path}')) VIRTUAL")
            print(f"[index] {table}.{col} generated column added ({time.time()-t0:.1f}s)")
        t0 = time.time()
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{col} ON {table}({col})")
        conn.commit()
        print(f"[index] idx_{table}_{col} ready ({time.time()-t0:.0f}s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-db", required=True, help="sibling db holding cl_reporter_citation")
    ap.add_argument("--indexes-only", action="store_true")
    ap.add_argument("--no-indexes", action="store_true")
    ap.add_argument("--reset-checkpoint", action="store_true", default=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    store = Store()
    print(f"main db: {store.db_path}")
    if not a.indexes_only:
        load_docket_court_map(store.conn)
        load_cluster_cites(Path(a.api_db).expanduser())
        clb.normalize_opinion_cluster_row = normalize_enriched  # patch in place
        from dataform import bulk_load_courtlistener as blc
        blc.normalize_opinion_cluster_row = normalize_enriched
        key = "courtlistener:bulk:opinion-clusters"
        if a.reset_checkpoint:
            store.set_checkpoint(key, "in_progress", records_done_delta=-store.get_checkpoint_records_done(key))
            store.conn.commit()
            print("[clusters] checkpoint reset to row 0 (full re-pass)")
        t0 = time.time()
        n = blc.load_file(store, "opinion-clusters", clb._bulk_url("opinion-clusters"), normalize_enriched,
                          limit=None if a.limit == 0 else a.limit)
        print(f"[clusters] re-pass wrote {n:,} documents in {time.time()-t0:.0f}s")
    if not a.no_indexes:
        add_indexes(store.conn)
    for q in ("SELECT COUNT(*) FROM document WHERE json_extract(data,'$.jurisdiction') IS NOT NULL",
              "SELECT COUNT(*) FROM document WHERE json_extract(data,'$.citation') IS NOT NULL",
              "SELECT COUNT(*) FROM document WHERE json_extract(data,'$.summary') IS NOT NULL"):
        print(q.split("'$.")[1].split("'")[0], store.conn.execute(q).fetchone()[0])
    print("done.")


if __name__ == "__main__":
    main()
