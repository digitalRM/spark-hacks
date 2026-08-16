"""Lossless JSON serialization for the canonical :mod:`query_language.ast`.

GitHub's v2 AST represents nested data with ``FieldRef.path``, names every table
with a ``TableRef``/alias, and adds ``Unnest``, aggregation and ``group_by``.
This module is the process boundary used by the compiler, frontend and the
downstream optimizer. It deliberately contains no second AST of its own.

All functions are pure Python and linear in the query size.
"""
from __future__ import annotations

from typing import Any, Iterator

from .ast import (
    Aggregator,
    AggregatorOp,
    And,
    Between,
    Comparison,
    ComparisonOperator,
    FieldRef,
    Fuzzy,
    InList,
    Join,
    Like,
    Not,
    Or,
    Query,
    TableRef,
    Unnest,
)

BQL_VERSION = "2.0"

CONDITIONS = (Comparison, InList, Between, Like, Fuzzy, And, Or, Not)
EXACTS = (Comparison, InList, Between, Like)
CONDITION_KINDS = tuple(cls.__name__ for cls in CONDITIONS)
EXPRESSION_KINDS = ("FieldRef", "Unnest", "Aggregator")
SOURCE_KINDS = ("TableRef", "Join")
OPS = tuple(op.value for op in ComparisonOperator)
AGG_OPS = tuple(op.value for op in AggregatorOp)
_LITERAL = (str, int, float, bool)

EXCLUDED: dict[str, str] = {
    "Sem": "the optimizer refines Fuzzy into Sem",
    "Sim": "the optimizer refines Fuzzy into Sim",
    "Visual": "the optimizer refines Fuzzy on an image field into Visual",
    "Audio": "the optimizer refines Fuzzy on an audio field into Audio",
    "Sort": "ORDER BY is not present in the canonical AST yet",
    "Let": "query variables are not present in the canonical AST yet",
    "Having": "filter before aggregating",
}


class DecodeError(dict):
    """One structured wire-format or schema-validation error."""

    def __init__(self, path: str, code: str, message: str) -> None:
        super().__init__(path=path, code=code, message=message)

    def __str__(self) -> str:
        return f"{self['path']}: {self['message']}"


class BQLDecodeError(ValueError):
    """Raised when a payload cannot be decoded as the canonical AST."""

    def __init__(self, errors: list[DecodeError]) -> None:
        self.errors = errors
        super().__init__("; ".join(str(error) for error in errors[:4]))


def encode(node: Any) -> Any:
    """Return a JSON-compatible, lossless representation of an AST node."""
    match node:
        case Query(select, source, where, group_by, limit):
            return {
                "kind": "Query",
                "select": [encode(expr) for expr in select],
                "source": encode(source),
                "where": None if where is None else encode(where),
                "group_by": [encode(expr) for expr in group_by],
                "limit": limit,
            }
        case TableRef(name, alias):
            return {"kind": "TableRef", "name": name, "alias": alias}
        case Join(condition, left, right):
            return {
                "kind": "Join",
                "condition": encode(condition),
                "left": encode(left),
                "right": encode(right),
            }
        case FieldRef(source, path):
            return {"kind": "FieldRef", "source": source, "path": list(path)}
        case Unnest(ref):
            return {"kind": "Unnest", "ref": encode(ref)}
        case Aggregator(op, arg):
            return {
                "kind": "Aggregator",
                "op": op.value,
                "arg": None if arg is None else encode(arg),
            }
        case Comparison(op, field1, field2):
            return {
                "kind": "Comparison",
                "op": op.value,
                "field1": encode(field1),
                "field2": encode(field2),
            }
        case InList(field, values):
            return {"kind": "InList", "field": encode(field), "values": list(values)}
        case Between(field, low, high):
            return {"kind": "Between", "field": encode(field), "low": low, "high": high}
        case Like(field, pattern):
            return {"kind": "Like", "field": encode(field), "pattern": pattern}
        case Fuzzy(field, text):
            return {"kind": "Fuzzy", "field": encode(field), "text": text}
        case And(children):
            return {"kind": "And", "children": [encode(child) for child in children]}
        case Or(children):
            return {"kind": "Or", "children": [encode(child) for child in children]}
        case Not(child):
            return {"kind": "Not", "child": encode(child)}
        case str() | int() | float() | bool():
            return node
        case _:
            raise TypeError(f"cannot encode {node!r}")


def decode(obj: Any) -> Query:
    """Decode a JSON value into ``Query`` or raise ``BQLDecodeError``."""
    errors: list[DecodeError] = []
    query = _query(obj, "$", errors)
    if errors:
        raise BQLDecodeError(errors)
    assert query is not None
    return query


