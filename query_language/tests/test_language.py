"""Contract tests for compiler/frontend/optimizer handoff (BQL AST v2)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from query_language import (checks, client, compiler, legal_answer, relevance,
                            schema, serde, serialize)
from query_language.ast import (
    Aggregator,
    AggregatorOp,
    And,
    Between,
    Comparison,
    ComparisonOperator as Op,
    FieldRef,
    Fuzzy,
    InList,
    Join,
    Like,
    Not,
    Or,
    Query,
    TableRef,
    Unnest,
    pp_query,
)
from query_language.typechecker import (
    example_aggregate_typecheck,
    example_schema,
    example_typecheck,
    example_unnest_typecheck,
    typecheck,
)

REG = schema.load("courtlistener")
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def f(source: str, *path: str) -> FieldRef:
    return FieldRef(source, tuple(path))


def t(name: str, alias: str | None = None) -> TableRef:
    return TableRef(name, alias or name)


def eq(left, right) -> Comparison:
    return Comparison(Op.EQ, left, right)


def q(*, select=None, source=None, where=None, group_by=(), limit=10) -> Query:
    return Query(
        select=select or (f("cluster", "id"),),
        source=source or t("cluster"),
        where=where,
        group_by=group_by,
        limit=limit,
    )


def codes(errors) -> list[str]:
    return [error["code"] for error in errors]


class TestCanonicalAst(unittest.TestCase):
    def test_github_typechecker_examples(self):
        self.assertTrue(example_typecheck())
        self.assertTrue(example_unnest_typecheck())
        self.assertTrue(example_aggregate_typecheck())

    def test_not_equal_is_present(self):
        self.assertIn("!=", serde.OPS)

    def test_not_equal_typechecks_for_text(self):
        query = Query(
            (f("cluster", "id"),),
            t("cluster"),
            Comparison(Op.NE, f("cluster", "case_name"), "Unpublished"),
            (),
            10,
        )
        self.assertTrue(typecheck(query, example_schema))

    def test_round_trip_every_node(self):
        source = Join(
            eq(f("c", "docket_id"), f("d", "id")),
            t("cluster", "c"),
            t("docket", "d"),
        )
        query = Query(
            select=(f("d", "court_id"), Aggregator(AggregatorOp.COUNT, None)),
            source=source,
            where=And((
                Comparison(Op.NE, f("c", "precedential_status"), "Unpublished"),
                InList(f("d", "court_id"), ("ca9", "ca10")),
                Between(f("c", "date_filed"), "2020-01-01", "2026-01-01"),
                Like(f("c", "case_name"), "%Graham%"),
                Or((Fuzzy(f("c", "scan_text"), "mentions Graham"),
                    Not(Fuzzy(Unnest(f("c", "scan_pages")), "contains a photo")))),
            )),
            group_by=(f("d", "court_id"),),
            limit=10,
        )
        self.assertEqual(serde.decode(serde.encode(query)), query)

    def test_legacy_serialize_module_delegates_to_v2_contract(self):
        query = q(where=Fuzzy(f("cluster", "scan_pages"), "photo"))
        self.assertEqual(serialize.query_to_dict(query), serde.encode(query))
        self.assertEqual(json.loads(serialize.to_json(query)), serde.encode(query))

    def test_wire_shape_matches_remote_ast(self):
        wire = serde.encode(q(where=Fuzzy(f("cluster", "scan_pages"), "photo")))
        self.assertEqual(wire["source"], {"kind": "TableRef", "name": "cluster", "alias": "cluster"})
        self.assertEqual(wire["select"][0]["path"], ["id"])
        self.assertIsInstance(wire["where"]["field"], dict)
        self.assertEqual(wire["group_by"], [])

    def test_nested_field_path_round_trips(self):
        ref = f("document", "media", "images")
        query = Query((Unnest(ref),), t("document"), None, (), None)
        self.assertEqual(serde.decode(serde.encode(query)), query)

    def test_pretty_printer_handles_new_nodes(self):
        query = Query(
            (f("d", "court_id"), Aggregator(AggregatorOp.COUNT, None)),
            t("docket", "d"), None, (f("d", "court_id"),), None,
        )
        rendered = pp_query(query)
        self.assertIn("docket as d", rendered)
        self.assertIn("count(*)", rendered)
        self.assertIn("group by d.court_id", rendered)


class TestWireValidation(unittest.TestCase):
    def test_query_requires_v2_fields(self):
        errors = serde.decode_errors({"kind": "Query", "select": [], "source": "cluster"})
        self.assertIn("empty_select", codes(errors))
        self.assertIn("missing_key", codes(errors))

    def test_legacy_column_field_is_rejected(self):
        wire = serde.encode(q())
        wire["select"][0] = {"kind": "FieldRef", "source": "cluster", "column": "id"}
        self.assertIn("unexpected_key", codes(serde.decode_errors(wire)))

    def test_legacy_bare_source_is_rejected(self):
        wire = serde.encode(q())
        wire["source"] = "cluster"
        self.assertIn("not_an_object", codes(serde.decode_errors(wire)))

    def test_fuzzy_field_is_not_a_list(self):
        wire = serde.encode(q(where=Fuzzy(f("cluster", "scan_pages"), "photo")))
        wire["where"]["field"] = [wire["where"]["field"]]
        self.assertIn("fuzzy_single_field", codes(serde.decode_errors(wire)))

    def test_bad_path_is_rejected(self):
        wire = serde.encode(q())
        wire["select"][0]["path"] = ["id", 2]
        self.assertIn("bad_field_path", codes(serde.decode_errors(wire)))

    def test_unknown_condition_is_rejected_with_path(self):
        wire = serde.encode(q(where=eq(f("cluster", "id"), 1)))
        wire["where"] = {"kind": "Nope"}
        errors = serde.decode_errors(wire)
        self.assertEqual(errors[0]["path"], "$.where")

    def test_refined_predicate_is_compiler_excluded(self):
        wire = serde.encode(q(where=eq(f("cluster", "id"), 1)))
        wire["where"] = {"kind": "Visual", "field": wire["select"][0], "text": "photo"}
        self.assertIn("excluded_kind", codes(serde.decode_errors(wire)))

    def test_non_count_aggregate_needs_arg(self):
        wire = serde.encode(q(select=(Aggregator(AggregatorOp.MAX, f("cluster", "id")),)))
        wire["select"][0]["arg"] = None
        self.assertIn("missing_aggregate_arg", codes(serde.decode_errors(wire)))


class TestSchemaChecks(unittest.TestCase):
    def test_valid_alias_query(self):
        query = Query((f("c", "id"),), t("cluster", "c"), None, (), 10)
        self.assertEqual(checks.validate(query, REG), [])

    def test_unknown_alias(self):
        self.assertIn("table_not_in_scope", codes(checks.validate(
            Query((f("x", "id"),), t("cluster", "c"), None, (), 10), REG)))

    def test_unknown_field_suggests_real_field(self):
        errors = checks.validate(q(select=(f("cluster", "case_nam"),)), REG)
        self.assertIn("unknown_field", codes(errors))
        self.assertIn("cluster.case_name", str(errors[0]))

    def test_exact_on_modal_is_rejected(self):
        errors = checks.validate(q(where=eq(f("cluster", "scan_pages"), "x")), REG)
        self.assertIn("exact_on_modal_field", codes(errors))

    def test_fuzzy_on_scalar_is_rejected(self):
        errors = checks.validate(q(where=Fuzzy(f("cluster", "id"), "one")), REG)
        self.assertIn("fuzzy_on_scalar", codes(errors))

    def test_unnest_requires_collection(self):
        errors = checks.validate(q(select=(Unnest(f("cluster", "id")),)), REG)
        self.assertIn("unnest_scalar", codes(errors))

    def test_unnest_modal_collection_is_valid(self):
        self.assertEqual(checks.validate(
            q(where=Fuzzy(Unnest(f("cluster", "scan_pages")), "photo")), REG), [])

    def test_join_aliases_follow_edge(self):
        source = Join(eq(f("c", "docket_id"), f("d", "id")), t("cluster", "c"), t("docket", "d"))
        query = Query((f("c", "id"),), source, eq(f("d", "court_id"), "ca9"), (), 10)
        self.assertEqual(checks.validate(query, REG), [])

    def test_join_must_follow_foreign_key(self):
        source = Join(eq(f("c", "id"), f("a", "docket_id")), t("cluster", "c"), t("audio", "a"))
        self.assertIn("unknown_join_edge", codes(checks.validate(
            Query((f("c", "id"),), source, None, (), 10), REG)))

    def test_join_must_use_equality(self):
        source = Join(Comparison(Op.LT, f("c", "docket_id"), f("d", "id")),
                      t("cluster", "c"), t("docket", "d"))
        self.assertIn("bad_join_op", codes(checks.validate(
            Query((f("c", "id"),), source, None, (), 10), REG)))

    def test_group_by_rejects_aggregate(self):
        agg = Aggregator(AggregatorOp.COUNT, None)
        self.assertIn("bad_group_by", codes(checks.validate(q(select=(agg,), group_by=(agg,)), REG)))

    def test_aggregate_select_requires_grouping(self):
        query = q(select=(f("cluster", "case_name"), Aggregator(AggregatorOp.COUNT, None)),
                  group_by=())
        self.assertIn("ungrouped_select", codes(checks.validate(query, REG)))

    def test_field_written_as_string_is_flagged(self):
        query = q(select=("cluster.id",))
        self.assertIn("field_as_string", codes(checks.validate(query, REG)))


class TestExamplesAndPrompt(unittest.TestCase):
    def test_every_few_shot_round_trips_and_validates(self):
        for question, ast in compiler.FEW_SHOTS:
            with self.subTest(question=question[:40]):
                wire = serde.encode(ast)
                self.assertEqual(serde.decode(wire), ast)
                self.assertEqual(checks.validate(ast, REG), [])

    def test_examples_are_v2_and_valid(self):
        paths = sorted(EXAMPLES.glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                payload = json.loads(path.read_text())
                self.assertEqual(payload["bql_version"], serde.BQL_VERSION)
                ast = serde.decode(payload["query"])
                self.assertEqual(checks.validate(ast, REG), [])

    def test_few_shots_cover_new_nodes(self):
        wire = json.dumps([serde.encode(ast) for _, ast in compiler.FEW_SHOTS])
        for kind in ("TableRef", "Unnest", "Aggregator"):
            self.assertIn(f'"kind": "{kind}"', wire)

    def test_prompt_describes_canonical_contract(self):
        prompt = compiler.build_system_prompt(REG)
        for term in ("TableRef", "path", "Unnest", "Aggregator", "group_by"):
            self.assertIn(term, prompt)
        self.assertIn("cluster.scan_pages", prompt)

    def test_prompt_fits_context(self):
        messages = compiler.build_messages("a question", REG)
        self.assertLess(client.estimate_tokens(messages) + client.MAX_TOKENS, client.CONTEXT_TOKENS)

    def test_dataform_schema_uses_nested_paths_and_its_own_examples(self):
        registry = schema.load("dataform")
        self.assertTrue(registry.has("document.media.text.plain_text"))
        self.assertTrue(registry.has("document.media.images"))
        for _, ast in compiler.few_shots_for(registry):
            self.assertEqual(checks.validate(ast, registry), [])
        prompt = compiler.build_system_prompt(registry)
        self.assertIn("document.media.images", prompt)
        self.assertNotIn("cluster.scan_pages", prompt)


class TestCompilerLoop(unittest.TestCase):
    @staticmethod
    def scripted(*replies):
        sent = []

        def fn(messages, **kwargs):
            sent.append((messages, kwargs))
            return client.ChatResponse(text=replies[min(len(sent) - 1, len(replies) - 1)], model="scripted")

        fn.sent = sent
        return fn

    def test_json_extraction_variants(self):
        expected = {"kind": "Query"}
        for value in ('{"kind":"Query"}', '```json\n{"kind":"Query"}\n```',
                      'Sure\n{"kind":"Query"}\nDone',
                      '<think>reason</think>{"kind":"Query"}'):
            with self.subTest(value=value[:10]):
                self.assertEqual(compiler.extract_json(value)[0], expected)

    def test_first_try_success(self):
        good = json.dumps(serde.encode(q(where=eq(f("cluster", "id"), 1))))
        result = compiler.compile_question("x", registry=REG, use_cache=False,
                                           chat_fn=self.scripted(good))
        self.assertTrue(result.ok)
        self.assertEqual(result.ast, serde.decode(result.query))
        self.assertEqual(result.envelope()["bql_version"], "2.0")
        self.assertEqual(result.envelope()["mode"], "compile")
        self.assertIs(result.envelope()["is_legal"], True)
        self.assertEqual(result.envelope()["bql"], result.printed)
        self.assertIn("select", result.envelope()["bql"])

    def test_repairs_legacy_payload(self):
        bad = json.dumps({
            "kind": "Query",
            "select": [{"kind": "FieldRef", "source": "cluster", "column": "id"}],
            "source": "cluster", "where": None, "limit": 10,
        })
        good = json.dumps(serde.encode(q()))
        fn = self.scripted(bad, good)
        result = compiler.compile_question("x", registry=REG, use_cache=False, chat_fn=fn)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.attempts), 2)

    def test_repairs_schema_error(self):
        bad = json.dumps(serde.encode(q(where=eq(f("cluster", "scan_pages"), "x"))))
        good = json.dumps(serde.encode(q(where=Fuzzy(f("cluster", "scan_pages"), "photo"))))
        result = compiler.compile_question("x", registry=REG, use_cache=False,
                                           chat_fn=self.scripted(bad, good))
        self.assertTrue(result.ok)
        self.assertIn("exact_on_modal_field", codes(result.attempts[0]["errors"]))

    def test_repairs_use_temperature(self):
        fn = self.scripted("no json")
        compiler.compile_question("x", registry=REG, use_cache=False, chat_fn=fn, max_attempts=3)
        temperatures = [kwargs["temperature"] for _, kwargs in fn.sent]
        self.assertEqual(temperatures[0], compiler.INITIAL_TEMPERATURE)
        self.assertTrue(all(value == compiler.REPAIR_TEMPERATURE for value in temperatures[1:]))

    def test_gives_structured_failure(self):
        result = compiler.compile_question("x", registry=REG, use_cache=False,
                                           chat_fn=self.scripted("no json"), max_attempts=2)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_report()["stage"], "compile")
        json.dumps(result.error_report())

    def test_aggregation_is_now_expressible_but_ordering_is_not(self):
        self.assertEqual(compiler.cannot_express("How many cases by court?"), [])
        self.assertTrue(compiler.cannot_express("Five most recent cases"))

    def test_direct_legal_answer_bypasses_query_compiler(self):
        compiler_called = False

        def forbidden_chat(messages, **kwargs):
            nonlocal compiler_called
            compiler_called = True
            raise AssertionError("NL-to-JSON compiler must not run")

        result = compiler.compile_question(
            "What is qualified immunity?",
            registry=REG,
            use_cache=False,
            chat_fn=forbidden_chat,
            relevance_fn=lambda question: relevance.RelevanceResult(
                True, "lightning-test", 5, route="answer",
            ),
            answer_fn=lambda question: client.ChatResponse(
                text="A direct legal explanation.", model="super-test",
            ),
        )
        self.assertTrue(result.ok)
        self.assertFalse(compiler_called)
        self.assertIsNone(result.query)
        self.assertEqual(result.answer, "A direct legal explanation.")
        self.assertEqual(result.envelope(), {
            "mode": "answer", "is_legal": True,
            "question": "What is qualified immunity?",
            "answer": "A direct legal explanation.", "model": "super-test",
        })


class TestRelevanceGate(unittest.TestCase):
    def test_lightning_returns_strict_routing_contract(self):
        seen = {}

        def fake_chat(messages, **kwargs):
            seen["messages"] = messages
            seen["kwargs"] = kwargs
            return client.ChatResponse(
                text='{"is_legal": true, "route": "compile"}',
                model="lightning-test", latency_ms=12,
            )

        result = relevance.classify("cases about qualified immunity", chat_fn=fake_chat)
        self.assertTrue(result.is_legal)
        self.assertEqual(result.route, "compile")
        self.assertEqual(result.model, "lightning-test")
        self.assertIn("legal_request_router", seen["messages"][0]["content"])
        self.assertEqual(seen["kwargs"]["model"], client.RELEVANCE_MODEL)
        self.assertEqual(seen["kwargs"]["temperature"], 0.0)
        self.assertEqual(seen["kwargs"]["max_tokens"], relevance.MAX_TOKENS)
        self.assertIs(seen["kwargs"]["enable_thinking"], False)
        self.assertIn('"route": "answer"', seen["messages"][0]["content"])
        self.assertIn('"hi" -> {"is_legal": false', seen["messages"][0]["content"])
        self.assertIn("reliably sort/rank records", seen["messages"][0]["content"])

    def test_invalid_classifier_shape_fails_closed(self):
        with self.assertRaises(client.ModelError):
            relevance._decode('{"related": true}')
        with self.assertRaises(client.ModelError):
            relevance._decode('{"is_legal": "yes"}')
        with self.assertRaises(client.ModelError):
            relevance._decode('{"is_legal": true, "route": "reject"}')
        with self.assertRaises(client.ModelError):
            relevance._decode('{"is_legal": true, "route": "other"}')

    def test_nonlegal_input_never_reaches_compiler(self):
        compiler_called = False

        def forbidden_chat(messages, **kwargs):
            nonlocal compiler_called
            compiler_called = True
            raise AssertionError("Super compiler must not run")

        result = compiler.compile_question(
            "Write me a pancake recipe",
            registry=REG,
            use_cache=False,
            chat_fn=forbidden_chat,
            relevance_fn=lambda question: relevance.RelevanceResult(
                False, "lightning-test", 5,
            ),
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.is_legal)
        self.assertFalse(compiler_called)
        report = result.error_report()
        self.assertEqual(report["stage"], "relevance")
        self.assertIs(report["is_legal"], False)
        self.assertIn("legal research", report["message"])

    def test_mock_gate_handles_legal_and_unrelated_inputs(self):
        self.assertTrue(relevance._mock_is_legal("Find cases about qualified immunity"))
        self.assertFalse(relevance._mock_is_legal("hi"))
        self.assertFalse(relevance._mock_is_legal("Give me a chocolate cake recipe"))
        self.assertEqual(relevance._mock_route("Find cases about immunity"), "compile")
        self.assertEqual(relevance._mock_route("What is qualified immunity?"), "answer")
        self.assertEqual(relevance._mock_route("hi"), "reject")


class TestLegalAnswer(unittest.TestCase):
    def test_super_answer_uses_separate_plain_text_prompt(self):
        seen = {}

        def fake_chat(messages, **kwargs):
            seen["messages"] = messages
            seen["kwargs"] = kwargs
            return client.ChatResponse(text="General legal information.", model="super-test")

        response = legal_answer.answer_question("What is negligence?", chat_fn=fake_chat)
        self.assertEqual(response.text, "General legal information.")
        self.assertEqual(seen["kwargs"]["model"], legal_answer.ANSWER_MODEL)
        self.assertTrue(seen["kwargs"]["enable_thinking"])
        self.assertIn("rather than legal advice", seen["messages"][0]["content"])


class TestClientAndMock(unittest.TestCase):
    def test_mock_reply_is_canonical_and_valid(self):
        obj, error = compiler.extract_json(client._mock_reply([]))
        self.assertIsNone(error)
        self.assertEqual(serde.decode_errors(obj), [])
        self.assertEqual(checks.validate(serde.decode(obj), REG), [])

    def test_mock_reply_tracks_selected_dataform_schema(self):
        registry = schema.load("dataform")
        messages = compiler.build_messages("a question", registry)
        obj, error = compiler.extract_json(client._mock_reply(messages))
        self.assertIsNone(error)
        self.assertEqual(checks.validate(serde.decode(obj), registry), [])

    def test_context_drops_shots_before_system_and_question(self):
        messages = compiler.build_messages("keep me", REG)
        fitted, dropped = client.fit_context(messages, max_tokens=512, budget=2500)
        self.assertGreater(dropped, 0)
        self.assertEqual(fitted[0]["role"], "system")
        self.assertEqual(fitted[-1]["content"], "keep me")

    def test_local_and_remote_routing(self):
        self.assertTrue(client.is_local("nvidia/nemotron-3.5-lightning"))
        self.assertIn(":8001", client.base_url_for("nvidia/nemotron-3.5-lightning"))
        self.assertEqual(client.COMPILER_MODEL, "nvidia/nemotron-3-super-120b-a12b")
        self.assertFalse(client.is_local(client.COMPILER_MODEL))
        self.assertEqual(client.base_url_for(client.COMPILER_MODEL),
                         "https://integrate.api.nvidia.com/v1")
        self.assertFalse(client.is_local("nvidia/some-unserved-model"))
        self.assertEqual(client.base_url_for("nvidia/some-unserved-model"), client.REMOTE_BASE_URL)

    def capture(self, model: str):
        seen = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "model": model,
                    "message": {"content": "{}"},
                    "choices": [{"message": {"content": "{}"}}],
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                    "usage": {},
                }).encode()

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data.decode())
            return FakeResponse()

        original = client.urllib.request.urlopen
        client.urllib.request.urlopen = fake_urlopen
        try:
            client.chat([{"role": "user", "content": "hi"}], model=model)
        finally:
            client.urllib.request.urlopen = original
        return seen

    def test_ollama_dialect(self):
        local_model = "test/nemotron-super-local"
        client.LOCAL_MODELS[local_model] = (11434, client.OLLAMA)
        try:
            seen = self.capture(local_model)
            self.assertTrue(seen["url"].endswith("/api/chat"))
            self.assertIs(seen["body"]["think"], client.ENABLE_THINKING)
            self.assertGreater(seen["body"]["options"]["num_ctx"], 8192)
        finally:
            del client.LOCAL_MODELS[local_model]

    def test_llama_server_dialect(self):
        seen = self.capture("nvidia/nemotron-3.5-lightning")
        self.assertTrue(seen["url"].endswith("/chat/completions"))
        self.assertEqual(seen["body"]["chat_template_kwargs"],
                         {"enable_thinking": client.ENABLE_THINKING})

    def test_hosted_super_streams_final_content_and_discards_reasoning(self):
        seen = {}

        class Delta:
            def __init__(self, *, content=None, reasoning_content=None):
                self.content = content
                self.reasoning_content = reasoning_content

        class Choice:
            def __init__(self, delta):
                self.delta = delta

        class Chunk:
            def __init__(self, delta=None):
                self.choices = [] if delta is None else [Choice(delta)]

        class FakeCompletions:
            def create(self, **kwargs):
                seen["request"] = kwargs
                return [
                    Chunk(),
                    Chunk(Delta(reasoning_content="private chain of thought")),
                    Chunk(Delta(content='{"kind":')),
                    Chunk(Delta(content='"Query"}')),
                ]

        class FakeChat:
            completions = FakeCompletions()

        class FakeOpenAI:
            def __init__(self, **kwargs):
                seen["client"] = kwargs
                self.chat = FakeChat()

        original_loader = client._load_openai
        original_key = client.api_key_for
        client._load_openai = lambda: FakeOpenAI
        client.api_key_for = lambda model: "test-key"
        try:
            response = client.chat([{"role": "user", "content": "hello"}],
                                   model=client.COMPILER_MODEL)
        finally:
            client._load_openai = original_loader
            client.api_key_for = original_key

        self.assertEqual(response.text, '{"kind":"Query"}')
        self.assertTrue(response.thought)
        self.assertNotIn("private chain", response.text)
        self.assertEqual(seen["client"]["base_url"], client.REMOTE_BASE_URL)
        self.assertEqual(seen["request"]["model"], "nvidia/nemotron-3-super-120b-a12b")
        self.assertIs(seen["request"]["stream"], True)
        self.assertEqual(seen["request"]["temperature"], 1)
        self.assertEqual(seen["request"]["top_p"], 0.95)
        self.assertEqual(seen["request"]["max_tokens"], 16384)
        self.assertEqual(seen["request"]["extra_body"], {
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning_budget": 16384,
        })


if __name__ == "__main__":
    unittest.main()
