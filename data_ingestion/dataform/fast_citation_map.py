"""FAST raw load of citation-map (citing_opinion_id, cited_opinion_id, depth) into the staging table
cl_citation_map — no pydantic, no per-row JSON, just executemany. ~10x faster than the Citation-record
path in bulk_load_citations phase C; that path can still run later to build canonical records.
Join to clusters via opinion_text(opinion_id, cluster_id) (or cl_opinion_cluster once complete).

  DATAFORM_DB=.../dataform_api.db python -m dataform.fast_citation_map
"""
from __future__ import annotations
import sqlite3, sys, time
from dataform.store import Store
from dataform.bulk_load_citations import stream_csv_rows_fast, _bulk_url, _staging_schema

def main() -> None:
    store = Store(); _staging_schema(store)
    conn = store.conn; conn.execute("PRAGMA busy_timeout=120000"); conn.execute("PRAGMA synchronous=OFF")
    key = "courtlistener:bulk:citation-map-raw"
    done = store.get_checkpoint_records_done(key)
    if store.get_checkpoint(key) == "complete":
        print("already complete"); return
    print(f"db {store.db_path}; resuming from row {done:,}" if done else f"db {store.db_path}")
    t0, n, batch = time.time(), done, []
    for row in stream_csv_rows_fast(_bulk_url("citation-map"), skip=done):
        n += 1
        a, b = row.get("citing_opinion_id") or "", row.get("cited_opinion_id") or ""
        if a and b:
            d = row.get("depth") or ""
            batch.append((a, b, int(d) if d.isdigit() else None))
        if len(batch) >= 20000:
            conn.executemany("INSERT OR IGNORE INTO cl_citation_map VALUES (?,?,?)", batch)
            store.set_checkpoint(key, "in_progress", records_done_delta=n - store.get_checkpoint_records_done(key))
            batch = []
            if n % 1_000_000 < 20000:
                print(f"[citation-map raw] {n:,} edges in {time.time()-t0:.0f}s ({n/(time.time()-t0):.0f} rows/s)", flush=True)
    if batch:
        conn.executemany("INSERT OR IGNORE INTO cl_citation_map VALUES (?,?,?)", batch)
    store.set_checkpoint(key, "complete", records_done_delta=n - store.get_checkpoint_records_done(key))
    conn.commit()
    print(f"[citation-map raw] COMPLETE: {n:,} edges in {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
