from .ast import (
    FieldRef, Unnest, Aggregator, AggregatorOp, Expression,
    Comparison, InList, Between, Like, Fuzzy, And, Or, Not,
    Condition, TableRef, Join, Source, Query, ComparisonOperator,
    Date, is_iso_8601,
)
from .type_system import (
    TextType, ImageType, AudioType, TimestampType, DateTimeType,
    IntType, FloatType, BoolType,
    ModalType,
    ArrayType, SequenceType, OptionalType,
    FieldType, Schema,
)


type Env = dict[str, FieldType]

class TypeCheckError(Exception): pass

def field_access(t: FieldType, key: str) -> FieldType:
    match t:
        case dict() as obj:
            ft = obj.get(key)
            if ft is None: raise TypeCheckError(f'No field {key!r} on object')
            return ft
        case OptionalType(inner): return OptionalType(field_access(inner, key))
        case _: raise TypeCheckError(f'Cannot access field {key!r} on {t!r}')

def resolve_field_ref(source: str, path: tuple[str, ...], env: Env) -> FieldType:
    t = env.get(source)
    if t is None: raise TypeCheckError(f'Unknown source {source!r}')
    for key in path: t = field_access(t, key)
    return t

def modal_base(t: FieldType, ctx: str) -> ModalType:
    """Strip Optional/Array/Sequence wrappers and return the inner modal type."""
    match t:
        case OptionalType(inner):   return modal_base(inner, ctx)
        case ArrayType(element):    return modal_base(element, ctx)
        case SequenceType(element): return modal_base(element, ctx)
        case TextType() | ImageType() | AudioType(): return t
        case _: raise TypeCheckError(f'{ctx}: expected modal type (text/image/audio), got {t!r}')

def resolve_expression(e: Expression, env: Env) -> FieldType:
    match e:
        case FieldRef(source, path): return resolve_field_ref(source, path, env)
        case Unnest(ref):
            match resolve_field_ref(ref.source, ref.path, env):
                case ArrayType(el) | SequenceType(el):               return el
                case OptionalType(ArrayType(el) | SequenceType(el)): return el
                case t: raise TypeCheckError(f'Cannot unnest {t!r}')
        case Aggregator(AggregatorOp.COUNT, _): return IntType()
        case Aggregator(AggregatorOp.AVG, arg) if arg is not None:
            if not isinstance(scalar_base(resolve_expression(arg, env)), (IntType, FloatType)):
                raise TypeCheckError('avg requires numeric type')
            return FloatType()
        case Aggregator(AggregatorOp.SUM, arg) if arg is not None:
            t = scalar_base(resolve_expression(arg, env))
            if not isinstance(t, (IntType, FloatType)):
                raise TypeCheckError(f'sum requires numeric type, got {t!r}')
            return t
        case Aggregator(op, arg) if arg is not None:            # min / max: anything orderable
            t = scalar_base(resolve_expression(arg, env))
            if not isinstance(t, ORDERABLE):
                raise TypeCheckError(f'{op.value} requires an orderable type, got {t!r}')
            return t
        case Aggregator(op, None): raise TypeCheckError(f'{op.value} requires an argument')
        case Date():  return DateTimeType()
        case bool():  return BoolType()   # before int — bool subclasses int
        case int():   return IntType()
        case float(): return FloatType()
        case str():   return TextType()
        case _: raise TypeCheckError(f'Unknown expression: {e!r}')

# Ordering (<, <=, >, >=, between, min, max) needs a type that has an order. Text is not
# on this list, and no longer needs to be: a date column is DATE in the registry and
# DateTimeType here, so the one case that used to require lexicographic text ordering is
# typed, and `case_name >= "Graham"` goes back to being the mistake it looks like.
ORDERABLE = (IntType, FloatType, TimestampType, DateTimeType)
# Scalars otherwise compare across kinds -- `envelope.id = 123`, `flag = true` -- with
# SQLite's affinity semantics: the literal is coerced to the column. The registry cannot
# say whether an id column is text or integer (both exist in one corpus), so a strict
# type-equality rule would fail the most ordinary predicates on a guess. What is NOT
# comparable: modal content (image/audio) and collections, except by membership below.
SCALAR = (TextType, IntType, FloatType, BoolType, TimestampType, DateTimeType)

def scalar_base(t: FieldType) -> FieldType:
    """Strip Optional wrappers. Collections and modal types come back unchanged."""
    return scalar_base(t.inner) if isinstance(t, OptionalType) else t

def as_date(value: Expression, other: FieldType) -> bool:
    """Whether a bare ISO-8601 string standing next to a date should be read as one.

    The compiler writes `date_filed >= "2020-01-01"` far more often than it writes a
    Date node, and a query that means exactly the right thing should not fail on
    notation. The narrowness is the point: only a real calendar date, and only when the
    other side is already a DateTimeType, so nothing else is silently retyped.
    """
    return isinstance(other, DateTimeType) and is_iso_8601(value)

def operand_types(left: Expression, right: Expression, env: Env) -> tuple[FieldType, FieldType]:
    """Both sides of a comparison, with a bare ISO string next to a date read as a date."""
    lt = scalar_base(resolve_expression(left, env))
    rt = scalar_base(resolve_expression(right, env))
    if as_date(right, lt): rt = DateTimeType()
    if as_date(left, rt):  lt = DateTimeType()
    return lt, rt