def decode_errors(obj: Any) -> list[DecodeError]:
    """Return all wire-format errors without raising."""
    errors: list[DecodeError] = []
    _query(obj, "$", errors)
    return errors


def _obj(value: Any, path: str, expected: str, errors: list[DecodeError]) -> dict | None:
    if not isinstance(value, dict):
        errors.append(DecodeError(path, "not_an_object", f"expected {expected}, got {_name(value)}"))
        return None
    if not isinstance(value.get("kind"), str):
        errors.append(DecodeError(path, "missing_kind", 'every node needs a "kind" string'))
        return None
    return value


def _keys(value: dict, path: str, allowed: set[str], errors: list[DecodeError]) -> None:
    """Unknown keys are ignored, not errors. A model that writes `"operator":"and"` on an
    And, or `"order_by":[]` on a Query, has still produced a valid query, and rejecting it
    costs a full repair round trip for nothing. A *misspelled required* key is still caught,
    because the required key is then missing and its own check fires."""
    return None


def _query(value: Any, path: str, errors: list[DecodeError]) -> Query | None:
    obj = _obj(value, path, "a Query object", errors)
    if obj is None:
        return None
    if obj["kind"] != "Query":
        errors.append(DecodeError(path, "wrong_kind", f"expected Query, got {obj['kind']}"))
        return None
    allowed = {"select", "source", "where", "group_by", "limit"}
    _keys(obj, path, allowed, errors)
    for key in ("select", "source", "where", "group_by", "limit"):
        if key not in obj:
            errors.append(DecodeError(f"{path}.{key}", "missing_key", f"Query requires {key!r}"))

    raw_select = obj.get("select")
    if not isinstance(raw_select, list) or not raw_select:
        errors.append(DecodeError(f"{path}.select", "empty_select", "SELECT needs a non-empty list"))
        raw_select = []
    select = tuple(_expression(item, f"{path}.select[{i}]", errors)
                   for i, item in enumerate(raw_select))

    source = _source(obj.get("source"), f"{path}.source", errors)
    where = None if obj.get("where") is None else _condition(obj.get("where"), f"{path}.where", errors)

    raw_group = obj.get("group_by")
    if not isinstance(raw_group, list):
        errors.append(DecodeError(f"{path}.group_by", "expected_list", "group_by must be a list"))
        raw_group = []
    group_by = tuple(_expression(item, f"{path}.group_by[{i}]", errors)
                     for i, item in enumerate(raw_group))

    limit = obj.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        errors.append(DecodeError(f"{path}.limit", "expected_int", "limit must be an integer or null"))
        limit = None
    elif isinstance(limit, int) and limit < 0:
        errors.append(DecodeError(f"{path}.limit", "negative_limit", "limit must be non-negative"))

    return Query(select=select, source=source, where=where, group_by=group_by, limit=limit)


def _source(value: Any, path: str, errors: list[DecodeError]) -> Any:
    obj = _obj(value, path, "a TableRef or Join object", errors)
    if obj is None:
        return TableRef("", "")
    kind = obj["kind"]
    if kind == "TableRef":
        _keys(obj, path, {"name", "alias"}, errors)
        name = _nonempty_string(obj.get("name"), f"{path}.name", "table name", errors)
        alias = _nonempty_string(obj.get("alias"), f"{path}.alias", "table alias", errors)
        return TableRef(name, alias)
    if kind == "Join":
        _keys(obj, path, {"condition", "left", "right"}, errors)
        for key in ("condition", "left", "right"):
            if key not in obj:
                errors.append(DecodeError(f"{path}.{key}", "missing_key", f"Join requires {key!r}"))
        return Join(
            _condition(obj.get("condition"), f"{path}.condition", errors),
            _source(obj.get("left"), f"{path}.left", errors),
            _source(obj.get("right"), f"{path}.right", errors),
        )
    errors.append(DecodeError(path, "wrong_kind", f"a source is TableRef or Join, got {kind!r}"))
    return TableRef("", "")


def _expression(value: Any, path: str, errors: list[DecodeError]) -> Any:
    if isinstance(value, _LITERAL):
        return value
    obj = _obj(value, path, "an expression", errors)
    if obj is None:
        return ""
    kind = obj["kind"]
    if kind == "FieldRef":
        return _field_ref(obj, path, errors)
    if kind == "Unnest":
        _keys(obj, path, {"ref"}, errors)
        return Unnest(_field_ref(obj.get("ref"), f"{path}.ref", errors))
    if kind == "Aggregator":
        _keys(obj, path, {"op", "arg"}, errors)
        op = obj.get("op")
        if op not in AGG_OPS:
            errors.append(DecodeError(f"{path}.op", "unknown_aggregator", f"aggregator must be one of {AGG_OPS}"))
            op = AggregatorOp.COUNT.value
        arg = None if obj.get("arg") is None else _expression(obj.get("arg"), f"{path}.arg", errors)
        if op != AggregatorOp.COUNT.value and arg is None:
            errors.append(DecodeError(f"{path}.arg", "missing_aggregate_arg", f"{op} requires an argument"))
        return Aggregator(AggregatorOp(op), arg)
    errors.append(DecodeError(path, "wrong_kind", f"expected an expression, got {kind!r}"))
    return ""


