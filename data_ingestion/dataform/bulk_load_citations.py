"""Load CourtListener CITATION data from the bulk CSV snapshots -- fills the `citation` table.

Three bulk files (same snapshot/bucket as bulk_load_courtlistener):

  citations-<date>.csv.bz2     (~0.13 GB)  cluster_id -> volume/reporter/page   ("490 U.S. 386")
  opinions-<date>.csv.bz2      (~55 GB)    opinion id -> cluster_id (+ full text, which we DON'T store here)
  citation-map-<date>.csv.bz2  (~0.53 GB)  citing_opinion_id -> cited_opinion_id, depth   (the graph)

Documents in this corpus are keyed by CLUSTER id (deterministic_id("courtlistener", cluster_id)),
while citation-map edges are keyed by OPINION id, so the graph only joins to documents through
the opinion->cluster mapping that lives in the big opinions file. Phases:

  A  citations     -> staging table cl_reporter_citation(cluster_id, citation_string, type)
                      + in-memory cluster -> "first cite" dict (used for Citation.citation_string)
  B  opinions      -> opinion_id -> cluster_id map, streamed ID-ONLY (text discarded), persisted to
                      data/opinion_to_cluster.tsv so it is never re-streamed; resumable by row offset
  C  citation-map  -> Citation records (citing/cited document ids resolved via B; unresolvable ids
                      keep an "opinion:<id>" placeholder + raw ids in envelope.external_ids)
                      + staging table cl_citation_map(citing_opinion_id, cited_opinion_id, depth)

Attaching reporter cites to Document.citation is a separate post-pass (attach_citations_to_documents)
because documents live in the main db, which has ONE writer (the docket/cluster bulk loader).

Idempotent + checkpoint-resumable like the other loaders (keys courtlistener:bulk:citations,
courtlistener:bulk:opinion-id-map, courtlistener:bulk:citation-map). Run with DATAFORM_DB pointing
at the sibling db to keep the main db single-writer:

  DATAFORM_DB=~/amicus-dataform/data/dataform_api.db python -m dataform.bulk_load_citations
  ... --limit 0 (default, unbounded)   --skip-opinions (edges keep opinion-id placeholders)
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from dataform.models import Citation, RecordEnvelope, SourceSystem, deterministic_id
from dataform.sources.courtlistener_bulk import _bulk_url, stream_csv_rows
from dataform.store import Store

csv.field_size_limit(sys.maxsize)  # opinion text fields are huge

OPINION_MAP_FILE = "opinion_to_cluster.tsv"
# Opinion text captured during the opinions pass (first N chars; the executor judges
# keyword-centred excerpts, it does not need the whole opinion). Env override for N.
import html as _html
import os as _os
import re as _re
TEXT_CHARS = int(_os.environ.get("DATAFORM_TEXT_CHARS", "20000"))
_TAG_RE = _re.compile(r"<[^>]+>")
_WS_RE = _re.compile(r"[ \t\r\f\v]+")
_NL_RE = _re.compile(r"\n{3,}")
_TEXT_COLS = ("plain_text", "html_with_citations", "html", "html_lawbox", "html_columbia", "html_anon_2020",
              "xml_harvard", "xml_scan")


def _opinion_text(row: dict) -> str:
    """Best available text for an opinion row: plain_text, else the first non-empty html/xml
    column with tags stripped. Truncated to TEXT_CHARS. Cost: regex over ≤ a few hundred KB."""
    for col in _TEXT_COLS:
        raw = row.get(col) or ""
        if not raw.strip():
            continue
        if col != "plain_text":
            raw = _html.unescape(_TAG_RE.sub(" ", raw))
        raw = _NL_RE.sub("\n\n", _WS_RE.sub(" ", raw)).strip()
        if raw:
            return raw[:TEXT_CHARS]
    return ""


def _staging_schema(store: Store) -> None:
    store.conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cl_reporter_citation (
            cluster_id TEXT NOT NULL, citation_string TEXT NOT NULL, cite_type TEXT,
            PRIMARY KEY (cluster_id, citation_string));
        CREATE INDEX IF NOT EXISTS idx_cl_reporter_citation_cite ON cl_reporter_citation(citation_string);
        CREATE TABLE IF NOT EXISTS cl_citation_map (
            citing_opinion_id TEXT NOT NULL, cited_opinion_id TEXT NOT NULL, depth INTEGER,
            PRIMARY KEY (citing_opinion_id, cited_opinion_id));
        CREATE INDEX IF NOT EXISTS idx_cl_citation_map_cited ON cl_citation_map(cited_opinion_id);
        CREATE TABLE IF NOT EXISTS cl_opinion_cluster (
            opinion_id TEXT PRIMARY KEY, cluster_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS opinion_text (
            opinion_id TEXT PRIMARY KEY, cluster_id TEXT NOT NULL, opinion_type TEXT, author_str TEXT,
            n_chars INTEGER, plain_text TEXT);
        CREATE INDEX IF NOT EXISTS idx_opinion_text_cluster ON opinion_text(cluster_id);
        """
    )
    store.conn.commit()


