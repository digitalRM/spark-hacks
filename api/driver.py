"""The Amicus pipeline: one question in, one envelope out.

    question --[route]--> reject | answer | compile
              compile --> [compile] BQL AST --[typecheck]-> schema-resolved query
                      --> [optimize] ExecutionPlan --[execute]-> results + telemetry

This module is where the pipeline logic lives, including the routing decision. Every
other module does one thing and does not know what runs before or after it:
`relevance.classify` routes, `compiler.compile_question` compiles, `optimizer.optimize`
plans, `runtime.executor.execute` runs. `api/cli.py` and `api/server.py` are both thin:
they parse an invocation, call `run()`, and format the result.

Every stage reports on its own, so a half-built pipeline stays legible instead of opaque.
Route, compile, typecheck and optimize work today; `runtime.executor.execute` still
raises NotImplementedError and is reported as `stub` until it does not.

`ok` on the envelope means the question produced an answer -- a compiled query or a
direct legal answer -- and nothing about the later stages. That is the contract the
frontend depends on, and it must not start failing the day the executor lands and errors
on a hard query.

Dates are a type, not a convention: a date column is DATE in the registry and
DateTimeType to the typechecker, compared against a `Date` literal (a bare ISO-8601
string is accepted next to one). So `date_filed >= "2020-01-01"` typechecks, and it
typechecks because both sides are dates rather than because text was made orderable.

Cost: one routing call, then one compiler round trip on a cache miss, or one direct
answer. Pure computation from there until the executor lands.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from time import perf_counter
from typing import Any, Literal

from optimizer import optimizer
from query_language import checks, client, compiler, legal_answer, relevance, schema, serde
from query_language.ast import Query, pp_query
from query_language.bridge import BridgeError, registry_to_schema
from query_language.compiler import CompileResult
from query_language.schema import Registry
from query_language.serde import BQL_VERSION, DecodeError
from query_language.type_system import Schema
from query_language.typechecker import TypeCheckError, typecheck
from runtime import executor

type Status = Literal['ok', 'failed', 'stub', 'skipped']
type Mode = Literal['compile', 'answer', 'reject']

@dataclass(frozen=True)
class Stage:
    """What one stage did. `stub` means the module behind it is not written yet;
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

def skipped(names: tuple[str, ...], why: str) -> list[Stage]:
    return [Stage(name, 'skipped', message=why) for name in names]

def type_error(e: Exception) -> DecodeError:
    """One typecheck failure in the same shape as every wire and schema error."""
    return DecodeError('$.where', 'type_error', str(e))

# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #
def relevance_stage(question: str) -> tuple[Stage, relevance.RelevanceResult]:
    """Route the question: is this a record search, a legal question, or neither?

    Its own stage rather than a hidden step inside compile, so a rejection reads as a
    rejection instead of a compiler failure.

    Fails open. The router is a courtesy filter on input, not a correctness gate on
    output, so a router that is down must not take the pipeline with it.

    Cost: one call to the router model.
    """
    t0 = perf_counter()
    try:
        decision = relevance.classify(question)
    except client.ModelError as e:
        return (Stage('relevance', 'failed', since(t0), f'router unavailable, allowing through: {e}',
                      detail={'route': 'compile', 'failed_open': True}),
                relevance.RelevanceResult('compile', 'unavailable'))
    detail = {'route': decision.route, 'model': decision.model}
    if decision.call is not None: detail['calls'] = [decision.call.telemetry()]
    if decision.route == 'reject':
        return Stage('relevance', 'failed', since(t0), relevance.REJECTION_MESSAGE,
                     detail=detail), decision
    return Stage('relevance', 'ok', since(t0), detail=detail), decision

def answer_stage(question: str) -> tuple[Stage, str, str]:
    """The `answer` route: a legal question BQL cannot express, answered by Super.

    Returns (stage, answer, model). A failure here is the whole response, so it is
    reported as a failed stage with an empty answer rather than raised.
    """
    t0 = perf_counter()
    try:
        response = legal_answer.answer_question(question)
    except client.ModelError as e:
        return Stage('answer', 'failed', since(t0), f'model unavailable: {e}'), '', ''
    return (Stage('answer', 'ok', since(t0),
                  detail={'model': response.model, 'calls': [response.telemetry()]}),
            response.text.strip(), response.model)

