"""Schema-aware checks for the canonical BQL AST.

Wire validation in :mod:`query_language.serde` proves that a payload has the
right shape. These checks prove that aliases, nested field paths, joins,
modal predicates, unnests and grouping make sense for the configured corpus.
The optimizer can therefore consume a successful query without guessing.

Validation is pure Python and linear in query size.
"""
from __future__ import annotations

from typing import Any

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
from .serde import DecodeError, table_refs


def validate(query: Query, registry: Any) -> list[DecodeError]:
    """Return schema/semantic errors, or ``[]`` when ``query`` is executable."""
    errors: list[DecodeError] = []
    refs = table_refs(query.source)
    aliases: dict[str, str] = {}

    if not refs:
        errors.append(DecodeError("$.source", "no_source", "the query has no source table"))
    for table in refs:
        if table.alias in aliases:
            errors.append(DecodeError(
                "$.source", "duplicate_alias", f"source alias {table.alias!r} is used more than once",
            ))
        aliases[table.alias] = table.name
        if not registry.has_table(table.name):
            errors.append(DecodeError(
                "$.source", "unknown_table",
                f"unknown table {table.name!r}; tables: {', '.join(sorted(registry.tables))}",
            ))

    for i, expr in enumerate(query.select):
        _expression(expr, f"$.select[{i}]", aliases, registry, errors)

    _source(query.source, "$.source", aliases, registry, errors)
    if query.where is not None:
        _condition(query.where, "$.where", aliases, registry, errors)

    for i, expr in enumerate(query.group_by):
        path = f"$.group_by[{i}]"
        if not isinstance(expr, (FieldRef, Unnest)):
            errors.append(DecodeError(
                path, "bad_group_by", "GROUP BY accepts fields (or an unnested field), not literals or aggregates",
            ))
        _expression(expr, path, aliases, registry, errors)

    has_aggregate = any(isinstance(expr, Aggregator) for expr in query.select)
    if has_aggregate:
        grouped = {_expression_key(expr) for expr in query.group_by}
        for i, expr in enumerate(query.select):
            if not isinstance(expr, Aggregator) and _expression_key(expr) not in grouped:
                errors.append(DecodeError(
                    f"$.select[{i}]", "ungrouped_select",
                    "a non-aggregate SELECT expression must also appear in GROUP BY",
                ))
    return errors


def _expression_key(expr: Any) -> str:
    if isinstance(expr, FieldRef):
        return f"field:{expr.source}:{'.'.join(expr.path)}"
    if isinstance(expr, Unnest):
        return f"unnest:{expr.ref.source}:{'.'.join(expr.ref.path)}"
    return repr(expr)


def _field_name(ref: FieldRef, aliases: dict[str, str]) -> str | None:
    table = aliases.get(ref.source)
    if table is None:
        return None
    suffix = ".".join(ref.path)
    return table if not suffix else f"{table}.{suffix}"


def _suspect_literal(value: Any, path: str, registry: Any, errors: list[DecodeError]) -> None:
    if isinstance(value, str) and registry.has(value):
        source, *parts = value.split(".")
        errors.append(DecodeError(
            path, "field_as_string",
            f"{value!r} is a string constant, not a field reference. Write "
            f'{{"kind":"FieldRef","source":"{source}","path":{parts!r}}}.',
        ))


def _field(ref: FieldRef, path: str, aliases: dict[str, str], registry: Any,
           errors: list[DecodeError]) -> Any:
    table = aliases.get(ref.source)
    if table is None:
        errors.append(DecodeError(
            path, "table_not_in_scope",
            f"unknown source alias {ref.source!r}; aliases: {', '.join(sorted(aliases))}",
        ))
        return None
    name = _field_name(ref, aliases)
    assert name is not None
    if not registry.has(name):
        errors.append(DecodeError(path, "unknown_field", f"no field {name!r}. {registry.suggest(name)}"))
        return None
    return registry.get(name)


def _expression(expr: Any, path: str, aliases: dict[str, str], registry: Any,
                errors: list[DecodeError]) -> Any:
    if isinstance(expr, FieldRef):
        return _field(expr, path, aliases, registry, errors)
    if isinstance(expr, Unnest):
        spec = _field(expr.ref, f"{path}.ref", aliases, registry, errors)
        if spec is not None and not spec.is_set_valued:
            errors.append(DecodeError(path, "unnest_scalar", f"cannot unnest scalar field {spec.name}"))
        return spec
    if isinstance(expr, Aggregator):
        if expr.op is not AggregatorOp.COUNT and expr.arg is None:
            errors.append(DecodeError(f"{path}.arg", "missing_aggregate_arg", f"{expr.op.value} needs an argument"))
        if isinstance(expr.arg, Aggregator):
            errors.append(DecodeError(f"{path}.arg", "nested_aggregate", "aggregates cannot be nested"))
        if expr.arg is not None:
            return _expression(expr.arg, f"{path}.arg", aliases, registry, errors)
        return None
    _suspect_literal(expr, path, registry, errors)
    return None


