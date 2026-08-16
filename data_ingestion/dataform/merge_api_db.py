"""Merge the sibling api db (dataform_api.db) into the MAIN dataform.db the executor reads.

  * every canonical table (document, proceeding, person, organization, citation, ...):
        INSERT OR REPLACE main.<t> SELECT * FROM api.<t>      (id-keyed upsert)
  * opinion_text (opinion_id, cluster_id, opinion_type, author_str, n_chars, plain_text):
        copied as its own table + index on cluster_id — the executor looks up opinion text by the
        document's courtlistener cluster id (document.cl_cluster_id generated column) instead of
        us rewriting 5M JSON blobs.  --inline-text additionally writes the lead opinion's text into
        document.media.text.plain_text (slow: rewrites every opinion blob).
  * cl_reporter_citation / cl_citation_map / cl_opinion_cluster staging tables: copied verbatim.
  * checkpoints: rows from api overwrite main (they are the newer state for those keys).

MUST be the only writer on the main db while it runs. Idempotent.
  python -m dataform.merge_api_db --api-db ~/amicus-dataform/data/dataform_api.db [--inline-text]
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from dataform.store import Store

CANONICAL = ("document", "proceeding", "person", "organization", "citation", "financialdisclosure",
             "event", "position", "investment", "gift", "debt")
STAGING = ("opinion_text", "cl_reporter_citation", "cl_citation_map", "cl_opinion_cluster")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-db", required=True)
    ap.add_argument("--inline-text", action="store_true", help="also json_set document.media.text.plain_text")
    ap.add_argument("--only", default=None, help="comma list of tables to merge")
    a = ap.parse_args()
    store = Store()
    conn = store.conn
    conn.execute("PRAGMA busy_timeout=600000")
    conn.execute("ATTACH DATABASE ? AS api", (str(Path(a.api_db).expanduser()),))
    api_tables = {r[0] for r in conn.execute("SELECT name FROM api.sqlite_master WHERE type='table'")}
    only = set(a.only.split(",")) if a.only else None
    print(f"main: {store.db_path}\napi tables: {sorted(api_tables)}")

    for t in CANONICAL:
        if t not in api_tables or (only and t not in only):
            continue
        n_api = conn.execute(f"SELECT COUNT(*) FROM api.{t}").fetchone()[0]
        if not n_api:
            continue
        t0 = time.time()
        conn.execute(f"INSERT OR REPLACE INTO main.{t} (id, source_system, source_id, doc_type, date, data) "
                     f"SELECT id, source_system, source_id, doc_type, date, data FROM api.{t}")
        conn.commit()
        print(f"[merge] {t}: {n_api:,} rows upserted ({time.time()-t0:.0f}s)")

    for t in STAGING:
        if t not in api_tables or (only and t not in only):
            continue
        t0 = time.time()
        conn.execute(f"DROP TABLE IF EXISTS main.{t}")
        conn.execute(f"CREATE TABLE main.{t} AS SELECT * FROM api.{t}")
        conn.commit()
        n = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        print(f"[merge] {t}: {n:,} rows copied ({time.time()-t0:.0f}s)")
    if "opinion_text" in api_tables and (not only or "opinion_text" in only):
        t0 = time.time()
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_opinion_text_cluster ON opinion_text(cluster_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_opinion_text_opinion ON opinion_text(opinion_id);
            CREATE INDEX IF NOT EXISTS idx_cl_reporter_citation_cite ON cl_reporter_citation(citation_string);
            CREATE INDEX IF NOT EXISTS idx_cl_reporter_citation_cluster ON cl_reporter_citation(cluster_id);
            CREATE INDEX IF NOT EXISTS idx_cl_citation_map_cited ON cl_citation_map(cited_opinion_id);
            CREATE INDEX IF NOT EXISTS idx_cl_citation_map_citing ON cl_citation_map(citing_opinion_id);
        """)
        conn.commit()
        print(f"[merge] staging indexes ready ({time.time()-t0:.0f}s)")

    if "checkpoints" in api_tables and (not only or "checkpoints" in only):
        conn.execute("INSERT OR REPLACE INTO main.checkpoints SELECT * FROM api.checkpoints")
        conn.commit()
        print("[merge] checkpoints synced")

    if a.inline_text and "opinion_text" in api_tables:
        # lead opinion per cluster: prefer combined/lead types, else the longest
        t0 = time.time()
        conn.execute("""
            CREATE TEMP TABLE lead AS
            SELECT cluster_id, plain_text FROM (
              SELECT cluster_id, plain_text,
                     ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY
                        CASE WHEN opinion_type IN ('010combined','020lead') THEN 0 ELSE 1 END, n_chars DESC) rn
              FROM main.opinion_text) WHERE rn = 1""")
        n = conn.execute("""
            UPDATE main.document SET data = json_set(data, '$.media.text', json_object('plain_text', lead.plain_text,
                                                                                       'html', NULL, 'language', 'en'))
            FROM lead WHERE main.document.source_system = 'courtlistener'
              AND json_extract(main.document.data, '$.envelope.external_ids.courtlistener_cluster_id') = lead.cluster_id
        """).rowcount
        conn.commit()
        print(f"[merge] inlined plain_text into {n:,} documents ({time.time()-t0:.0f}s)")

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for q, label in (
        ("SELECT COUNT(*) FROM citation", "citation rows"),
        ("SELECT COUNT(*) FROM opinion_text", "opinion_text rows"),
        ("SELECT COUNT(*) FROM document WHERE json_extract(data,'$.jurisdiction') IS NOT NULL", "documents with jurisdiction"),
        ("SELECT COUNT(*) FROM document WHERE json_extract(data,'$.citation') IS NOT NULL", "documents with citation"),
        ("SELECT COUNT(*) FROM document WHERE json_extract(data,'$.summary') IS NOT NULL", "documents with summary"),
        ("SELECT COUNT(*) FROM document WHERE json_extract(data,'$.media.text.plain_text') IS NOT NULL", "documents with inline plain_text"),
        ("SELECT COUNT(DISTINCT cluster_id) FROM opinion_text", "clusters with opinion_text"),
    ):
        try:
            print(f"  {label:34s} {conn.execute(q).fetchone()[0]:,}")
        except sqlite3.Error as e:
            print(f"  {label:34s} n/a ({e})")
    print("done.")


if __name__ == "__main__":
    main()
