"""JSON wire format for the BQL AST in `grammar.py`.

Every dataclass becomes an object with a snake_case `type` tag plus its fields;
literals stay as plain JSON scalars; tuples become lists. This is the shape the
frontend (`frontend/lib/bql.ts`) is typed against, so keep the two in sync.

    >>> to_json(example())
    {"select": [{"type": "field_ref", "source": "cluster", "column": "id"}, ...],
     "source": {"type": "join", "condition": {...}, "left": "cluster", "right": "docket"},
     "where": {"type": "and", "children": [...]},
     "limit": 10}
"""

from __future__ import annotations

import json
from typing import Any

from .grammar import (
    And,
    Between,
    Comparison,
    Condition,
    Expression,
    FieldRef,
    Fuzzy,
    InList,
    Join,
    Like,
    Not,
    Or,
    Query,
    Source,
)


def expression_to_dict(e: Expression) -> Any:
    match e:
        case FieldRef(source, column):
            return {"type": "field_ref", "source": source, "column": column}
        case str() | int() | float() | bool():
            return e
        case _:
            raise TypeError(f"Unknown expression: {e!r}")


def condition_to_dict(c: Condition) -> dict[str, Any]:
    match c:
        case Comparison(op, field1, field2):
            return {
                "type": "comparison",
                "op": op.value,
                "field1": expression_to_dict(field1),
                "field2": expression_to_dict(field2),
            }
        case InList(field, values):
            return {
                "type": "in_list",
                "field": expression_to_dict(field),
                "values": [expression_to_dict(v) for v in values],
            }
        case Between(field, low, high):
            return {
                "type": "between",
                "field": expression_to_dict(field),
                "low": expression_to_dict(low),
                "high": expression_to_dict(high),
            }
        case Like(field, pattern):
            return {"type": "like", "field": expression_to_dict(field), "pattern": pattern}
        case Fuzzy(fields, text):
            return {
                "type": "fuzzy",
                "field": [expression_to_dict(f) for f in fields],
                "text": text,
            }
        case And(children):
            return {"type": "and", "children": [condition_to_dict(x) for x in children]}
        case Or(children):
            return {"type": "or", "children": [condition_to_dict(x) for x in children]}
        case Not(child):
            return {"type": "not", "child": condition_to_dict(child)}
        case _:
            raise TypeError(f"Unknown condition: {c!r}")


def source_to_dict(s: Source) -> Any:
    match s:
        case str():
            return s
        case Join(condition, left, right):
            return {
                "type": "join",
                "condition": condition_to_dict(condition),
                "left": source_to_dict(left),
                "right": source_to_dict(right),
            }
        case _:
            raise TypeError(f"Unknown source: {s!r}")


def query_to_dict(q: Query) -> dict[str, Any]:
    return {
        "select": [expression_to_dict(e) for e in q.select],
        "source": source_to_dict(q.source),
        "where": condition_to_dict(q.where) if q.where is not None else None,
        "limit": q.limit,
    }


def to_json(q: Query, **kwargs: Any) -> str:
    return json.dumps(query_to_dict(q), **kwargs)