def stream_csv_rows_fast(url: str, skip: int = 0):
    """Like courtlistener_bulk.stream_csv_rows but decompresses with lbzip2 (parallel bz2) when
    available: ``curl url | lbzip2 -dc -n <cores>`` → csv.DictReader. Measured on the Spark:
    2× the single-thread python-bz2 path on the opinions file (parse becomes the limiter).
    Falls back to stream_csv_rows if lbzip2 is missing. Cost: network-bound (≈ file size / link speed)."""
    import io
    import shutil
    import subprocess
    lb = shutil.which("lbzip2") or (str(Path.home() / "lbzip2/usr/bin/lbzip2")
                                    if (Path.home() / "lbzip2/usr/bin/lbzip2").exists() else None)
    if not lb:
        yield from stream_csv_rows(url, skip=skip)
        return
    curl = subprocess.Popen(["curl", "-s", "--retry", "8", "--retry-delay", "5", url], stdout=subprocess.PIPE)
    lbz = subprocess.Popen([lb, "-dc", "-n", str(max(2, min(16, (_os.cpu_count() or 4) - 2)))],
                           stdin=curl.stdout, stdout=subprocess.PIPE, bufsize=1 << 22)
    curl.stdout.close()
    text = io.TextIOWrapper(lbz.stdout, encoding="utf-8", errors="replace", newline="")
    reader = csv.DictReader(text, doublequote=False, escapechar="\\")
    for i, row in enumerate(reader):
        if i < skip:
            continue
        yield row
    lbz.wait(); curl.wait()


def _cite_string(row: dict) -> Optional[str]:
    vol, rep, page = (row.get("volume") or "").strip(), (row.get("reporter") or "").strip(), (row.get("page") or "").strip()
    if not (vol and rep and page):
        return None
    return f"{vol} {rep} {page}"


# --------------------------------------------------------------------------- #
# Phase A — reporter citations (cluster -> "490 U.S. 386")
# --------------------------------------------------------------------------- #
def load_reporter_citations(store: Store, limit: Optional[int]) -> Dict[str, str]:
    key = "courtlistener:bulk:citations"
    first_cite: Dict[str, str] = {}
    if store.get_checkpoint(key) == "complete":
        print("[citations] already complete; loading cluster->cite dict from staging table")
        for cid, cite in store.conn.execute(
                "SELECT cluster_id, citation_string FROM cl_reporter_citation ORDER BY rowid"):
            first_cite.setdefault(cid, cite)
        return first_cite
    done = store.get_checkpoint_records_done(key)
    if done:
        print(f"[citations] resuming from row {done:,}")
        for cid, cite in store.conn.execute("SELECT cluster_id, citation_string FROM cl_reporter_citation ORDER BY rowid"):
            first_cite.setdefault(cid, cite)
    t0, n, batch = time.time(), done, []
    for row in stream_csv_rows(_bulk_url("citations"), limit=limit, skip=done):
        cite = _cite_string(row)
        cid = (row.get("cluster_id") or "").strip()
        n += 1
        if cite and cid.isdigit():
            batch.append((cid, cite, row.get("type")))
            first_cite.setdefault(cid, cite)
        if len(batch) >= 5000:
            store.conn.executemany("INSERT OR IGNORE INTO cl_reporter_citation VALUES (?,?,?)", batch)
            store.set_checkpoint(key, "in_progress", records_done_delta=n - store.get_checkpoint_records_done(key))
            store.conn.commit()
            batch = []
            if n % 500000 < 5000:
                print(f"[citations] {n:,} rows in {time.time()-t0:.0f}s ({n/(time.time()-t0):.0f} rows/s)")
    if batch:
        store.conn.executemany("INSERT OR IGNORE INTO cl_reporter_citation VALUES (?,?,?)", batch)
    store.set_checkpoint(key, "complete", records_done_delta=n - store.get_checkpoint_records_done(key))
    store.conn.commit()
    print(f"[citations] COMPLETE: {n:,} rows, {len(first_cite):,} clusters with a cite, {time.time()-t0:.0f}s")
    return first_cite


