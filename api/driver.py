"""The Amicus pipeline: one question in, one envelope out.

    question --[relevance]-> in-domain? --[compile]--> BQL AST
             --[typecheck]-> Schema-resolved query
             --[optimize]--> ExecutionPlan --[execute]--> results + telemetry

This module is where the pipeline logic lives. `api/cli.py` and `api/server.py` are both
thin: they parse an invocation, call `run()`, and format the result.

Every stage reports on its own, so a half-built pipeline stays legible instead of opaque.
Compile and typecheck work today. Optimize and execute are *seams*: the driver imports them
lazily and reports `stub` until the module behind each one exists, so wiring in a real
optimizer means writing `optimizer/optimizer.py` and changing nothing here.

    stage      module                 needs
    relevance  query_language         classify(question) -> RelevanceResult
    compile    query_language         compile_question(question, registry) -> CompileResult
    typecheck  query_language         registry_to_schema(reg) + typecheck(ast, schema)
    optimize   optimizer.optimizer    optimize(ast, schema) -> plan; to_json(plan) -> dict
    execute    runtime.executor       execute(plan) -> dict

`ok` on the envelope means the COMPILE stage succeeded, and nothing else. That is the
contract the frontend depends on, and it must not start failing the day a later stage is
implemented and errors on a hard query.

One known gap, deliberately left visible rather than papered over: the registry types every
non-modal column as the single string SCALAR, so an ISO date arrives as text, while
`check_comparable` allows <, <=, >, >= on numeric and timestamp only. A query carrying
`date_filed >= "2020-01-01"` compiles and then fails typecheck. The one-line fix is to let
ordering comparisons through on TextType -- lexicographic order is correct for ISO-8601 and
is exactly what SQLite does on a TEXT date column -- but that is a semantics decision in
`query_language/typechecker.py`, so it is reported, not taken.

Cost: compile is the only stage that touches the network, and only on a cache miss.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from importlib import import_module
from time import perf_counter
from typing import Any, Literal

from query_language import checks, client, compiler, relevance, schema, serde
from query_language.ast import Query, pp_query
from query_language.bridge import BridgeError, registry_to_schema
from query_language.compiler import CompileResult
from query_language.schema import Registry
from query_language.serde import BQL_VERSION, DecodeError
from query_language.type_system import Schema
from query_language.typechecker import TypeCheckError, typecheck

# The two modules the driver calls but does not own. Named here so a grep for either seam
# lands on the contract in the module docstring.
OPTIMIZER_SEAM = 'optimizer.optimizer'
EXECUTOR_SEAM = 'runtime.executor'

type Status = Literal['ok', 'failed', 'stub', 'skipped']

@dataclass(frozen=True)
class Stage:
    """What one stage did. `stub` means the module behind the seam is not written yet;
    `skipped` means an earlier stage made this one meaningless."""
    name: str
    status: Status
    ms: float = 0.0
    message: str = ''
    errors: tuple[Any, ...] = ()
    detail: dict[str, Any] = dc_field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        out: dict[str, Any] = {'name': self.name, 'status': self.status, 'ms': round(self.ms, 1)}
        if self.message: out['message'] = self.message
        if self.errors: out['errors'] = list(self.errors)
        if self.detail: out['detail'] = self.detail
        return out

def since(t0: float) -> float:
    return (perf_counter() - t0) * 1000

def skipped(name: str, why: str) -> Stage:
    return Stage(name, 'skipped', message=why)

def type_error(e: Exception) -> DecodeError:
    """One typecheck failure in the same shape as every wire and schema error."""
    return DecodeError('$.where', 'type_error', str(e))

def seam(module_name: str, *names: str) -> tuple[Any, ...] | None:
    """Import a stage module that may not exist yet. None when it, or any name in it, is missing."""
    try: module = import_module(module_name)
    except ModuleNotFoundError: return None
    found = tuple(getattr(module, n, None) for n in names)
    return found if all(f is not None for f in found) else None

def seam_status(module_name: str, *names: str) -> Status:
    """Whether a seam is really implemented, for reporting rather than dispatch.

    A stub module exists and exports the right names -- presence proves nothing. A module
    that is not written yet declares `STUB = True` so this does not have to call it to find
    out. Dispatch still catches NotImplementedError, because a module can lie.
    """
    try: module = import_module(module_name)
    except ModuleNotFoundError: return 'stub'
    if getattr(module, 'STUB', False): return 'stub'
    return 'ok' if all(getattr(module, n, None) is not None for n in names) else 'stub'

# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #
def relevance_stage(question: str) -> tuple[Stage, relevance.RelevanceResult]:
    """Local Lightning gate: is this a legal-research question at all?

    Its own stage rather than a hidden step inside compile, so a domain rejection reads as
    a rejection instead of a compiler failure. The decision is handed to `compile_question`
    afterwards so the gate is never consulted twice.

    Fails open. The gate is a courtesy filter on input, not a correctness gate on output,
    so a Lightning server that is down must not take the pipeline with it.

    Cost: one small local call, ~32 tokens.
    """
    t0 = perf_counter()
    try:
        decision = relevance.classify(question)
    except client.ModelError as e:
        return (Stage('relevance', 'failed', since(t0), f'gate unavailable, allowing through: {e}',
                      detail={'is_legal': True, 'failed_open': True}),
                relevance.RelevanceResult(True, 'unavailable'))
    detail = {'is_legal': decision.is_legal, 'route': decision.route, 'model': decision.model}
    if decision.route == 'reject':
        return Stage('relevance', 'failed', since(t0), relevance.REJECTION_MESSAGE,
                     detail=detail), decision
    return Stage('relevance', 'ok', since(t0), detail=detail), decision

def compile_stage(question: str, reg: Registry, decision: relevance.RelevanceResult, *,
                  use_cache: bool, max_attempts: int) -> tuple[Stage, CompileResult]:
    """Natural language to a validated BQL AST, via the compiler's validate-repair loop.

    Carries the loop's telemetry onto the stage -- model, attempt count, cache hit -- which
    `CompileResult.envelope()` drops. That telemetry is the evidence a compiler ran rather
    than a prompt, and it should be visible in the UI.
    """
    t0 = perf_counter()
    try:
        result = compiler.compile_question(question, registry=reg, use_cache=use_cache,
                                           relevance_fn=lambda _: decision,
                                           max_attempts=max_attempts)
    except client.ModelError as e:
        failed = CompileResult(ok=False, question=question, schema_name=reg.name,
                               errors=[DecodeError('$', 'model_unavailable', str(e))])
        return Stage('compile', 'failed', since(t0), f'model unavailable: {e}',
                     tuple(failed.errors)), failed

    detail = {'model': result.model, 'attempts': len(result.attempts), 'cached': result.cached}
    if result.warnings: detail['warnings'] = list(result.warnings)
    if not result.ok:
        return Stage('compile', 'failed', since(t0), result.message(),
                     tuple(result.errors), detail), result
    return Stage('compile', 'ok', since(t0), detail=detail), result

def typecheck_stage(ast: Query, reg: Registry) -> tuple[Stage, Schema | None]:
    """Resolve every expression in the query against the structural schema.

    Returns the Schema rather than the Env, because that is what the optimizer needs for
    P2 type refinement -- Fuzzy on ImageType is a Visual, on AudioType an Audio. The Env is
    recomputed there rather than threaded through a stage boundary.
    """
    t0 = perf_counter()
    try:
        structural = registry_to_schema(reg)
    except BridgeError as e:
        return Stage('typecheck', 'failed', since(t0), f'schema is not convertible: {e}',
                     (type_error(e),)), None
    try:
        env = typecheck(ast, structural)
    except TypeCheckError as e:
        return Stage('typecheck', 'failed', since(t0), str(e), (type_error(e),)), None
    return Stage('typecheck', 'ok', since(t0), detail={'sources': sorted(env)}), structural

def optimize_stage(ast: Query, structural: Schema) -> tuple[Stage, dict[str, Any] | None]:
    """BQL AST to an ExecutionPlan plus one snapshot per pass. Seam: not built yet."""
    found = seam(OPTIMIZER_SEAM, 'optimize', 'to_json')
    if found is None:
        return Stage('optimize', 'stub',
                     message=f'{OPTIMIZER_SEAM}.optimize(ast, schema) is not implemented'), None
    optimize, to_json = found
    t0 = perf_counter()
    try:
        plan = to_json(optimize(ast, structural))
    except NotImplementedError as e:
        return Stage('optimize', 'stub', since(t0), str(e)), None
    except Exception as e:
        return Stage('optimize', 'failed', since(t0), f'{type(e).__name__}: {e}'), None
    return Stage('optimize', 'ok', since(t0),
                 detail={'passes': len(plan.get('snapshots', ()))}), plan

def execute_stage(plan: dict[str, Any]) -> tuple[Stage, dict[str, Any] | None]:
    """Run the plan against the corpus. Seam: not built yet."""
    found = seam(EXECUTOR_SEAM, 'execute')
    if found is None:
        return Stage('execute', 'stub',
                     message=f'{EXECUTOR_SEAM}.execute(plan) is not implemented'), None
    (execute,) = found
    t0 = perf_counter()
    try:
        results = execute(plan)
    except NotImplementedError as e:
        return Stage('execute', 'stub', since(t0), str(e)), None
    except Exception as e:
        return Stage('execute', 'failed', since(t0), f'{type(e).__name__}: {e}'), None
    return Stage('execute', 'ok', since(t0), detail={'total': results.get('total')}), results

# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #
def envelope(question: str, reg: Registry, result: CompileResult, stages: list[Stage],
             plan: dict[str, Any] | None, results: dict[str, Any] | None) -> dict[str, Any]:
    """The wire form.

    A superset of `CompileResult.envelope()` -- same `mode`, `query`, `bql`, `schema`,
    `bql_version`, `is_legal`, `answer`, `warnings` at the same keys -- so every existing
    consumer keeps working and the stage reporting is additive. `mode` is the frontend's
    discriminant: a compiled search and a direct legal answer are different shapes.
    """
    out: dict[str, Any] = {
        'ok': result.ok,
        'mode': ('answer' if result.answer is not None else
                 'compile' if result.is_legal else 'reject'),
        'bql_version': BQL_VERSION,
        'schema': reg.name,
        'is_legal': result.is_legal,
        'question': question,
        'query': result.query,
        'bql': result.printed,
        'model': result.model,
        'stages': [s.json() for s in stages],
        'plan': plan,
        'results': results,
    }
    if result.answer is not None: out['answer'] = result.answer
    if result.warnings: out['warnings'] = list(result.warnings)
    errors = [e for s in stages for e in s.errors]
    if errors: out['errors'] = errors
    if not result.ok: out['message'] = result.message()
    return out

def run(question: str, *, schema_name: str | None = None, use_cache: bool = True,
        max_attempts: int = compiler.MAX_ATTEMPTS) -> dict[str, Any]:
    """Gate, compile, typecheck, optimize and execute one question. Never raises for a bad query.

    A stage that cannot run makes the rest `skipped` rather than inventing a result: a query
    that does not typecheck has no meaningful plan, and saying so beats planning around it.

    Cost: one small local gate call, plus one hosted round trip on a compile cache miss;
    pure computation from there until the executor seam is filled.
    """
    reg = schema.load(schema_name)
    question = ' '.join(question.split())

    gate, decision = relevance_stage(question)
    if not decision.is_legal:
        rejected = CompileResult(ok=False, question=question, schema_name=reg.name,
                                 is_legal=False, model=decision.model,
                                 relevance_model=decision.model)
        why = 'the question is not a legal-research request'
        return envelope(question, reg, rejected,
                        [gate, skipped('compile', why), skipped('typecheck', why),
                         skipped('optimize', why), skipped('execute', why)], None, None)

    compiled, result = compile_stage(question, reg, decision, use_cache=use_cache,
                                     max_attempts=max_attempts)
    if result.answer is not None:
        # The router sent this to Super instead: a legal question the query language cannot
        # express. There is no AST, so nothing downstream applies -- and it is a success,
        # not a compile failure.
        why = 'the router answered this directly; there is no query to plan'
        return envelope(question, reg, result,
                        [gate, compiled, skipped('typecheck', why), skipped('optimize', why),
                         skipped('execute', why)], None, None)

    if not result.ok or result.ast is None:
        why = 'the query did not compile'
        return envelope(question, reg, result,
                        [gate, compiled, skipped('typecheck', why), skipped('optimize', why),
                         skipped('execute', why)], None, None)

    checked, structural = typecheck_stage(result.ast, reg)
    if structural is None:
        why = 'the query did not typecheck'
        return envelope(question, reg, result,
                        [gate, compiled, checked, skipped('optimize', why),
                         skipped('execute', why)], None, None)

    planned, plan = optimize_stage(result.ast, structural)
    if plan is None:
        return envelope(question, reg, result,
                        [gate, compiled, checked, planned,
                         skipped('execute', f'no plan: the optimize stage is {planned.status}')],
                        None, None)

    executed, results = execute_stage(plan)
    return envelope(question, reg, result,
                    [gate, compiled, checked, planned, executed], plan, results)

def check(wire: Any, reg: Registry) -> tuple[Query | None, list[DecodeError]]:
    """Decode, schema-check and typecheck a hand-written query. Returns (query, errors).

    The same three gates `run()` puts a compiled query through, minus the model. Cost: free.
    """
    errors = serde.decode_errors(wire)
    if errors: return None, errors
    query = serde.decode(wire)
    errors = checks.validate(query, reg)
    if errors: return query, errors
    try:
        typecheck(query, registry_to_schema(reg))
    except (BridgeError, TypeCheckError) as e:
        return query, [type_error(e)]
    return query, []

def printed(query: Query) -> str:
    """The query in readable BQL. Display only -- the JSON is the contract."""
    return pp_query(query).strip()

def _smoke() -> None:
    """python3 -m api.driver -- runs the flagship question offline against the cache."""
    import json
    out = run('9th Circuit cases with a photo in the record', schema_name='courtlistener')
    for stage in out['stages']:
        print(f"  {stage['name']:<10} {stage['status']:<8} {stage['ms']:>7.1f} ms  "
              f"{stage.get('message', '')}")
    print()
    print(out['bql'] or json.dumps(out.get('errors'), indent=2))

if __name__ == '__main__':
    _smoke()