def compile_stage(question: str, reg: Registry, *, use_cache: bool,
                  max_attempts: int) -> tuple[Stage, CompileResult]:
    """Natural language to a validated BQL AST, via the compiler's validate-repair loop.

    Carries the loop's telemetry onto the stage -- model, attempt count, cache hit, and
    one entry per round trip. That telemetry is the evidence a compiler ran rather than a
    prompt, and with a repair loop "compile took 30s" is never the interesting number:
    how many round trips, and how much of each was the model thinking, is.
    """
    t0 = perf_counter()
    try:
        result = compiler.compile_question(question, registry=reg, use_cache=use_cache,
                                           max_attempts=max_attempts)
    except client.ModelError as e:
        failed = CompileResult(ok=False, question=question, schema_name=reg.name,
                               errors=[DecodeError('$', 'model_unavailable', str(e))])
        return Stage('compile', 'failed', since(t0), f'model unavailable: {e}',
                     tuple(failed.errors)), failed

    detail = {'model': result.model, 'attempts': len(result.attempts),
              'cached': result.cached,
              'calls': [{k: v for k, v in a.items() if k not in ('raw', 'errors')}
                        for a in result.attempts]}
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
    """BQL AST to an ExecutionPlan plus one snapshot per pass."""
    t0 = perf_counter()
    try:
        plan = optimizer.to_json(optimizer.optimize(ast, structural))
    except Exception as e:
        return Stage('optimize', 'failed', since(t0), f'{type(e).__name__}: {e}'), None
    return Stage('optimize', 'ok', since(t0),
                 detail={'passes': len(plan.get('snapshots', ()))}), plan

def execute_stage(plan: dict[str, Any]) -> tuple[Stage, dict[str, Any] | None]:
    """Run the plan against the corpus. Still a stub -- see runtime/executor.py."""
    t0 = perf_counter()
    try:
        results = executor.execute(plan)
    except NotImplementedError as e:
        return Stage('execute', 'stub', since(t0), str(e)), None
    except Exception as e:
        return Stage('execute', 'failed', since(t0), f'{type(e).__name__}: {e}'), None
    return Stage('execute', 'ok', since(t0), detail={'total': results.get('total')}), results

# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #
def calls(stages: list[Stage]) -> list[dict[str, Any]]:
    """Every model call the pipeline made, in order, whichever stage made it."""
    return [c for s in stages for c in s.detail.get('calls', ())]

def cost(stages: list[Stage]) -> dict[str, Any]:
    """What the whole question cost: wall clock, model time, and where the tokens went.

    `pipeline_ms` minus `model_ms` is everything Amicus itself did -- decoding,
    validating, typechecking, planning. It is normally under a millisecond, and the
    point of reporting it next to the model time is that this stays true.
    """
    made = calls(stages)
    total = lambda field: sum(c.get(field, 0) for c in made)  # noqa: E731
    return {'calls': len(made),
            'pipeline_ms': round(sum(s.ms for s in stages), 1),
            'model_ms': round(total('ms'), 1),
            'waiting_ms': round(total('ttfb_ms'), 1),
            'thinking_ms': round(total('thinking_ms'), 1),
            'writing_ms': round(total('writing_ms'), 1),
            'tokens_in': total('tokens_in'),
            'tokens_out': total('tokens_out'),
            'reasoning_tokens': total('reasoning_tokens'),
            'answer_tokens': total('answer_tokens')}

def envelope(question: str, reg: Registry, mode: Mode, stages: list[Stage], *,
             ok: bool, result: CompileResult | None = None, answer: str | None = None,
             model: str = '', plan: dict[str, Any] | None = None,
             results: dict[str, Any] | None = None) -> dict[str, Any]:
    """The wire form. `mode` is the frontend's discriminant: a compiled search, a direct
    legal answer and a rejection are three different shapes over the same envelope."""
    out: dict[str, Any] = {
        'ok': ok,
        'mode': mode,
        'bql_version': BQL_VERSION,
        'schema': reg.name,
        'is_legal': mode != 'reject',
        'question': question,
        'query': result.query if result else None,
        'bql': result.printed if result else '',
        'model': model or (result.model if result else ''),
        'stages': [s.json() for s in stages],
        'plan': plan,
        'results': results,
    }
    out['cost'] = cost(stages)
    if answer is not None: out['answer'] = answer
    if result and result.warnings: out['warnings'] = list(result.warnings)
    errors = [e for s in stages for e in s.errors]
    if errors: out['errors'] = errors
    if not ok:
        # The last failure is the one that stopped the pipeline; an earlier stage may
        # have failed open and carried on.
        failed = [s for s in stages if s.status == 'failed']
        out['message'] = failed[-1].message if failed else 'the pipeline produced no result'
    return out

