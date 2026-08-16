"""Time-boxed, parallel, resumable loader across CourtListener, eCFR,
congress.gov, and Oyez (GovInfo excluded per the redundancy analysis in
docs/DATAFORM.md).

Ten worker threads run as ten independent "lanes", each an infinite
fetch-normalize-save loop against one narrow slice of one source, so lanes
don't block each other and don't need to coordinate:

  courtlistener opinions  x6 lanes, one per court filter (scotus, ca1..ca5)
  congress.gov bills      x2 lanes  (split by even/odd bill offset)
  ecfr CFR sections       x1 lane   (walks titles sequentially)
  oyez oral arguments     x1 lane   (walks terms sequentially)

Every lane checkpoints its resume position (a CourtListener/congress.gov
`next` URL, or an eCFR title index / Oyez term index) to the `checkpoints`
table after each unit of work, so a second run with --resume picks up
exactly where the previous run's time box cut it off, instead of
re-fetching from the start and re-doing (harmlessly, since Store.save() is
an upsert -- but wastefully) everything already saved.

Usage:
  python -m dataform.parallel_load --duration 500          # fresh run, default lane split
  python -m dataform.parallel_load --duration 500 --resume  # continue from last checkpoint
  python -m dataform.parallel_load --reset                  # wipe all data + checkpoints, no run
"""
from __future__ import annotations

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional

from dataform.config import MissingAPIKeyError
from dataform.store import Store

_print_lock = threading.Lock()


def log(lane: str, msg: str) -> None:
    with _print_lock:
        print(f"[{lane:22}] {msg}", flush=True)


@dataclass
class LaneResult:
    lane: str
    records_saved: int = 0
    error: Optional[str] = None
    exhausted: bool = False  # True if the lane ran out of data rather than hitting the deadline


# ---------------------------------------------------------------------------
# CourtListener opinion lanes -- one court filter per lane, own checkpoint key
# ---------------------------------------------------------------------------

def run_courtlistener_lane(lane_name: str, court: Optional[str], deadline: float) -> LaneResult:
    from dataform.sources.courtlistener import fetch_opinion_pages, normalize_search_result

    store = Store()
    checkpoint_key = f"courtlistener:opinions:{lane_name}"
    result = LaneResult(lane=lane_name)
    consecutive_errors = 0
    try:
        while time.time() < deadline:
            try:
                start_url = store.get_checkpoint(checkpoint_key)
                for results, next_url in fetch_opinion_pages(court=court, start_url=start_url):
                    if time.time() >= deadline:
                        log(lane_name, f"deadline hit, checkpointing mid-stream ({result.records_saved} saved)")
                        return result
                    for r in results:
                        document, proceeding, organization = normalize_search_result(r)
                        store.save(organization)
                        store.save(proceeding)
                        store.save(document)
                        result.records_saved += 1
                    store.set_checkpoint(checkpoint_key, next_url, records_done_delta=len(results))
                    consecutive_errors = 0
                    result.error = None  # a later success clears an earlier transient failure
                    if not next_url:
                        result.exhausted = True
                        log(lane_name, f"exhausted this court's opinions ({result.records_saved} saved)")
                        return result
            except Exception as exc:  # noqa: BLE001 -- transient errors must not permanently kill a lane
                consecutive_errors += 1
                wait = min(60, 2 ** consecutive_errors)
                result.error = str(exc)
                log(lane_name, f"error #{consecutive_errors} ({exc}) -- backing off {wait}s and resuming from checkpoint")
                time.sleep(wait)
    finally:
        store.close()
    if not result.exhausted:
        log(lane_name, f"deadline hit ({result.records_saved} saved, last error: {result.error})")
    return result


def run_courtlistener_people_lane(deadline: float) -> LaneResult:
    from dataform.sources.courtlistener import fetch_person_pages, normalize_person

    store = Store()
    checkpoint_key = "courtlistener:people"
    result = LaneResult(lane="courtlistener:people")
    try:
        start_url = store.get_checkpoint(checkpoint_key)
        for results, next_url in fetch_person_pages(start_url=start_url):
            if time.time() >= deadline:
                log(result.lane, f"deadline hit ({result.records_saved} saved)")
                return result
            for row in results:
                store.save(normalize_person(row))
                result.records_saved += 1
            store.set_checkpoint(checkpoint_key, next_url, records_done_delta=len(results))
            if not next_url:
                result.exhausted = True
                return result
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
        log(result.lane, f"ERROR: {exc}")
    finally:
        store.close()
    return result


