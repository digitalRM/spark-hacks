"""Bounded bulk-CSV load of CourtListener data -- the rate-limit-free path.

The REST API caps anonymous access at 50 requests/hour; this streams
CourtListener's free quarterly CSV snapshots directly from S3 instead, with
no such limit. Bounded by --limit (rows per large file) rather than a court
filter, since filtering by court would require a full scan of the 5GB
dockets file just to build the filter -- a row-count cap gets real volume
fast and is exact/predictable, at the cost of not choosing which courts.

Usage:
  python -m dataform.bulk_load_courtlistener --limit 500000
  python -m dataform.bulk_load_courtlistener --limit 500000 --resume   # skip files already fully loaded
"""
from __future__ import annotations

import argparse
import time

from dataform.sources.courtlistener_bulk import (
    _bulk_url,
    normalize_court_row,
    normalize_docket_row,
    normalize_financial_disclosure_row,
    normalize_opinion_cluster_row,
    normalize_person_row,
    stream_csv_rows,
)
from dataform.store import Store


def load_file(store: Store, label: str, url: str, normalize, limit: int = None, batch_size: int = 2000) -> int:
    checkpoint_key = f"courtlistener:bulk:{label}"
    already_done = store.get_checkpoint(checkpoint_key)
    if already_done == "complete":
        print(f"[{label}] already complete this snapshot, skipping (pass without --resume to redo)")
        return 0

    # Resume point survives a full process restart, not just an in-process
    # retry: records_done is persisted to the checkpoints table on every
    # flush (see below), so relaunching this script after a crash picks up
    # from here instead of re-streaming the file from row 0.
    count = store.get_checkpoint_records_done(checkpoint_key)
    if count:
        print(f"[{label}] resuming from row {count:,} (persisted checkpoint)")

    start = time.time()
    batch = []

    def flush():
        nonlocal count
        for rec in batch:
            store.save(rec, commit=False)
        n = len(batch)
        count += n
        batch.clear()
        store.set_checkpoint(checkpoint_key, "in_progress", records_done_delta=n)

    # Outer retry: a dropped HTTP connection mid-stream (confirmed live
    # 2026-08-16 -- a ConnectionResetError killed a 3.35M-row-in unbounded
    # dockets load with no error surfaced, just a silent early stop) must not
    # end the whole load. `skip=count` re-enters the CSV past every row
    # already flushed to the DB, so a retry doesn't redo that work or
    # re-insert duplicates.
    backoff = 5
    while True:
        try:
            for row in stream_csv_rows(url, limit=limit, skip=count):
                try:
                    batch.append(normalize(row))
                except Exception as exc:  # noqa: BLE001 -- one malformed row shouldn't kill the stream
                    print(f"[{label}] row skipped: {exc}")
                    continue
                if len(batch) >= batch_size:
                    flush()
                    backoff = 5  # reset after any successful progress
                    if count % 50000 < batch_size:
                        elapsed = time.time() - start
                        print(f"[{label}] {count:>9,} rows in {elapsed:6.0f}s ({count/max(elapsed,1):.0f} rows/s)")
            flush()
            break
        except Exception as exc:  # noqa: BLE001 -- network/stream errors must not kill the whole load
            flush()
            print(f"[{label}] stream error at {count:,} rows, retrying in {backoff}s: {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    store.set_checkpoint(checkpoint_key, "complete" if limit is None else f"limit={limit}")
    elapsed = time.time() - start
    print(f"[{label}] DONE: {count:,} rows in {elapsed:.0f}s ({count/max(elapsed,1):.0f} rows/s)")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=500_000, help="row cap for dockets.csv and opinion-clusters.csv; 0 = unbounded (full corpus)")
    parser.add_argument("--resume", action="store_true", help="no-op flag for clarity; per-file completion is always checked via checkpoints")
    args = parser.parse_args()
    limit = None if args.limit == 0 else args.limit

    store = Store()
    total_start = time.time()

    print("=== courts (full, ~81KB) ===")
    load_file(store, "courts", _bulk_url("courts"), normalize_court_row, limit=None)

    print("=== people (full, ~455KB) ===")
    load_file(store, "people", _bulk_url("people-db-people"), normalize_person_row, limit=None)

    limit_label = f"limit={limit:,}" if limit else "unbounded (full corpus)"
    print(f"=== dockets ({limit_label}) ===")
    load_file(store, "dockets", _bulk_url("dockets"), normalize_docket_row, limit=limit)

    print(f"=== opinion-clusters ({limit_label}) ===")
    load_file(store, "opinion-clusters", _bulk_url("opinion-clusters"), normalize_opinion_cluster_row, limit=limit)

    print("=== financial-disclosures (full -- far smaller than dockets/opinion-clusters) ===")
    load_file(store, "financial-disclosures", _bulk_url("financial-disclosures"), normalize_financial_disclosure_row, limit=None)

    print(f"\nTOTAL WALL TIME: {time.time() - total_start:.0f}s")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
