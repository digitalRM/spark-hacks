from .ast import (
    FieldRef, Unnest, Aggregator, AggregatorOp, Expression,
    Comparison, InList, Between, Like, Fuzzy, And, Or, Not,
    Condition, TableRef, Join, Source, Query, ComparisonOperator,
)
from .type_system import (
    TextType, ImageType, AudioType, TimestampType,
    IntType, FloatType, BoolType,
    ModalType, NumericType,
    ArrayType, SequenceType, OptionalType,
    FieldType, ObjectType, Schema,
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

def unwrap_optional(t: FieldType) -> FieldType:
    """Strip OptionalType only -- unlike modal_base this doesn't also unwrap
    Array/Sequence, since a bare array/sequence still isn't a comparable
    scalar. Needed because a nullable field (the common case for any real
    date/numeric column) otherwise fails every isinstance/type-equality check
    below even though nullability has nothing to do with comparability."""
    return unwrap_optional(t.inner) if isinstance(t, OptionalType) else t

def resolve_expression(e: Expression, env: Env) -> FieldType:
    match e:
        case FieldRef(source, path): return resolve_field_ref(source, path, env)
        case Unnest(ref, path):
            match resolve_field_ref(ref.source, ref.path, env):
                case ArrayType(el) | SequenceType(el):               elem_t = el
                case OptionalType(ArrayType(el) | SequenceType(el)): elem_t = el
                case t: raise TypeCheckError(f'Cannot unnest {t!r}')
            for key in path: elem_t = field_access(elem_t, key)
            return elem_t
        case Aggregator(AggregatorOp.COUNT, _): return IntType()
        case Aggregator(AggregatorOp.AVG, arg) if arg is not None:
            if not isinstance(resolve_expression(arg, env), (IntType, FloatType)):
                raise TypeCheckError('avg requires numeric type')
            return FloatType()
        case Aggregator(op, arg) if arg is not None:
            t = resolve_expression(arg, env)
            if not isinstance(t, (IntType, FloatType)):
                raise TypeCheckError(f'{op.value} requires numeric type, got {t!r}')
            return t
        case Aggregator(op, None): raise TypeCheckError(f'{op.value} requires an argument')
        case bool():  return BoolType()   # before int — bool subclasses int
        case int():   return IntType()
        case float(): return FloatType()
        case str():   return TextType()
        case _: raise TypeCheckError(f'Unknown expression: {e!r}')

def check_comparable(op: ComparisonOperator, lt: FieldType, rt: FieldType) -> None:
    lt, rt = unwrap_optional(lt), unwrap_optional(rt)
    if op != ComparisonOperator.EQ and not isinstance(lt, (IntType, FloatType, TimestampType)):
        raise TypeCheckError(f'Operator {op.value} not supported for {lt!r}')
    if type(lt) != type(rt) and not (
        isinstance(lt, (IntType, FloatType)) and isinstance(rt, (IntType, FloatType))
    ):
        raise TypeCheckError(f'Type mismatch: {lt!r} vs {rt!r}')

def check_condition(c: Condition, env: Env) -> None:
    match c:
        case Fuzzy(field, _):      modal_base(resolve_expression(field, env), 'fuzzy')
        case Comparison(op, l, r): check_comparable(op, resolve_expression(l, env), resolve_expression(r, env))
        case InList(field, vals):
            ft = resolve_expression(field, env)
            for v in vals: check_comparable(ComparisonOperator.EQ, ft, resolve_expression(v, env))
        case Between(field, lo, hi):
            ft = unwrap_optional(resolve_expression(field, env))
            if not isinstance(ft, (IntType, FloatType, TimestampType)):
                raise TypeCheckError(f'between requires numeric/timestamp field, got {ft!r}')
        case Like(field, _):
            if not isinstance(unwrap_optional(resolve_expression(field, env)), TextType):
                raise TypeCheckError('like requires text field')
        case And(children) | Or(children):
            for ch in children: check_condition(ch, env)
        case Not(child): check_condition(child, env)
        case _: raise TypeCheckError(f'Unknown condition: {c!r}')

def resolve_source(s: Source, schema: Schema) -> Env:
    match s:
        case TableRef(name, alias):
            if name not in schema: raise TypeCheckError(f'Unknown table {name!r}')
            return {alias: schema[name]}
        case Join(condition, left, right):
            env = resolve_source(left, schema) | resolve_source(right, schema)
            check_condition(condition, env)
            return env
        case _: raise TypeCheckError(f'Unknown source: {s!r}')

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
