"""One-time backfill: re-fetch already-saved Oyez oral-argument terms just to
populate AudioAsset.captured_at/location.

Why this is needed: those terms' documents were saved by the oyez lane
*before* captured_at/location were added to normalize_oral_argument (the
lane runs continuously for hours, and Python doesn't hot-reload a running
process's already-imported modules) -- confirmed 2026-08-16 that 0 of 2,711
already-saved oral_argument documents had captured_at set. The live lane was
restarted so every term from here forward gets the fields for free; this
script covers the ones that already completed under the stale code.

Only touches terms strictly before the checkpoint value the live lane had
reached at restart time (passed explicitly, not re-read live, so this script
doesn't grow its own scope while it runs). Reuses the exact same
fetch/normalize path as the live lane -- store.save() upserts via
deterministic_id, so re-saving an already-correct document is a harmless
no-op; the only real effect is filling in the two new fields.
"""
from __future__ import annotations

import sys
import time

from dataform.sources.oyez import fetch_case_detail, fetch_cases, fetch_oral_arguments_for_case, normalize_case
from dataform.store import Store

TERMS = [str(y) for y in range(2025, 1954, -1)]


def main() -> int:
    upto = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if upto <= 0:
        print("usage: python -m dataform.backfill_oyez_metadata <term_count>")
        return 1

    store = Store()
    print(f"backfilling terms 0..{upto - 1} of {len(TERMS)} ({TERMS[0]} down to {TERMS[upto - 1]})")

    updated = 0
    start = time.time()
    for i in range(upto):
        term = TERMS[i]
        try:
            stubs = fetch_cases(term=term, limit=200)
        except Exception as exc:  # noqa: BLE001 -- a bad/empty term shouldn't kill the backfill
            print(f"term {term} skipped: {exc}")
            continue
        for stub in stubs:
            try:
                detail = fetch_case_detail(term, stub["docket_number"])
                proceeding, *_ = normalize_case(detail)
                for doc in fetch_oral_arguments_for_case(detail, proceeding.envelope.id):
                    store.save(doc)
                    updated += 1
            except Exception as exc:  # noqa: BLE001 -- one malformed case shouldn't kill the backfill
                print(f"case {stub.get('docket_number')} in term {term} skipped: {exc}")
                continue
        elapsed = time.time() - start
        print(f"term {term} done ({updated} docs re-saved so far, {elapsed:.0f}s elapsed)")

    store.close()
    print(f"DONE: {updated} documents backfilled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