# --------------------------------------------------------------------------- #
# Phase B — opinion id -> cluster id (streams the 55 GB opinions file, ids only)
# --------------------------------------------------------------------------- #
def load_opinion_map(store: Store, limit: Optional[int]) -> Dict[str, str]:
    key = "courtlistener:bulk:opinion-id-map"
    map_path = store.db_path.parent / OPINION_MAP_FILE
    op2cl: Dict[str, str] = {}
    if map_path.exists():
        with open(map_path) as fh:
            for line in fh:
                a, _, b = line.rstrip("\n").partition("\t")
                if a and b:
                    op2cl[a] = b
        print(f"[opinion-id-map] loaded {len(op2cl):,} ids from {map_path.name}")
    if store.get_checkpoint(key) == "complete":
        return op2cl
    done = store.get_checkpoint_records_done(key)
    print(f"[opinion-id-map] streaming opinions.csv (ids only){' from row %s' % f'{done:,}' if done else ''} — "
          f"this is the 55 GB file; expect an hour or more")
    t0, n, with_text, batch = time.time(), done, 0, []
    with open(map_path, "a") as fh:
        for row in stream_csv_rows_fast(_bulk_url("opinions"), skip=done):
            if limit is not None and n - done >= limit:
                break
            n += 1
            oid, cid = (row.get("id") or "").strip(), (row.get("cluster_id") or "").strip()
            if oid.isdigit() and cid.isdigit():
                op2cl[oid] = cid
                fh.write(f"{oid}\t{cid}\n")
                text = _opinion_text(row)
                if text:
                    with_text += 1
                    batch.append((oid, cid, row.get("type"), row.get("author_str"), len(text), text))
            if len(batch) >= 500:
                store.conn.executemany("INSERT OR REPLACE INTO opinion_text VALUES (?,?,?,?,?,?)", batch)
                store.conn.commit()
                batch = []
            if n % 50000 == 0:
                fh.flush()
                store.set_checkpoint(key, "in_progress", records_done_delta=n - store.get_checkpoint_records_done(key))
                store.conn.commit()
                print(f"[opinion-id-map] {n:,} opinions ({with_text:,} with text) in {time.time()-t0:.0f}s "
                      f"({n/(time.time()-t0):.0f} rows/s)")
        if batch:
            store.conn.executemany("INSERT OR REPLACE INTO opinion_text VALUES (?,?,?,?,?,?)", batch)
    store.conn.executemany("INSERT OR REPLACE INTO cl_opinion_cluster VALUES (?,?)", list(op2cl.items()))
    store.set_checkpoint(key, "complete", records_done_delta=n - store.get_checkpoint_records_done(key))
    store.conn.commit()
    print(f"[opinion-id-map] COMPLETE: {len(op2cl):,} opinions mapped in {time.time()-t0:.0f}s")
    return op2cl


# --------------------------------------------------------------------------- #
# Phase C — citation graph -> Citation records
# --------------------------------------------------------------------------- #
def _doc_id(opinion_id: str, op2cl: Dict[str, str]) -> str:
    cid = op2cl.get(opinion_id)
    return deterministic_id(SourceSystem.COURTLISTENER.value, cid) if cid else f"opinion:{opinion_id}"


