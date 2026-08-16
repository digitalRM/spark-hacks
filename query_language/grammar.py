from dataclasses import dataclass
from enum import Enum

type Literal = str | int | float | bool

@dataclass(frozen=True)
class FieldRef:
    source : str
    column : str

"""Future directions -- add exact and semantic aggregation."""

type Expression = FieldRef | Literal

class ComparisonOperator(Enum):
    LT = '<'
    LE = '<='
    EQ = '='
    GT = '>'
    GE = '>='

@dataclass(frozen=True)
class Comparison:
    op : ComparisonOperator
    field1 : Expression
    field2 : Expression

@dataclass(frozen=True)
class InList:
    field : FieldRef
    values : tuple[Literal, ...]

@dataclass(frozen=True)
class Between:
    field : FieldRef
    low : Literal
    high : Literal

@dataclass(frozen=True)
class Like:
    field : FieldRef
    pattern : str

type Exact = Comparison | InList | Between | Like

@dataclass(frozen=True)
class Fuzzy:
    field : tuple[FieldRef, ...]
    text : str

@dataclass(frozen=True)
class And: children : tuple['Condition', ...]

@dataclass(frozen=True)
class Or: children : tuple['Condition', ...]

@dataclass(frozen=True)
class Not: child: 'Condition'

Condition = Exact | Fuzzy | And | Or | Not

@dataclass(frozen=True)
class Join:
    condition : 'Condition'
    left : 'Source'
    right : 'Source'

type Source = str | Join

@dataclass(frozen=True)
class Query:
    select : tuple[Expression, ...]
    source : Source
    where : Condition | None
    limit : int | None

def pp_source(s : Source) -> str:
    match s:
        case str():
            return s
        case Join(condition, left, right):
            return f'({pp_source(left)} join {pp_source(right)} on {pp_condition(condition)})'
        case _:
            raise TypeError(f'Unknown source: {s!r}')

def pp_expression(e : Expression) -> str:
    match e:
        case FieldRef(source, column):
            return f'{source}.{column}'
        case str() as x:
            return f'"{x}"'
        case int() | float() | bool() as x:
            return f'{x}'
        case _:
            raise TypeError(f'Unknown expression: {e!r}')

def pp_condition(c : Condition) -> str:
    match c:
        case Comparison(op, left, right):
            return f'{pp_expression(left)} {op.value} {pp_expression(right)}'
        case InList(field, values):
            items = ', '.join(pp_expression(v) for v in values)
            return f'{pp_expression(field)} in ({items})'
        case Between(field, low, high):
            return f'{pp_expression(field)} between {pp_expression(low)} and {pp_expression(high)}'
        case Like(field, pattern):
            return f'{pp_expression(field)} like "{pattern}"'
        case Fuzzy(fields, text):
            items = ', '.join(pp_expression(f) for f in fields)
            return f'fuzzy({items}, "{text}")'
        case And(children):
            items = tuple(children)
            return f'({" and ".join(pp_condition(child) for child in items)})'
        case Or(children):
            items = tuple(children)
            return f'({" or ".join(pp_condition(child) for child in items)})'
        case Not(child):
            return f'not {pp_condition(child)}'
        case _:
            raise TypeError(f'Unknown condition: {c!r}')

# Pretty-print a query.
def pp_query(q : Query) -> str:
    result = ''
    result += f'select {", ".join(pp_expression(e) for e in q.select)}\n'
    result += f'from {pp_source(q.source)}\n'
    if q.where is not None:
        result += f'where {pp_condition(q.where)}\n'
    if q.limit is not None:
        result += f'limit {q.limit}'
    return result

def example() -> Query: return Query(
    select=(
        FieldRef('cluster', 'id'),
        FieldRef('cluster', 'case_name')
    ),
    source=Join(
        Comparison(
            ComparisonOperator.EQ,
            FieldRef('cluster', 'docket_id'),
            FieldRef('docket', 'id')
        ),
        'cluster',
        'docket'
    ),
    where=And([
        Comparison(
            ComparisonOperator.EQ,
            FieldRef('docket', 'court_id'),
            'ca9'
        ),
        Fuzzy(
            (FieldRef('cluster', 'scan_pages'),),
            'contains a photographic exhibit'
        ),
    ]),
    limit=10,
)
