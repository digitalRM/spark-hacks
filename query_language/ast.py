from dataclasses import dataclass
from enum import Enum

type Literal = str | int | float | bool

@dataclass(frozen=True)
class FieldRef:
    source: str
    path: tuple[str, ...]  # empty = the variable itself; ('a', 'b') = source.a.b

@dataclass(frozen=True)
class Unnest:
    ref: FieldRef  # must resolve to Array[T]; expression type is T

class AggregatorOp(Enum):
    COUNT = 'count'
    SUM   = 'sum'
    AVG   = 'avg'
    MIN   = 'min'
    MAX   = 'max'

@dataclass(frozen=True)
class Aggregator:
    op: AggregatorOp
    arg: 'Expression | None'  # None = count(*)

type Expression = FieldRef | Literal | Unnest | Aggregator

class ComparisonOperator(Enum):
    LT = '<'
    LE = '<='
    EQ = '='
    NE = '!='
    GT = '>'
    GE = '>='

@dataclass(frozen=True)
class Comparison:
    op: ComparisonOperator
    field1: Expression
    field2: Expression

@dataclass(frozen=True)
class InList:
    field: FieldRef
    values: tuple[Literal, ...]

@dataclass(frozen=True)
class Between:
    field: FieldRef
    low: Literal
    high: Literal

@dataclass(frozen=True)
class Like:
    field: FieldRef
    pattern: str

type Exact = Comparison | InList | Between | Like

@dataclass(frozen=True)
class Fuzzy:
    field: Expression  # FieldRef for scalar/array (quantified); Unnest for element-wise
    text: str

@dataclass(frozen=True)
class And: children: tuple['Condition', ...]

@dataclass(frozen=True)
class Or: children: tuple['Condition', ...]

@dataclass(frozen=True)
class Not: child: 'Condition'

type Condition = Exact | Fuzzy | And | Or | Not

@dataclass(frozen=True)
class TableRef:
    name: str
    alias: str

@dataclass(frozen=True)
class Join:
    condition: Condition
    left: 'Source'
    right: 'Source'

type Source = TableRef | Join

@dataclass(frozen=True)
class Query:
    select: tuple[Expression, ...]
    source: Source
    where: Condition | None
    group_by: tuple[Expression, ...]
    limit: int | None

def pp_field_ref(f: FieldRef) -> str:
    return f.source if not f.path else f'{f.source}.{".".join(f.path)}'

def pp_source(s: Source) -> str:
    match s:
        case TableRef(name, alias): return f'{name} as {alias}'
        case Join(condition, left, right):
            return f'({pp_source(left)} join {pp_source(right)} on {pp_condition(condition)})'
        case _: raise TypeError(f'Unknown source: {s!r}')

def pp_expression(e: Expression) -> str:
    match e:
        case FieldRef() as f: return pp_field_ref(f)
        case Unnest(ref): return f'unnest({pp_field_ref(ref)})'
        case Aggregator(op, None): return f'{op.value}(*)'
        case Aggregator(op, arg): return f'{op.value}({pp_expression(arg)})'
        case str() as x: return f'"{x}"'
        case int() | float() | bool() as x: return f'{x}'
        case _: raise TypeError(f'Unknown expression: {e!r}')

def pp_condition(c: Condition) -> str:
    match c:
        case Comparison(op, left, right):
            return f'{pp_expression(left)} {op.value} {pp_expression(right)}'
        case InList(field, values):
            return f'{pp_field_ref(field)} in ({", ".join(pp_expression(v) for v in values)})'
        case Between(field, low, high):
            return f'{pp_field_ref(field)} between {pp_expression(low)} and {pp_expression(high)}'
        case Like(field, pattern):
            return f'{pp_field_ref(field)} like "{pattern}"'
        case Fuzzy(field, text):
            return f'fuzzy({pp_expression(field)}, "{text}")'
        case And(children):
            return f'({" and ".join(pp_condition(ch) for ch in children)})'
        case Or(children):
            return f'({" or ".join(pp_condition(ch) for ch in children)})'
        case Not(child):
            return f'not {pp_condition(child)}'
        case _: raise TypeError(f'Unknown condition: {c!r}')

def pp_query(q: Query) -> str:
    result = f'select {", ".join(pp_expression(e) for e in q.select)}\n'
    result += f'from {pp_source(q.source)}\n'
    if q.where is not None: result += f'where {pp_condition(q.where)}\n'
    if q.group_by: result += f'group by {", ".join(pp_expression(e) for e in q.group_by)}\n'
    if q.limit is not None: result += f'limit {q.limit}'
    return result

# fuzzy over an array field: model interprets quantification from the text
def example() -> Query: return Query(
    select=(
        FieldRef('cluster', ('id',)),
        FieldRef('cluster', ('case_name',)),
    ),
    source=Join(
        Comparison(
            ComparisonOperator.EQ,
            FieldRef('cluster', ('docket_id',)),
            FieldRef('docket', ('id',)),
        ),
        TableRef('cluster', 'cluster'),
        TableRef('docket', 'docket'),
    ),
    where=And((
        Comparison(
            ComparisonOperator.EQ,
            FieldRef('docket', ('court_id',)),
            'ca9',
        ),
        Fuzzy(
            FieldRef('cluster', ('scan_pages',)),
            'some page contains a photographic exhibit',
        ),
    )),
    group_by=(),
    limit=10,
)

# explicit unnest in select + where: element-grain result
def example_unnest() -> Query: return Query(
    select=(
        FieldRef('cluster', ('id',)),
        Unnest(FieldRef('cluster', ('scan_pages',))),
    ),
    source=Join(
        Comparison(
            ComparisonOperator.EQ,
            FieldRef('cluster', ('docket_id',)),
            FieldRef('docket', ('id',)),
        ),
        TableRef('cluster', 'cluster'),
        TableRef('docket', 'docket'),
    ),
    where=And((
        Comparison(
            ComparisonOperator.EQ,
            FieldRef('docket', ('court_id',)),
            'ca9',
        ),
        Fuzzy(Unnest(FieldRef('cluster', ('scan_pages',))), 'contains a photographic exhibit'),
    )),
    group_by=(),
    limit=10,
)

def example_aggregate() -> Query: return Query(
    select=(
        FieldRef('docket', ('court_id',)),
        Aggregator(AggregatorOp.COUNT, FieldRef('cluster', ('id',))),
    ),
    source=Join(
        Comparison(
            ComparisonOperator.EQ,
            FieldRef('cluster', ('docket_id',)),
            FieldRef('docket', ('id',)),
        ),
        TableRef('cluster', 'cluster'),
        TableRef('docket', 'docket'),
    ),
    where=None,
    group_by=(FieldRef('docket', ('court_id',)),),
    limit=None,
)