def _field_ref(value: Any, path: str, errors: list[DecodeError]) -> FieldRef:
    obj = value if isinstance(value, dict) else _obj(value, path, "a FieldRef", errors)
    if obj is None:
        return FieldRef("", ())
    if obj.get("kind") != "FieldRef":
        errors.append(DecodeError(path, "wrong_kind", f"expected FieldRef, got {obj.get('kind')!r}"))
        return FieldRef("", ())
    _keys(obj, path, {"source", "path"}, errors)
    source = _nonempty_string(obj.get("source"), f"{path}.source", "source alias", errors)
    raw_path = obj.get("path")
    if not isinstance(raw_path, list) or any(not isinstance(part, str) or not part for part in raw_path):
        errors.append(DecodeError(f"{path}.path", "bad_field_path", "FieldRef.path must be a list of non-empty strings"))
        raw_path = []
    return FieldRef(source, tuple(raw_path))


def _condition(value: Any, path: str, errors: list[DecodeError]) -> Any:
    obj = _obj(value, path, "a condition", errors)
    if obj is None:
        return And(())
    kind = obj["kind"]
    if kind in EXCLUDED:
        errors.append(DecodeError(path, "excluded_kind", f"{kind} is not compiler BQL: {EXCLUDED[kind]}"))
        return And(())
    if kind not in CONDITION_KINDS:
        errors.append(DecodeError(path, "unknown_kind", f"unknown condition {kind!r}"))
        return And(())

    if kind == "Comparison":
        _keys(obj, path, {"op", "field1", "field2"}, errors)
        op = obj.get("op")
        if op not in OPS:
            errors.append(DecodeError(f"{path}.op", "unknown_op", f"operator must be one of {OPS}"))
            op = ComparisonOperator.EQ.value
        return Comparison(
            ComparisonOperator(op),
            _expression(obj.get("field1"), f"{path}.field1", errors),
            _expression(obj.get("field2"), f"{path}.field2", errors),
        )
    if kind == "InList":
        _keys(obj, path, {"field", "values"}, errors)
        raw = obj.get("values")
        if not isinstance(raw, list) or not raw:
            errors.append(DecodeError(f"{path}.values", "missing_values", "InList needs a non-empty values list"))
            raw = []
        return InList(
            _field_ref(obj.get("field"), f"{path}.field", errors),
            tuple(_literal(item, f"{path}.values[{i}]", errors) for i, item in enumerate(raw)),
        )
    if kind == "Between":
        _keys(obj, path, {"field", "low", "high"}, errors)
        return Between(
            _field_ref(obj.get("field"), f"{path}.field", errors),
            _literal(obj.get("low"), f"{path}.low", errors),
            _literal(obj.get("high"), f"{path}.high", errors),
        )
    if kind == "Like":
        _keys(obj, path, {"field", "pattern"}, errors)
        pattern = obj.get("pattern")
        if not isinstance(pattern, str):
            errors.append(DecodeError(f"{path}.pattern", "like_pattern", "Like.pattern must be a string"))
            pattern = ""
        return Like(_field_ref(obj.get("field"), f"{path}.field", errors), pattern)
    if kind == "Fuzzy":
        _keys(obj, path, {"field", "text"}, errors)
        if isinstance(obj.get("field"), list):
            errors.append(DecodeError(f"{path}.field", "fuzzy_single_field", "Fuzzy.field is one expression in AST v2, not a list"))
        field = _expression(obj.get("field"), f"{path}.field", errors)
        text_value = obj.get("text")
        if not isinstance(text_value, str) or not text_value.strip():
            errors.append(DecodeError(f"{path}.text", "empty_text", "Fuzzy needs non-empty text"))
            text_value = ""
        return Fuzzy(field, text_value)
    if kind in ("And", "Or"):
        _keys(obj, path, {"children"}, errors)
        raw = obj.get("children")
        if not isinstance(raw, list) or not raw:
            errors.append(DecodeError(f"{path}.children", "empty_children", f"{kind} needs at least one child"))
            raw = []
        children = tuple(_condition(child, f"{path}.children[{i}]", errors)
                         for i, child in enumerate(raw))
        return And(children) if kind == "And" else Or(children)
    _keys(obj, path, {"child"}, errors)
    if "child" not in obj:
        errors.append(DecodeError(f"{path}.child", "missing_key", "Not requires child"))
    return Not(_condition(obj.get("child"), f"{path}.child", errors))