def load_citation_map(store: Store, op2cl: Dict[str, str], first_cite: Dict[str, str], limit: Optional[int],
                      batch_size: int = 5000) -> int:
    key = "courtlistener:bulk:citation-map"
    if store.get_checkpoint(key) == "complete":
        print("[citation-map] already complete, skipping")
        return 0
    done = store.get_checkpoint_records_done(key)
    if done:
        print(f"[citation-map] resuming from row {done:,}")
    t0, n, unresolved = time.time(), done, 0
    raw_batch = []
    for row in stream_csv_rows(_bulk_url("citation-map"), limit=limit, skip=done):
        n += 1
        citing, cited = (row.get("citing_opinion_id") or "").strip(), (row.get("cited_opinion_id") or "").strip()
        if not (citing.isdigit() and cited.isdigit()):
            continue
        depth = int(row["depth"]) if (row.get("depth") or "").isdigit() else None
        cited_cluster = op2cl.get(cited)
        if cited_cluster is None:
            unresolved += 1
        rec = Citation(
            envelope=RecordEnvelope(
                source_system=SourceSystem.COURTLISTENER,
                source_id=f"{citing}->{cited}",
                id=deterministic_id(SourceSystem.COURTLISTENER.value, f"citation-map:{citing}->{cited}"),
                external_ids={"courtlistener_citing_opinion_id": citing, "courtlistener_cited_opinion_id": cited,
                              **({"courtlistener_cited_cluster_id": cited_cluster} if cited_cluster else {})},
            ),
            citing_document_id=_doc_id(citing, op2cl),
            cited_document_id=_doc_id(cited, op2cl),
            citation_string=first_cite.get(cited_cluster or "", "") if cited_cluster else "",
            depth=depth,
        )
        store.save(rec, commit=False)
        raw_batch.append((citing, cited, depth))
        if len(raw_batch) >= batch_size:
            store.conn.executemany("INSERT OR IGNORE INTO cl_citation_map VALUES (?,?,?)", raw_batch)
            store.set_checkpoint(key, "in_progress", records_done_delta=n - store.get_checkpoint_records_done(key))
            store.conn.commit()
            raw_batch = []
            if n % 250000 < batch_size:
                print(f"[citation-map] {n:,} edges in {time.time()-t0:.0f}s ({n/(time.time()-t0):.0f} rows/s), "
                      f"{unresolved:,} with unresolved cited opinion")
    if raw_batch:
        store.conn.executemany("INSERT OR IGNORE INTO cl_citation_map VALUES (?,?,?)", raw_batch)
    store.set_checkpoint(key, "complete", records_done_delta=n - store.get_checkpoint_records_done(key))
    store.conn.commit()
    print(f"[citation-map] COMPLETE: {n:,} edges in {time.time()-t0:.0f}s ({unresolved:,} unresolved)")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="rows per file; 0 = unbounded")
    ap.add_argument("--skip-opinions", action="store_true",
                    help="skip the 55 GB opinions pass; edges keep opinion:<id> placeholders")
    ap.add_argument("--only", choices=["citations", "opinions", "citation-map"], default=None)
    a = ap.parse_args()
    limit = None if a.limit == 0 else a.limit
    store = Store()
    print(f"db: {store.db_path}")
    _staging_schema(store)
    first_cite: Dict[str, str] = {}
    op2cl: Dict[str, str] = {}
    if a.only in (None, "citations"):
        first_cite = load_reporter_citations(store, limit)
    if a.only in (None, "opinions") and not a.skip_opinions:
        op2cl = load_opinion_map(store, limit)
    elif a.only == "citation-map":
        map_path = store.db_path.parent / OPINION_MAP_FILE
        if map_path.exists():
            op2cl = dict(line.rstrip("\n").split("\t", 1) for line in open(map_path) if "\t" in line)
        first_cite = {cid: cite for cid, cite in store.conn.execute(
            "SELECT cluster_id, MIN(citation_string) FROM cl_reporter_citation GROUP BY cluster_id")}
    if a.only in (None, "citation-map"):
        load_citation_map(store, op2cl, first_cite, limit)
    print("done.")


if __name__ == "__main__":
    main()
