from .ast import (
    FieldRef, Unnest, Aggregator, AggregatorOp, Expression,
    Comparison, InList, Between, Like, Fuzzy, And, Or, Not,
    Condition, TableRef, Join, Source, Query, ComparisonOperator,
)
from .type_system import (
    TextType, ImageType, AudioType, TimestampType,
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
    if op not in (ComparisonOperator.EQ, ComparisonOperator.NE) and not isinstance(lt, (IntType, FloatType, TimestampType)):
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
            ft = resolve_field_ref(field.source, field.path, env)
            for v in vals: check_comparable(ComparisonOperator.EQ, ft, resolve_expression(v, env))
        case Between(field, _, _):
            ft = resolve_field_ref(field.source, field.path, env)
            if not isinstance(ft, (IntType, FloatType, TimestampType)):
                raise TypeCheckError(f'between requires numeric/timestamp field, got {ft!r}')
        case Like(field, _):
            if not isinstance(resolve_field_ref(field.source, field.path, env), TextType):
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
