"""CLI: fetch from a source, normalize into the canonical dataform, persist to SQLite.

Usage:
  python -m dataform.load --source courtlistener --limit 5
  python -m dataform.load --source ecfr --limit 5
  python -m dataform.load --source oyez --limit 2 --term 2019
  python -m dataform.load --source govinfo --limit 5        # needs GOVINFO_API_KEY
  python -m dataform.load --source congress --limit 5        # needs CONGRESS_API_KEY
  python -m dataform.load --source all --limit 5
"""
from __future__ import annotations

import argparse
import sys

from dataform.config import MissingAPIKeyError
from dataform.store import Store


def load_courtlistener(store: Store, limit: int, query: str) -> None:
    from dataform.sources import courtlistener as cl

    n = 0
    for result in cl.fetch_opinions(query=query, limit=limit):
        document, proceeding, organization = cl.normalize_search_result(result)
        store.save(organization)
        store.save(proceeding)
        store.save(document)
        for citation in cl.fetch_citations_for_result(result, document.envelope.id):
            store.save(citation)
        n += 1
    print(f"[courtlistener] saved {n} documents (+ proceedings/organizations/citations)")

    m = 0
    for person in cl.fetch_judges(limit=limit):
        store.save(person)
        m += 1
    print(f"[courtlistener] saved {m} people")


def load_ecfr(store: Store, limit: int) -> None:
    from dataform.sources import ecfr

    titles = ecfr.fetch_titles()
    n = 0
    for title in titles:
        if title.get("reserved"):
            continue
        for doc in ecfr.fetch_cfr_sections(title["number"], title["up_to_date_as_of"], limit=limit):
            store.save(doc)
            n += 1
        if n >= limit:
            break
    print(f"[ecfr] saved {n} CFR section documents")

    m = 0
    for agency in ecfr.fetch_agencies()[:limit]:
        store.save(ecfr.normalize_agency(agency))
        m += 1
    print(f"[ecfr] saved {m} agencies")


def load_oyez(store: Store, limit: int, term: str) -> None:
    from dataform.sources import oyez

    n_cases = n_audio = n_people = 0
    for case_stub in oyez.fetch_cases(term=term, limit=limit):
        detail = oyez.fetch_case_detail(term, case_stub["docket_number"])
        proceeding, organization, justices, advocates = oyez.normalize_case(detail)
        store.save(organization)
        store.save(proceeding)
        n_cases += 1
        for person in justices + advocates:
            store.save(person)
            n_people += 1
        for doc in oyez.fetch_oral_arguments_for_case(detail, proceeding.envelope.id):
            store.save(doc)
            n_audio += 1
    print(f"[oyez] saved {n_cases} proceedings, {n_audio} oral-argument documents, {n_people} people")


def load_govinfo(store: Store, limit: int) -> None:
    from dataform.sources import govinfo

    n = 0
    for doc in govinfo.fetch_documents("BILLS", "2024-01-01T00:00:00Z", limit=limit):
        store.save(doc)
        n += 1
    print(f"[govinfo] saved {n} documents")


def load_congress(store: Store, limit: int) -> None:
    from dataform.sources import congress

    n = 0
    for proceeding, document in congress.fetch_and_normalize_bills(118, limit=limit):
        store.save(proceeding)
        store.save(document)
        n += 1
    print(f"[congress] saved {n} bills (proceeding + document each)")

    m = 0
    for member in congress.fetch_members(limit=limit):
        store.save(congress.normalize_member(member))
        m += 1
    print(f"[congress] saved {m} members")


LOADERS = {
    "courtlistener": lambda store, args: load_courtlistener(store, args.limit, args.query),
    "ecfr": lambda store, args: load_ecfr(store, args.limit),
    "oyez": lambda store, args: load_oyez(store, args.limit, args.term),
    "govinfo": lambda store, args: load_govinfo(store, args.limit),
    "congress": lambda store, args: load_congress(store, args.limit),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=list(LOADERS.keys()) + ["all"],
        required=True,
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--query", default="graham v connor", help="courtlistener search query")
    parser.add_argument("--term", default="2019", help="oyez SCOTUS term (year)")
    args = parser.parse_args()

    store = Store()
    sources = list(LOADERS.keys()) if args.source == "all" else [args.source]
    exit_code = 0
    for source in sources:
        try:
            LOADERS[source](store, args)
        except MissingAPIKeyError as exc:
            print(f"[{source}] SKIPPED: {exc}", file=sys.stderr)
            exit_code = 1
        except Exception as exc:  # surface but keep going for --source all
            print(f"[{source}] FAILED: {exc}", file=sys.stderr)
            exit_code = 1
    store.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
