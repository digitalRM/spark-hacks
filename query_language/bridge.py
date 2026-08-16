"""Registry -> Schema: the conversion between the compiler's view of the corpus and the
typechecker's.

`schema.Registry` describes the corpus for the *compiler*: flat qualified names, one
modality string per field, join edges, prompt rendering. `type_system.Schema` describes it
for the *typechecker*: nested objects, real element types, arrays versus sequences. Both
are projections of the same JSON, and until this file nothing converted between them,
which is why `typecheck()` had no caller outside its own tests.

    TEXT          TextType()
    IMAGE_SET     Array | Sequence of ImageType()
    DOC_SCAN      Array | Sequence of TextType()     DOC_PARSE renders the pages as text
    TEXT_CHUNKED  Array | Sequence of TextType()
    AUDIO         Array | Sequence of AudioType()
    SCALAR        scalar_type(), plus a collection wrapper when the field is set-valued

Array versus Sequence is the one distinction the registry cannot state and the optimizer
needs. The rule: units that are already rows in the corpus are an Array; units that do not
exist until a derivation induces them are a Sequence. `scan_page` rows exist before
RASTERIZE fills in `image_path`, so pages are an Array. `audio_segment` rows do not exist
until ASR produces them, so segments are a Sequence.

Cost: one pass over the field list, no I/O.
"""
from __future__ import annotations

from typing import Any

from .schema import FieldSpec, Registry
from .type_system import (
    ArrayType, AudioType, FieldType, FloatType, FrozenDict, ImageType, IntType,
    ObjectType, Schema, SequenceType, TextType,
)

class BridgeError(Exception): pass

# Derivations that *invent* their units. ASR turns one recording into segments that were
# not rows before, and CHUNK splits one text into chunks that were not rows before.
# RASTERIZE and DOC_PARSE only fill a column on scan_page rows that already exist.
INDUCING = frozenset({'ASR', 'CHUNK'})

ELEMENT: dict[str, FieldType] = {
    'IMAGE_SET':    ImageType(),
    'DOC_SCAN':     TextType(),
    'TEXT_CHUNKED': TextType(),
    'AUDIO':        AudioType(),
}

# STOPGAP. FieldSpec collapses every non-modal column to the single string "SCALAR", so a
# count and a case name are indistinguishable to this function. Name the numeric ones until
# a FieldSpec can carry its own scalar type, then delete these two sets.
#
# Ids are deliberately NOT inferred: court.id is TEXT and docket.id is INTEGER in the same
# corpus, and guessing wrong turns `docket.court_id = "ca9"` -- the most common predicate in
# the demo -- into a type error. Dates are deliberately not inferred either; see the module
# note in api/driver.py on the one comparison this leaves unable to typecheck.
INT_COLUMNS = frozenset({'token_count', 'page_no', 'dpi', 'year', 'depth'})
FLOAT_COLUMNS = frozenset({'duration_s', 'start_s', 'end_s'})

def scalar_type(column: str) -> FieldType:
    """The structural type of a SCALAR column, keyed on its leaf name."""
    leaf = column.rsplit('.', 1)[-1]
    if leaf in INT_COLUMNS: return IntType()
    if leaf in FLOAT_COLUMNS: return FloatType()
    return TextType()

def collection(spec: FieldSpec, element: FieldType) -> FieldType:
    """Array when the units are rows in the corpus, Sequence when a derivation induces them."""
    return SequenceType(element) if spec.derivation in INDUCING else ArrayType(element)

def field_type(spec: FieldSpec) -> FieldType:
    """The structural type of one registry field."""
    if spec.type in ELEMENT: return collection(spec, ELEMENT[spec.type])
    if spec.type == 'TEXT': return TextType()
    scalar = scalar_type(spec.column)
    # dataform types a list of scalars as SCALAR carrying a fanout -- role_types, party_ids.
    return collection(spec, scalar) if spec.is_set_valued else scalar

def insert(tree: dict[str, Any], path: tuple[str, ...], t: FieldType, name: str) -> None:
    """Fold a dotted path into nested dicts, in place. Raises when a leaf and an object collide."""
    head, rest = path[0], path[1:]
    if not rest:
        if isinstance(tree.get(head), dict):
            raise BridgeError(f'{name}: {head!r} is both a field and an object on the same table')
        tree[head] = t
        return
    child = tree.setdefault(head, {})
    if not isinstance(child, dict):
        raise BridgeError(f'{name}: {head!r} is both a field and an object on the same table')
    insert(child, rest, t, name)

def freeze(tree: dict[str, Any]) -> ObjectType:
    return FrozenDict.of({k: freeze(v) if isinstance(v, dict) else v for k, v in tree.items()})

def registry_to_schema(reg: Registry) -> Schema:
    """The registry as the typechecker sees it. One pass over the fields, no I/O."""
    trees: dict[str, dict[str, Any]] = {table: {} for table in reg.tables}
    for spec in reg.fields.values():
        insert(trees.setdefault(spec.table, {}), tuple(spec.column.split('.')),
               field_type(spec), spec.name)
    return FrozenDict.of({table: freeze(tree) for table, tree in trees.items()})

from .schema import load

def example_courtlistener() -> Schema: return registry_to_schema(load('courtlistener'))

def example_dataform() -> Schema: return registry_to_schema(load('dataform'))

def _smoke() -> None:
    """python3 -m query_language.bridge"""
    for name, build in (('courtlistener', example_courtlistener), ('dataform', example_dataform)):
        s = build()
        print(f'{name}: {len(s)} tables')
        for table in sorted(s):
            leaves = sum(1 for _ in _walk(s[table]))
            print(f'  {table}: {leaves} leaf field(s)')
    cl = example_courtlistener()
    print()
    print("cluster.scan_pages :", cl['cluster']['scan_pages'])
    print("cluster.scan_text  :", cl['cluster']['scan_text'])
    print("docket.argument    :", cl['docket']['argument'])
    print("opinion.chunks     :", cl['opinion']['chunks'])

def _walk(t: FieldType):
    if isinstance(t, dict):
        for v in t.values(): yield from _walk(v)
    else: yield t

if __name__ == '__main__':
    _smoke()