# ---------------------------------------------------------------------------
# congress.gov lanes -- bills split even/odd by page, members single lane
# ---------------------------------------------------------------------------

def run_congress_bills_lane(lane_name: str, congress: int, deadline: float) -> LaneResult:
    from dataform.sources.congress import fetch_bill_pages, normalize_bill

    store = Store()
    checkpoint_key = f"congress:bills:{lane_name}"
    result = LaneResult(lane=lane_name)
    try:
        start_url = store.get_checkpoint(checkpoint_key)
        for bills, next_url in fetch_bill_pages(congress, start_url=start_url):
            if time.time() >= deadline:
                log(lane_name, f"deadline hit ({result.records_saved} saved)")
                return result
            for bill in bills:
                proceeding, document = normalize_bill(bill)
                store.save(proceeding)
                store.save(document)
                result.records_saved += 1
            store.set_checkpoint(checkpoint_key, next_url, records_done_delta=len(bills))
            if not next_url:
                result.exhausted = True
                log(lane_name, f"exhausted (all bills fetched, {result.records_saved} saved)")
                return result
    except MissingAPIKeyError as exc:
        result.error = str(exc)
        log(lane_name, f"SKIPPED: {exc}")
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
        log(lane_name, f"ERROR: {exc}")
    finally:
        store.close()
    return result


# ---------------------------------------------------------------------------
# eCFR lane -- walks titles sequentially by index
# ---------------------------------------------------------------------------

def run_ecfr_lane(deadline: float) -> LaneResult:
    from dataform.sources.ecfr import fetch_title_structure, fetch_titles, iter_structure_nodes, normalize_structure_node

    store = Store()
    checkpoint_key = "ecfr:sections"
    result = LaneResult(lane="ecfr:sections")
    try:
        titles = fetch_titles()
        titles = [t for t in titles if not t.get("reserved")]
        start_index = int(store.get_checkpoint(checkpoint_key) or 0)
        for i in range(start_index, len(titles)):
            if time.time() >= deadline:
                log(result.lane, f"deadline hit mid-title ({result.records_saved} saved)")
                return result
            title = titles[i]
            structure = fetch_title_structure(title["number"], title["up_to_date_as_of"])
            for node, path in iter_structure_nodes(structure):
                if node.get("type") != "section":
                    continue
                store.save(normalize_structure_node(node, title["number"], path))
                result.records_saved += 1
            store.set_checkpoint(checkpoint_key, str(i + 1))
            log(result.lane, f"title {title['number']} done ({result.records_saved} saved so far)")
        result.exhausted = True
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
        log(result.lane, f"ERROR: {exc}")
    finally:
        store.close()
    return result


# ---------------------------------------------------------------------------
# Oyez lane -- walks terms sequentially, newest first
# ---------------------------------------------------------------------------

def run_oyez_lane(deadline: float) -> LaneResult:
    from dataform.sources.oyez import (
        fetch_audio_detail,
        fetch_case_detail,
        fetch_cases,
        fetch_oral_arguments_for_case,
        normalize_case,
    )

    store = Store()
    checkpoint_key = "oyez:terms"
    result = LaneResult(lane="oyez:oral_arguments")
    terms = [str(y) for y in range(2025, 1954, -1)]  # OT2025 down to OT1955
    consecutive_errors = 0
    try:
        while time.time() < deadline:
            start_index = int(store.get_checkpoint(checkpoint_key) or 0)
            if start_index >= len(terms):
                result.exhausted = True
                return result
            try:
                for i in range(start_index, len(terms)):
                    if time.time() >= deadline:
                        return result
                    term = terms[i]
                    try:
                        stubs = fetch_cases(term=term, limit=200)
                    except Exception as exc:  # noqa: BLE001 -- a bad/empty term shouldn't kill the lane
                        log(result.lane, f"term {term} skipped: {exc}")
                        store.set_checkpoint(checkpoint_key, str(i + 1))
                        continue
                    for stub in stubs:
                        if time.time() >= deadline:
                            return result
                        try:
                            detail = fetch_case_detail(term, stub["docket_number"])
                            proceeding, organization, justices, advocates = normalize_case(detail)
                            store.save(organization)
                            store.save(proceeding)
                            for person in justices + advocates:
                                store.save(person)
                            for doc in fetch_oral_arguments_for_case(detail, proceeding.envelope.id):
                                store.save(doc)
                                result.records_saved += 1
                        except Exception as exc:  # noqa: BLE001 -- one malformed case shouldn't kill the lane
                            log(result.lane, f"case {stub.get('docket_number')} in term {term} skipped: {exc}")
                            continue
                    store.set_checkpoint(checkpoint_key, str(i + 1))
                    consecutive_errors = 0
                    result.error = None
                    log(result.lane, f"term {term} done ({result.records_saved} saved so far)")
                result.exhausted = True
                return result
            except Exception as exc:  # noqa: BLE001 -- transient errors (network, etc) must not kill the lane
                consecutive_errors += 1
                wait = min(60, 2 ** consecutive_errors)
                result.error = str(exc)
                log(result.lane, f"error #{consecutive_errors} ({exc}) -- backing off {wait}s and resuming from checkpoint")
                time.sleep(wait)
    finally:
        store.close()
    if not result.exhausted:
        log(result.lane, f"deadline hit ({result.records_saved} saved, last error: {result.error})")
    return result


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

