from dataclasses import dataclass

class FrozenDict[K, V](dict):
    """Hashable immutable dict for use as a field in frozen dataclasses."""
    __hash__ = lambda self: hash(frozenset(self.items()))

    @classmethod
    def of(cls, d: dict[K, V]) -> 'FrozenDict[K, V]':
        """Construct from a dict, taking ownership — caller must not reuse d."""
        result: FrozenDict[K, V] = cls.__new__(cls)
        dict.__init__(result, d)
        return result

@dataclass(frozen=True)
class TextType: pass

@dataclass(frozen=True)
class ImageType: pass

@dataclass(frozen=True)
class AudioType: pass

@dataclass(frozen=True)
class TimestampType: pass  # point/interval in a temporal sequence (e.g. audio segment)

@dataclass(frozen=True)
class DateTimeType: pass   # a calendar instant: a filing date, an issue date
# Not the same thing as TimestampType, which is an offset *inside* a piece of media --
# 12.5 seconds into an oral argument. Both are ordered; only one of them has a year.

@dataclass(frozen=True)
class IntType: pass

@dataclass(frozen=True)
class FloatType: pass

@dataclass(frozen=True)
class BoolType: pass

type ModalType   = TextType | ImageType | AudioType
type NumericType = IntType | FloatType
type OrderedType = NumericType | TimestampType | DateTimeType  # <, <=, >, >=, between
type ScalarType  = ModalType | TimestampType | DateTimeType | NumericType | BoolType

@dataclass(frozen=True)
class ArrayType:    # stored, discrete collection (e.g. pages of a document)
    element: 'FieldType'

@dataclass(frozen=True)
class SequenceType: # derived/generated sequence (e.g. audio.timestamps)
    element: 'FieldType'

@dataclass(frozen=True)
class OptionalType:
    inner: 'FieldType'

type CollectionType = ArrayType | SequenceType
type ObjectType     = FrozenDict[str, 'FieldType']
type FieldType      = ScalarType | CollectionType | OptionalType | ObjectType
type Schema         = FrozenDict[str, ObjectType]  # table name -> table schema