def _source(source: Any, path: str, aliases: dict[str, str], registry: Any,
            errors: list[DecodeError]) -> None:
    if isinstance(source, TableRef):
        return
    if not isinstance(source, Join):
        return
    condition = source.condition
    cpath = f"{path}.condition"
    if not isinstance(condition, Comparison):
        errors.append(DecodeError(cpath, "bad_join", "a join condition must compare two fields"))
    else:
        left, right = condition.field1, condition.field2
        if not isinstance(left, FieldRef) or not isinstance(right, FieldRef):
            errors.append(DecodeError(cpath, "bad_join", "a join compares two FieldRef expressions"))
        else:
            if condition.op is not ComparisonOperator.EQ:
                errors.append(DecodeError(
                    f"{cpath}.op", "bad_join_op", f"joins use '=', not {condition.op.value!r}",
                ))
            left_name = _field_name(left, aliases)
            right_name = _field_name(right, aliases)
            _field(left, f"{cpath}.field1", aliases, registry, errors)
            _field(right, f"{cpath}.field2", aliases, registry, errors)
            if (left_name and right_name and registry.has(left_name) and registry.has(right_name)
                    and not registry.has_edge(left_name, right_name)):
                errors.append(DecodeError(
                    cpath, "unknown_join_edge",
                    f"{left_name} = {right_name} is not a foreign key. Legal joins: {registry.edges_text()}",
                ))
    _source(source.left, f"{path}.left", aliases, registry, errors)
    _source(source.right, f"{path}.right", aliases, registry, errors)


def _condition(condition: Any, path: str, aliases: dict[str, str], registry: Any,
               errors: list[DecodeError]) -> None:
    match condition:
        case And(children) | Or(children):
            for i, child in enumerate(children):
                _condition(child, f"{path}.children[{i}]", aliases, registry, errors)
        case Not(child):
            _condition(child, f"{path}.child", aliases, registry, errors)
        case Fuzzy(field, _):
            if not isinstance(field, (FieldRef, Unnest)):
                errors.append(DecodeError(
                    f"{path}.field", "bad_fuzzy_field", "Fuzzy targets a FieldRef or Unnest expression",
                ))
                _expression(field, f"{path}.field", aliases, registry, errors)
                return
            spec = _expression(field, f"{path}.field", aliases, registry, errors)
            if spec is not None and not spec.accepts("Fuzzy"):
                errors.append(DecodeError(
                    f"{path}.field", "fuzzy_on_scalar",
                    f"{spec.name} is not modal content; use a comparison instead",
                ))
        case Comparison(_, left, right):
            for key, operand in (("field1", left), ("field2", right)):
                if isinstance(operand, FieldRef):
                    _exact_field(operand, f"{path}.{key}", aliases, registry, errors)
                elif isinstance(operand, (Unnest, Aggregator)):
                    errors.append(DecodeError(
                        f"{path}.{key}", "bad_comparison_expression",
                        "exact comparisons use field references and literals",
                    ))
                    _expression(operand, f"{path}.{key}", aliases, registry, errors)
                else:
                    _suspect_literal(operand, f"{path}.{key}", registry, errors)
        case InList(field, _) | Between(field, _, _) | Like(field, _):
            _exact_field(field, f"{path}.field", aliases, registry, errors)


def _exact_field(ref: FieldRef, path: str, aliases: dict[str, str], registry: Any,
                 errors: list[DecodeError]) -> None:
    spec = _field(ref, path, aliases, registry, errors)
    if spec is not None and not spec.accepts("Cmp"):
        errors.append(DecodeError(
            path, "exact_on_modal_field",
            f"{spec.name} is {spec.type} content a model must interpret; use Fuzzy instead",
        ))


def check(query: Query, registry: Any) -> Query:
    """Validate and return ``query``, raising ``BQLDecodeError`` on failure."""
    from .serde import BQLDecodeError

    errors = validate(query, registry)
    if errors:
        raise BQLDecodeError(errors)
    return query
