"""Generic file -> canonical dataform -> SQLite + formatted output file.

Takes an arbitrary input file (JSON, JSONL, or CSV — whatever a teammate
dumps: a bulk export, a scraped page, a hand-built test fixture) containing
RAW, source-native records, and:

  1. reads it in whatever shape it's in (list-of-objects JSON, one-object-
     per-line JSONL, or CSV with JSON-ish cells),
  2. runs each row through the same normalize_*() function the live loaders
     use (dataform/sources/*.py) — the exact mapping is not duplicated here,
  3. validates every result against the Pydantic models in dataform/models.py,
     skipping (and reporting) any row that doesn't fit rather than aborting
     the whole file,
  4. upserts everything into the canonical SQLite store, AND
  5. writes a second file of the *normalized* records (one canonical JSON
     object per line) — the "formatted, ready to use" artifact — independent
     of the database, so a teammate can inspect or hand it to something else
     without touching SQLite at all.

Usage:
  python -m dataform.file_loader --input raw/cl_opinions.jsonl \\
      --source courtlistener --record-kind opinion

  python -m dataform.file_loader --input raw/agencies.csv \\
      --source ecfr --record-kind agency --out data/ecfr_agencies.normalized.jsonl

Record kinds by source (what shape each row must be — matches the raw shape
each source's live API already returns, see dataform/sources/<source>.py):
  courtlistener : opinion (a /search/?type=o hit) | person (a /people/ row)
  ecfr          : cfr_section (flat: title_number, identifier, type,
                                label_description, label, hierarchy_path)
                | agency (an /admin/v1/agencies.json row)
  oyez          : case (a /cases/{term}/{docket} detail)
                | audio (a /case_media/oral_argument_audio/{id} detail,
                         must include a "proceeding_id" key linking it back
                         to an already-loaded case)
  govinfo       : package (a /packages/{id}/summary row)
  congress      : bill (a /v3/bill/.../{number} row) | member | committee
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

from pydantic import BaseModel, ValidationError

from dataform.store import Store

# ---------------------------------------------------------------------------
# Step 1: read an arbitrary file into a stream of raw dicts
# ---------------------------------------------------------------------------

def read_records(path: Path) -> Iterator[Dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        yield from _read_jsonl(path)
    elif suffix == ".json":
        yield from _read_json(path)
    elif suffix == ".csv":
        yield from _read_csv(path)
    else:
        raise ValueError(f"Unsupported input format: {suffix} (use .json, .jsonl, or .csv)")


def _read_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open() as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[read] {path.name}:{line_no} not valid JSON, skipped ({exc})", file=sys.stderr)


# raw API dumps often come wrapped ({"results": [...]}, {"bills": [...]}, ...);
# unwrap the common ones so a straight `curl > file.json` works as input too
_KNOWN_WRAPPER_KEYS = ("results", "bills", "members", "committees", "packages", "agencies", "titles")


def _read_json(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open() as f:
        data = json.load(f)
    if isinstance(data, list):
        yield from data
        return
    if isinstance(data, dict):
        for key in _KNOWN_WRAPPER_KEYS:
            if isinstance(data.get(key), list):
                yield from data[key]
                return
        yield data  # a single raw record
        return
    raise ValueError(f"{path}: top-level JSON must be an object or array")


def _read_csv(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            yield {k: _coerce_csv_cell(v) for k, v in row.items()}


def _coerce_csv_cell(value: Any) -> Any:
    """CSV cells are always strings; unwrap JSON-looking cells (list/object
    columns from a bulk export) back into real values, leave the rest alone."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped[:1] in ("[", "{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value if value != "" else None


# ---------------------------------------------------------------------------
# Step 2: dispatch each raw record to the right normalize_*() function
# ---------------------------------------------------------------------------

def _cl_opinion(row: dict) -> List[BaseModel]:
    from dataform.sources.courtlistener import normalize_search_result
    return list(normalize_search_result(row))


def _cl_person(row: dict) -> List[BaseModel]:
    from dataform.sources.courtlistener import normalize_person
    return [normalize_person(row)]


def _ecfr_cfr_section(row: dict) -> List[BaseModel]:
    from dataform.sources.ecfr import normalize_structure_node
    node = {
        "identifier": row.get("identifier", ""),
        "type": row.get("type", "section"),
        "label_description": row.get("label_description"),
        "label": row.get("label"),
    }
    return [normalize_structure_node(node, int(row["title_number"]), row.get("hierarchy_path", ""))]


def _ecfr_agency(row: dict) -> List[BaseModel]:
    from dataform.sources.ecfr import normalize_agency
    return [normalize_agency(row)]


def _oyez_case(row: dict) -> List[BaseModel]:
    from dataform.sources.oyez import normalize_case
    proceeding, organization, justices, advocates = normalize_case(row)
    return [proceeding, organization, *justices, *advocates]


def _oyez_audio(row: dict) -> List[BaseModel]:
    from dataform.sources.oyez import normalize_oral_argument
    proceeding_id = row.get("proceeding_id")
    if not proceeding_id:
        raise ValueError("oyez 'audio' rows must include a 'proceeding_id' linking them to a loaded case")
    doc = normalize_oral_argument({}, row, proceeding_id)
    return [doc] if doc else []


def _govinfo_package(row: dict) -> List[BaseModel]:
    from dataform.sources.govinfo import normalize_package
    return [normalize_package(row)]


def _congress_bill(row: dict) -> List[BaseModel]:
    from dataform.sources.congress import normalize_bill
    return list(normalize_bill(row))


def _congress_member(row: dict) -> List[BaseModel]:
    from dataform.sources.congress import normalize_member
    return [normalize_member(row)]


def _congress_committee(row: dict) -> List[BaseModel]:
    from dataform.sources.congress import normalize_committee
    return [normalize_committee(row)]


NORMALIZERS: Dict[str, Dict[str, Callable[[dict], List[BaseModel]]]] = {
    "courtlistener": {"opinion": _cl_opinion, "person": _cl_person},
    "ecfr": {"cfr_section": _ecfr_cfr_section, "agency": _ecfr_agency},
    "oyez": {"case": _oyez_case, "audio": _oyez_audio},
    "govinfo": {"package": _govinfo_package},
    "congress": {"bill": _congress_bill, "member": _congress_member, "committee": _congress_committee},
}


def default_record_kind(source: str) -> str:
    return next(iter(NORMALIZERS[source]))


# ---------------------------------------------------------------------------
# Steps 3-5: normalize + validate + store + write the formatted output file
# ---------------------------------------------------------------------------

class LoadReport(BaseModel):
    input_path: str
    output_path: str
    records_read: int = 0
    records_normalized: int = 0
    records_skipped: int = 0
    entities_written: Dict[str, int] = {}
    errors: List[str] = []


def load_file(
    input_path: Path,
    source: str,
    record_kind: str,
    output_path: Path,
    store: Store,
    limit: int = None,
) -> LoadReport:
    if source not in NORMALIZERS:
        raise ValueError(f"Unknown source '{source}' (choices: {list(NORMALIZERS)})")
    if record_kind not in NORMALIZERS[source]:
        raise ValueError(f"Unknown record kind '{record_kind}' for {source} (choices: {list(NORMALIZERS[source])})")
    normalize = NORMALIZERS[source][record_kind]

    report = LoadReport(input_path=str(input_path), output_path=str(output_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w") as out:
        for raw in read_records(input_path):
            report.records_read += 1
            if limit and report.records_read > limit:
                report.records_read -= 1
                break
            try:
                entities = normalize(raw)
            except (ValidationError, ValueError, KeyError, TypeError) as exc:
                report.records_skipped += 1
                report.errors.append(f"record {report.records_read}: {exc}")
                continue

            report.records_normalized += 1
            for entity in entities:
                store.save(entity)
                out.write(
                    json.dumps({"entity_type": type(entity).__name__, **entity.model_dump(mode="json")})
                    + "\n"
                )
                type_name = type(entity).__name__
                report.entities_written[type_name] = report.entities_written.get(type_name, 0) + 1

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="raw input file (.json, .jsonl, or .csv)")
    parser.add_argument("--source", required=True, choices=list(NORMALIZERS))
    parser.add_argument("--record-kind", default=None, help="defaults to the first kind for --source")
    parser.add_argument("--out", type=Path, default=None, help="defaults to <input>.normalized.jsonl")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    record_kind = args.record_kind or default_record_kind(args.source)
    out_path = args.out or args.input.with_suffix("").with_suffix(".normalized.jsonl")

    store = Store()
    try:
        report = load_file(args.input, args.source, record_kind, out_path, store, limit=args.limit)
    finally:
        store.close()

    print(f"read:       {report.records_read}")
    print(f"normalized: {report.records_normalized}")
    print(f"skipped:    {report.records_skipped}")
    for type_name, count in sorted(report.entities_written.items()):
        print(f"  -> {type_name}: {count} rows (SQLite + {out_path})")
    if report.errors:
        print(f"\n{len(report.errors)} error(s):", file=sys.stderr)
        for err in report.errors[:10]:
            print(f"  {err}", file=sys.stderr)
        if len(report.errors) > 10:
            print(f"  ... and {len(report.errors) - 10} more", file=sys.stderr)

    return 0 if report.records_normalized > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