def run(question: str, *, schema_name: str | None = None, use_cache: bool = True,
        max_attempts: int = compiler.MAX_ATTEMPTS, execute: bool = True) -> dict[str, Any]:
    """Route, compile, typecheck, optimize and execute one question. Never raises for a
    bad query.

    A stage that cannot run makes the rest `skipped` rather than inventing a result: a
    query that does not typecheck has no meaningful plan, and saying so beats planning
    around it.

    `execute=False` stops after optimize and reports the execute stage as `skipped`, so a
    caller can show the compiled query and plan first and run the plan separately with
    `execute_plan()` -- execution is the slow, model-call-per-record part.
    """
    reg = schema.load(schema_name)
    question = ' '.join(question.split())
    rest = ('compile', 'typecheck', 'optimize', 'execute')

    routed, decision = relevance_stage(question)

    if decision.route == 'reject':
        why = 'the question is not a legal-research request'
        return envelope(question, reg, 'reject', [routed, *skipped(rest, why)],
                        ok=False, model=decision.model)

    if decision.route == 'answer':
        # A legal question the query language cannot express. There is no AST, so
        # nothing downstream applies -- and an answer is a success, not a failure.
        answered, answer, model = answer_stage(question)
        why = 'the router answered this directly; there is no query to plan'
        return envelope(question, reg, 'answer', [routed, answered, *skipped(rest[1:], why)],
                        ok=answered.status == 'ok', answer=answer, model=model)

    compiled, result = compile_stage(question, reg, use_cache=use_cache,
                                     max_attempts=max_attempts)
    if not result.ok or result.ast is None:
        why = 'the query did not compile'
        return envelope(question, reg, 'compile', [routed, compiled, *skipped(rest[1:], why)],
                        ok=False, result=result)

    checked, structural = typecheck_stage(result.ast, reg)
    if structural is None:
        why = 'the query did not typecheck'
        return envelope(question, reg, 'compile',
                        [routed, compiled, checked, *skipped(rest[2:], why)],
                        ok=True, result=result)

    planned, plan = optimize_stage(result.ast, structural)
    if plan is None:
        return envelope(question, reg, 'compile',
                        [routed, compiled, checked, planned,
                         *skipped(rest[3:], 'no plan: the optimize stage failed')],
                        ok=True, result=result)

    if not execute:
        return envelope(question, reg, 'compile',
                        [routed, compiled, checked, planned,
                         *skipped(rest[3:], 'execution deferred by the caller')],
                        ok=True, result=result, plan=plan)

    executed, results = execute_stage(plan)
    return envelope(question, reg, 'compile', [routed, compiled, checked, planned, executed],
                    ok=True, result=result, plan=plan, results=results)

def execute_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Run an already-optimized plan (as returned in an envelope's `plan`) and report the
    execute stage on its own. The pair `run(..., execute=False)` + `execute_plan(plan)` is
    exactly one `run(...)`, split at the point where the slow part starts."""
    executed, results = execute_stage(plan)
    out: dict[str, Any] = {'ok': executed.status == 'ok', 'stage': executed.json(),
                           'results': results}
    if executed.status != 'ok': out['message'] = executed.message
    return out

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
    """python3 -m api.driver -- runs the flagship question against the cache."""
    import json
    out = run('9th Circuit cases with a photo in the record', schema_name='courtlistener')
    for stage in out['stages']:
        print(f"  {stage['name']:<10} {stage['status']:<8} {stage['ms']:>7.1f} ms  "
              f"{stage.get('message', '')}")
    print()
    print(out['bql'] or json.dumps(out.get('errors') or out.get('message'), indent=2))

if __name__ == '__main__':
    _smoke()