def _literal(value: Any, path: str, errors: list[DecodeError]) -> Any:
    if not isinstance(value, _LITERAL):
        errors.append(DecodeError(path, "expected_literal", "expected a string, number or boolean"))
        return ""
    return value


def _nonempty_string(value: Any, path: str, label: str, errors: list[DecodeError]) -> str:
    if not isinstance(value, str) or not value:
        errors.append(DecodeError(path, "expected_string", f"{label} must be a non-empty string"))
        return ""
    return value


def _name(value: Any) -> str:
    return {dict: "an object", list: "a list", str: "a string", bool: "a boolean",
            int: "a number", float: "a number", type(None): "null"}.get(type(value), type(value).__name__)


def normalize(node: Any) -> Any:
    """Canonicalize child sequences to tuples for stable equality/hashing."""
    match node:
        case Query(select, source, where, group_by, limit):
            return Query(tuple(normalize(expr) for expr in select), normalize(source),
                         None if where is None else normalize(where),
                         tuple(normalize(expr) for expr in group_by), limit)
        case FieldRef(source, path):
            return FieldRef(source, tuple(path))
        case TableRef():
            return node
        case Join(condition, left, right):
            return Join(normalize(condition), normalize(left), normalize(right))
        case Unnest(ref):
            return Unnest(normalize(ref))
        case Aggregator(op, arg):
            return Aggregator(op, None if arg is None else normalize(arg))
        case And(children):
            return And(tuple(normalize(child) for child in children))
        case Or(children):
            return Or(tuple(normalize(child) for child in children))
        case Not(child):
            return Not(normalize(child))
        case Fuzzy(field, text_value):
            return Fuzzy(normalize(field), text_value)
        case InList(field, values):
            return InList(normalize(field), tuple(values))
        case Comparison(op, left, right):
            return Comparison(op, normalize(left), normalize(right))
        case Between(field, low, high):
            return Between(normalize(field), low, high)
        case Like(field, pattern):
            return Like(normalize(field), pattern)
        case _:
            return node


def walk_condition(condition: Any) -> Iterator[Any]:
    """Yield condition nodes in pre-order."""
    if condition is None:
        return
    yield condition
    match condition:
        case And(children) | Or(children):
            for child in children:
                yield from walk_condition(child)
        case Not(child):
            yield from walk_condition(child)


def table_refs(source: Any) -> list[TableRef]:
    """Return table references in source-tree order."""
    match source:
        case TableRef() as table:
            return [table]
        case Join(_, left, right):
            return table_refs(left) + table_refs(right)
        case _:
            return []


def tables_in(source: Any) -> list[str]:
    """Return physical table names in source-tree order."""
    return [table.name for table in table_refs(source)]


def aliases_in(source: Any) -> dict[str, str]:
    """Return ``alias -> physical table`` for a source tree."""
    return {table.alias: table.name for table in table_refs(source)}


def join_conditions(source: Any) -> Iterator[Any]:
    """Yield every join condition in a source tree."""
    if isinstance(source, Join):
        yield source.condition
        yield from join_conditions(source.left)
        yield from join_conditions(source.right)


def field_refs(node: Any) -> Iterator[FieldRef]:
    """Yield every ``FieldRef`` reachable from an AST node."""
    match node:
        case FieldRef() as ref:
            yield ref
        case Unnest(ref):
            yield ref
        case Aggregator(_, arg) if arg is not None:
            yield from field_refs(arg)
        case Query(select, source, where, group_by, _):
            for expr in (*select, *group_by):
                yield from field_refs(expr)
            yield from field_refs(source)
            if where is not None:
                yield from field_refs(where)
        case Join(condition, left, right):
            yield from field_refs(condition)
            yield from field_refs(left)
            yield from field_refs(right)
        case Comparison(_, left, right):
            yield from field_refs(left)
            yield from field_refs(right)
        case InList(ref, _) | Between(ref, _, _) | Like(ref, _):
            yield ref
        case Fuzzy(field, _):
            yield from field_refs(field)
        case And(children) | Or(children):
            for child in children:
                yield from field_refs(child)
        case Not(child):
            yield from field_refs(child)


def qualified(ref: FieldRef) -> str:
    """Return an alias-qualified dotted field path."""
    suffix = ".".join(ref.path)
    return ref.source if not suffix else f"{ref.source}.{suffix}"


def predicates(query: Query) -> list[Any]:
    """Return every exact/fuzzy WHERE leaf."""
    return [node for node in walk_condition(query.where) if isinstance(node, (*EXACTS, Fuzzy))]
