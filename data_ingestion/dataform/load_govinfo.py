"""Session-bounded GovInfo load -- run this a few times rather than as one
long unattended job (GovInfo requires 2 API calls per document: one to list
packages, one to fetch each package's summary/PDF link, and a single
collection's since-2024 backlog can be hundreds of thousands of packages --
e.g. BILLS alone is 290K+ -- so an unbounded run isn't the right default the
way the CourtListener bulk CSV load was).

Each invocation processes up to --per-collection packages from each of the
six mapped collections (BILLS, PLAW, CFR, FR, CRPT, USCOURTS), then stops.
Resumable via the same checkpoints table pattern as bulk_load_courtlistener.py
-- each collection's `nextPage` cursor is persisted, so the next invocation
(a new "session") continues exactly where the last one left off rather than
re-fetching from 2024-01-01 every time.

Usage:
  python -m dataform.load_govinfo --per-collection 50
  python -m dataform.load_govinfo --per-collection 50 --only BILLS,PLAW
"""
from __future__ import annotations

import argparse
import time

from dataform.sources.govinfo import (
    _COLLECTION_TO_DOCTYPE,
    fetch_package_pages,
    fetch_package_summary,
    normalize_package,
)
from dataform.store import Store

START_DATE = "2024-01-01T00:00:00Z"


def load_collection(store: Store, collection: str, per_session: int) -> int:
    checkpoint_key = f"govinfo:{collection}"
    cursor = store.get_checkpoint(checkpoint_key)
    if cursor == "complete":
        print(f"[{collection}] already complete, skipping")
        return 0

    start_url = cursor if cursor and cursor != "start" else None
    saved = 0
    start = time.time()

    # Always finish the page in hand before checkpointing -- per_session is a
    # floor, not a hard per-package cap, precisely so the checkpoint can be
    # `next_url` (a real forward-progressing cursor) at every stopping point
    # rather than the current page's own start_url. An earlier version
    # checked the cap mid-page and persisted start_url, which on a session
    # that hits its cap on page 1 (the common case, since per_session ==
    # pageSize == 100) saved the literal fallback "start" every time --
    # confirmed live 2026-08-16: two full sessions against BILLS/PLAW/etc.
    # left the govinfo document count at exactly 600 (unchanged from session
    # 1), because session 2 re-fetched and re-upserted the same first page
    # of every collection instead of advancing.
    for packages, next_url in fetch_package_pages(collection, START_DATE, start_url=start_url):
        for pkg in packages:
            try:
                summary = fetch_package_summary(pkg["packageId"])
                doc = normalize_package(summary)
                store.save(doc)
                saved += 1
            except Exception as exc:  # noqa: BLE001 -- one bad package shouldn't kill the session
                print(f"[{collection}] package {pkg.get('packageId')} skipped: {exc}")
                continue

        if next_url is None:
            store.set_checkpoint(checkpoint_key, "complete", records_done_delta=saved)
            print(f"[{collection}] EXHAUSTED: {saved} packages in {time.time()-start:.0f}s (no more pages)")
            return saved

        if saved >= per_session:
            store.set_checkpoint(checkpoint_key, next_url, records_done_delta=saved)
            print(f"[{collection}] session done: {saved} packages in {time.time()-start:.0f}s (more remain)")
            return saved

    store.set_checkpoint(checkpoint_key, "complete", records_done_delta=saved)
    print(f"[{collection}] EXHAUSTED: {saved} packages in {time.time()-start:.0f}s")
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--per-collection", type=int, default=50, help="packages to fetch per collection this session")
    parser.add_argument("--only", type=str, default=None, help="comma-separated collection codes to restrict to")
    args = parser.parse_args()

    collections = list(_COLLECTION_TO_DOCTYPE.keys())
    if args.only:
        wanted = {c.strip().upper() for c in args.only.split(",")}
        collections = [c for c in collections if c in wanted]

    store = Store()
    total = 0
    for collection in collections:
        total += load_collection(store, collection, args.per_collection)
    store.close()
    print(f"\nTOTAL this session: {total} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