DEFAULT_LANES = [
    ("courtlistener:scotus", lambda deadline: run_courtlistener_lane("scotus", "scotus", deadline)),
    ("courtlistener:ca1", lambda deadline: run_courtlistener_lane("ca1", "ca1", deadline)),
    ("courtlistener:ca2", lambda deadline: run_courtlistener_lane("ca2", "ca2", deadline)),
    ("courtlistener:ca3", lambda deadline: run_courtlistener_lane("ca3", "ca3", deadline)),
    ("courtlistener:ca4", lambda deadline: run_courtlistener_lane("ca4", "ca4", deadline)),
    ("courtlistener:ca5", lambda deadline: run_courtlistener_lane("ca5", "ca5", deadline)),
    ("congress:bills:119", lambda deadline: run_congress_bills_lane("119", 119, deadline)),
    ("congress:bills:118", lambda deadline: run_congress_bills_lane("118", 118, deadline)),
    ("ecfr:sections", run_ecfr_lane),
    ("oyez:oral_arguments", run_oyez_lane),
]
# courtlistener people and congress members are cheap (list-only, no detail
# call) -- piggyback them onto whichever slots are left after the 10 lanes
# above if you want them in this run; omitted by default to keep exactly 10
# workers matching the requested split.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=int, default=500, help="seconds to run before stopping all lanes")
    parser.add_argument("--reset", action="store_true", help="wipe all records + checkpoints, then exit")
    parser.add_argument("--resume", action="store_true", help="explicit no-op flag -- resuming is always the default when checkpoints exist; kept for clarity in invocations")
    parser.add_argument(
        "--only",
        default=None,
        help="comma-separated substrings to filter DEFAULT_LANES by name, e.g. --only oyez or --only courtlistener,ecfr",
    )
    args = parser.parse_args()

    if args.reset:
        store = Store()
        store.clear_all()
        store.close()
        print("cleared all records and checkpoints.")
        return 0

    lanes = DEFAULT_LANES
    if args.only:
        filters = [f.strip() for f in args.only.split(",")]
        lanes = [(name, fn) for name, fn in DEFAULT_LANES if any(f in name for f in filters)]
        if not lanes:
            print(f"--only {args.only!r} matched no lanes (available: {[n for n, _ in DEFAULT_LANES]})")
            return 1

    deadline = time.time() + args.duration
    print(f"running {len(lanes)} lane(s) for {args.duration}s (deadline at +{args.duration}s): {[n for n, _ in lanes]}")

    results: List[LaneResult] = []
    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        futures = {pool.submit(fn, deadline): name for name, fn in lanes}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                log(name, f"LANE CRASHED: {exc}")
                results.append(LaneResult(lane=name, error=str(exc)))

    elapsed = time.time() - (deadline - args.duration)
    print(f"\n{'='*60}\ndone in {elapsed:.1f}s\n{'='*60}")
    total = 0
    for r in sorted(results, key=lambda r: r.lane):
        status = "EXHAUSTED" if r.exhausted else ("ERROR" if r.error else "deadline-cut")
        print(f"  {r.lane:24} {r.records_saved:>6} saved   [{status}]" + (f"  -- {r.error}" if r.error else ""))
        total += r.records_saved
    print(f"\n  TOTAL: {total} records saved this run")

    store = Store()
    print(f"\ncheckpoints (resume state for next run):")
    for row in store.all_checkpoints():
        print(f"  {row['key']:28} records_done={row['records_done']:<8} cursor={str(row['value'])[:60]}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
