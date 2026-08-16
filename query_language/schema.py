"""The schema registry — what fields exist and what modality each one carries.

This is the file that makes a two-predicate language fully featured. A BQL query
never names a model or a modality; it names a FIELD. Everything the optimizer
needs in order to turn `Fuzzy(cluster.scan_pages, "...")` into a vision call over
47 page images lives here, in that field's spec:

    type        SCALAR | TEXT | TEXT_CHUNKED | IMAGE_SET | AUDIO | DOC_SCAN
    unit        what one model call actually looks at: row | page | chunk | segment
    fanout      expected units per row (47 pages per scanned record)
    quantifier  ANY (stop at the first matching unit) | ALL (stop at the first failure)
    derivation  null | RASTERIZE | DOC_PARSE | ASR | CHUNK — work needed before the
                predicate can run at all, which the optimizer makes an explicit stage
    embeddable  whether a cheap similarity pass can narrow the candidates first

Adding a modality is a row in this registry, not a keyword in the grammar.

Schemas are JSON so that stage 1 (data-ingestion) owns them and the compiler,
the optimizer and the runtime can all read the same file. `schemas/*.json` ships
a working default; point AMICUS_SCHEMA at another file to override.

Loading is cached; everything else here is free.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import config

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

FIELD_TYPES = ("SCALAR", "TEXT", "TEXT_CHUNKED", "IMAGE_SET", "AUDIO", "DOC_SCAN")
QUANTIFIERS = ("ANY", "ALL")
DERIVATIONS = ("RASTERIZE", "DOC_PARSE", "ASR", "CHUNK")

# Which predicate each field type accepts. The rule is deliberately blunt: Cmp is
# for SCALAR columns, Fuzzy is for everything else. One sentence, and a model can
# follow it. TEXT accepts both so LIKE still works on a text column.
CMP_TYPES = frozenset({"SCALAR", "TEXT"})
FUZZY_TYPES = frozenset({"TEXT", "TEXT_CHUNKED", "IMAGE_SET", "AUDIO", "DOC_SCAN"})


@dataclass(frozen=True)
class FieldSpec:
    name: str                        # qualified "table.column"
    type: str                        # one of FIELD_TYPES
    description: str = ""
    unit: str = "row"                # row | page | chunk | segment
    fanout: float = 1.0              # expected units per row
    quantifier: str = "ANY"          # ANY | ALL
    derivation: str | None = None    # None if stored; else how to materialize it
    embeddable: bool = False         # can a cheap similarity pass narrow this?
    materialized_field: str | None = None  # physical column the derivation fills
    unit_table: str | None = None    # table holding the units (scan_page, audio_segment)
    source_field: str | None = None  # physical column holding the raw source

    @property
    def table(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def column(self) -> str:
        return self.name.split(".", 1)[1]

    @property
    def is_set_valued(self) -> bool:
        """True when one row means many model calls (pages, chunks, segments)."""
        return self.fanout > 1.0 or self.unit != "row"

    def accepts(self, predicate_kind: str) -> bool:
        return self.type in (CMP_TYPES if predicate_kind == "Cmp" else FUZZY_TYPES)


@dataclass
class Registry:
    """One corpus context: its tables, fields and join edges."""
    name: str
    description: str = ""
    tables: dict[str, str] = dc_field(default_factory=dict)          # table -> primary key
    fields: dict[str, FieldSpec] = dc_field(default_factory=dict)
    edges: list[tuple[str, str]] = dc_field(default_factory=list)    # (left_field, right_field)

    # ---- lookups ----
    def has(self, name: str) -> bool:
        return name in self.fields

    def get(self, name: str) -> FieldSpec:
        try:
            return self.fields[name]
        except KeyError:
            raise KeyError(f"unknown field {name!r} in schema {self.name!r}") from None

    def has_table(self, table: str) -> bool:
        return table in self.tables

    def fields_of(self, table: str) -> list[FieldSpec]:
        return [f for f in self.fields.values() if f.table == table]

    def has_edge(self, left: str, right: str) -> bool:
        return (left, right) in self.edges or (right, left) in self.edges

    def edges_text(self) -> str:
        return "; ".join(f"{a} = {b}" for a, b in self.edges)

    def suggest(self, name: str) -> str:
        """A 'did you mean' hint for a bad field name — goes into the repair prompt."""
        close = difflib.get_close_matches(name, self.fields, n=3, cutoff=0.5)
        if close:
            return "Did you mean " + " or ".join(close) + "?"
        table = name.split(".")[0]
        if self.has_table(table):
            names = [f.name for f in self.fields_of(table)][:8]
            return f"Fields on {table}: {', '.join(names)}"
        return f"Tables: {', '.join(sorted(self.tables))}"

    def join_path(self, src: str, dst: str) -> list[tuple[str, str]] | None:
        """Shortest chain of join edges from table `src` to table `dst`; None if none. Free."""
        if src == dst:
            return []
        adj: dict[str, list[tuple[str, tuple[str, str]]]] = {}
        for a, b in self.edges:
            ta, tb = a.split(".")[0], b.split(".")[0]
            adj.setdefault(ta, []).append((tb, (a, b)))
            adj.setdefault(tb, []).append((ta, (a, b)))
        prev: dict[str, tuple[str, tuple[str, str]] | None] = {src: None}
        frontier = [src]
        while frontier:
            nxt = []
            for t in frontier:
                for nb, edge in adj.get(t, []):
                    if nb in prev:
                        continue
                    prev[nb] = (t, edge)
                    if nb == dst:
                        path, cur = [], nb
                        while prev[cur] is not None:
                            p, e = prev[cur]  # type: ignore[misc]
                            path.append(e)
                            cur = p
                        return list(reversed(path))
                    nxt.append(nb)
            frontier = nxt
        return None

    # ---- rendering ----
    def render_for_prompt(self) -> str:
        """The schema as the model sees it. This is most of the compiler prompt. Free."""
        lines = [f"# {self.name}: {self.description}".rstrip(": ")]
        for table, pk in self.tables.items():
            lines.append(f"TABLE {table} (key {pk})")
            for f in self.fields_of(table):
                if f.type == "SCALAR":
                    detail = "SCALAR — use Cmp"
                else:
                    span = (f"{f.quantifier} of ~{f.fanout:g} {f.unit}s"
                            if f.is_set_valued else "whole value")
                    detail = f"{f.type} — use Fuzzy, judged over {span}"
                lines.append(f"  {f.name}: {detail}. {f.description}")
        lines.append("JOIN EDGES (the only legal joins): " + self.edges_text())
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tables": self.tables,
            "edges": [list(e) for e in self.edges],
            "fields": [
                {k: v for k, v in f.__dict__.items() if v not in (None, "", 1.0) or k in ("name", "type")}
                for f in self.fields.values()
            ],
        }


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def from_dict(d: dict[str, Any]) -> Registry:
    """Build a Registry from decoded schema JSON. Raises ValueError on a bad spec."""
    reg = Registry(name=d.get("name", "unnamed"), description=d.get("description", ""),
                   tables=dict(d.get("tables") or {}),
                   edges=[(a, b) for a, b in (d.get("edges") or [])])
    for raw in d.get("fields") or []:
        spec = FieldSpec(**{k: v for k, v in raw.items() if k in FieldSpec.__dataclass_fields__})
        if spec.type not in FIELD_TYPES:
            raise ValueError(f"{spec.name}: unknown type {spec.type!r}; allowed {FIELD_TYPES}")
        if spec.quantifier not in QUANTIFIERS:
            raise ValueError(f"{spec.name}: unknown quantifier {spec.quantifier!r}")
        if spec.derivation is not None and spec.derivation not in DERIVATIONS:
            raise ValueError(f"{spec.name}: unknown derivation {spec.derivation!r}")
        if spec.table not in reg.tables:
            raise ValueError(f"{spec.name}: table {spec.table!r} is not declared in 'tables'")
        reg.fields[spec.name] = spec
    for a, b in reg.edges:
        for side in (a, b):
            if side not in reg.fields:
                raise ValueError(f"join edge {a} = {b} references unknown field {side!r}")
    return reg


_CACHE: dict[str, Registry] = {}


def load(name_or_path: str | Path | None = None) -> Registry:
    """Load a schema by name ("courtlistener") or path. Cached per resolved path.

    Resolution order: the argument, then config.SCHEMA ($AMICUS_SCHEMA).
    Cost: one file read the first time, free afterwards.
    """
    target = name_or_path or config.SCHEMA
    path = Path(target)
    if not path.suffix:
        path = SCHEMA_DIR / f"{target}.json"
    key = str(path.resolve())
    if key not in _CACHE:
        if not path.exists():
            available = ", ".join(sorted(p.stem for p in SCHEMA_DIR.glob("*.json"))) or "none"
            raise FileNotFoundError(f"no schema at {path}; available: {available}")
        _CACHE[key] = from_dict(json.loads(path.read_text()))
    return _CACHE[key]


def available() -> list[str]:
    return sorted(p.stem for p in SCHEMA_DIR.glob("*.json"))