def check_comparable(op: ComparisonOperator, lt: FieldType, rt: FieldType) -> None:
    lt, rt = scalar_base(lt), scalar_base(rt)
    # A set-valued scalar column against one scalar is membership: role_types = "judge",
    # party_ids in [...]. Only equality makes sense there.
    if isinstance(lt, (ArrayType, SequenceType)) and isinstance(rt, SCALAR):
        if op not in (ComparisonOperator.EQ, ComparisonOperator.NE):
            raise TypeCheckError(f'Operator {op.value} not supported on a set-valued field {lt!r}; use = or in')
        lt = scalar_base(lt.element)
    if not isinstance(lt, SCALAR) or not isinstance(rt, SCALAR):
        raise TypeCheckError(f'Cannot compare {lt!r} with {rt!r}; comparisons take scalar fields and literals')
    # A date is the one scalar that does NOT take affinity. `date_issued >= 2008` is not a
    # loose way of saying 2008-01-01: SQLite sorts every number before every string, so the
    # predicate is silently true for every row. A wrong date is worse than a refused one.
    if isinstance(lt, DateTimeType) != isinstance(rt, DateTimeType):
        raise TypeCheckError(
            f'Cannot compare {lt!r} with {rt!r}: a date column takes a Date literal or an '
            f'ISO-8601 string ("YYYY-MM-DD")')
    if op not in (ComparisonOperator.EQ, ComparisonOperator.NE) and not isinstance(lt, ORDERABLE):
        raise TypeCheckError(f'Operator {op.value} not supported for {lt!r}')

def check_condition(c: Condition, env: Env) -> None:
    match c:
        case Fuzzy(field, _):      modal_base(resolve_expression(field, env), 'fuzzy')
        case Comparison(op, l, r): check_comparable(op, *operand_types(l, r, env))
        case InList(field, vals):
            ft = resolve_field_ref(field.source, field.path, env)
            for v in vals: check_comparable(ComparisonOperator.EQ, *operand_types(field, v, env))
        case Between(field, low, high):
            ft = scalar_base(resolve_field_ref(field.source, field.path, env))
            if not isinstance(ft, ORDERABLE):
                raise TypeCheckError(f'between requires an orderable field, got {ft!r}')
            for bound in (low, high):
                check_comparable(ComparisonOperator.LE, *operand_types(field, bound, env))
        case Like(field, _):
            ft = scalar_base(resolve_field_ref(field.source, field.path, env))
            if isinstance(ft, (ArrayType, SequenceType)): ft = scalar_base(ft.element)   # any member matches
            if not isinstance(ft, TextType):
                raise TypeCheckError(f'like requires a text field, got {ft!r}')
        case And(children) | Or(children):
            for ch in children: check_condition(ch, env)
        case Not(child): check_condition(child, env)
        case _: raise TypeCheckError(f'Unknown condition: {c!r}')

def source_env(s: Source, schema: Schema) -> Env:
    """Every alias in the FROM clause, with its table type."""
    match s:
        case TableRef(name, alias):
            if name not in schema: raise TypeCheckError(f'Unknown table {name!r}')
            return {alias: schema[name]}
        case Join(_, left, right):
            return source_env(left, schema) | source_env(right, schema)
        case _: raise TypeCheckError(f'Unknown source: {s!r}')

def check_joins(s: Source, env: Env) -> None:
    """Join conditions resolve against the whole FROM clause, not just the subtree they
    hang off. Inner joins are one conjunction; the nesting is the model's parenthesisation,
    and `checks.validate` and `optimizer.lower` both already read it that way (lowering
    renders the tree flat). A condition on `doc_high` written one level below where
    `doc_high` is joined is a legal query with an odd tree, not a scope error."""
    if isinstance(s, Join):
        check_condition(s.condition, env)
        check_joins(s.left, env)
        check_joins(s.right, env)

def resolve_source(s: Source, schema: Schema) -> Env:
    env = source_env(s, schema)
    check_joins(s, env)
    return env

def typecheck(q: Query, schema: Schema) -> Env:
    """Validate q against schema. Returns the Env for downstream type lookups via resolve_expression."""
    env = resolve_source(q.source, schema)
    for e in q.select:   resolve_expression(e, env)
    if q.where is not None: check_condition(q.where, env)
    for e in q.group_by: resolve_expression(e, env)
    return env

from .ast import example, example_unnest, example_aggregate
from .type_system import FrozenDict

example_schema: Schema = FrozenDict.of({
    'cluster': FrozenDict.of({
        'id':         IntType(),
        'case_name':  TextType(),
        'docket_id':  IntType(),
        'scan_pages': ArrayType(ImageType()),
    }),
    'docket': FrozenDict.of({
        'id':       IntType(),
        'court_id': TextType(),
    }),
})

def example_typecheck()         -> Env: return typecheck(example(),           example_schema)
def example_unnest_typecheck()  -> Env: return typecheck(example_unnest(),    example_schema)
def example_aggregate_typecheck() -> Env: return typecheck(example_aggregate(), example_schema)
