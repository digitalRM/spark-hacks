

@dataclass(frozen=True)
class Sim(Node):
    """SIM(field, text, threshold) — embedding + rerank; cheap. Optimizer-internal."""
    field: FieldRef
    text: str
    threshold: float

    predicate_class = PredicateClass.SIM


@dataclass(frozen=True)
class Sem(Node):
    """SEM(field, text) — text LLM; expensive. Optimizer-internal."""
    field: FieldRef
    text: str

    predicate_class = PredicateClass.SEM


@dataclass(frozen=True)
class Visual(Node):
    """VISUAL(field, text) — VLM; most expensive. Optimizer-internal."""
    field: FieldRef
    text: str

    predicate_class = PredicateClass.VISUAL


@dataclass(frozen=True)
class Audio(Node):
    """AUDIO(field, text) — ASR then text LLM; expensive. Optimizer-internal."""
    field: FieldRef
    text: str

    predicate_class = PredicateClass.AUDIO


SemanticPredicate = Union[Fuzzy, Sim, Sem, Visual, Audio]
Predicate = Union[Exact, Fuzzy, Sim, Sem, Visual, Audio]