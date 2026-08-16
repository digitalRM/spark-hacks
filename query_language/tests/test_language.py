"""Contract tests for compiler/frontend/optimizer handoff (BQL AST v2)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import config

from api import driver
from query_language import (checks, client, compiler, legal_answer, relevance,
                            schema, serde)
from query_language.bridge import registry_to_schema
from query_language.ast import (
    Aggregator,
    AggregatorOp,
    And,
    Between,
    Comparison,
    ComparisonOperator as Op,
    Date,
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
from query_language.bridge import registry_to_schema
from query_language.type_system import DateTimeType
from query_language.typechecker import (
    TypeCheckError,
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

    def test_typechecker_accepts_ordinary_scalar_predicates(self):
        """Ordinary predicates must not be type errors: ordering and min/max on dates,
        numeric literals against text columns, and membership on set-valued scalars.

        Scalars compare across kinds because the registry cannot say whether an id column
        is text or integer. Dates are the exception -- see the rejected list below."""
        from query_language.bridge import registry_to_schema
        from query_language.schema import load
        S = registry_to_schema(load("dataform"))
        doc, per, proc = t("document", "doc"), t("person", "p"), t("proceeding", "proc")
        ok = [
            Query((f("doc", "title"),), doc, Comparison(Op.GE, f("doc", "date_issued"), "2008-01-01"), (), 10),
            Query((f("doc", "title"),), doc,
                  Comparison(Op.GE, f("doc", "date_issued"), Date("2008-01-01")), (), 10),
            Query((f("doc", "title"),), doc, Comparison(Op.EQ, f("doc", "envelope", "id"), 123), (), 10),
            Query((f("doc", "title"),), doc, Between(f("doc", "date_issued"), "2008-01-01", "2010-12-31"), (), 10),
            Query((f("p", "name_last"),), per, Comparison(Op.EQ, f("p", "role_types"), "judge"), (), 10),
            Query((f("p", "name_last"),), per, InList(f("p", "role_types"), ("judge", "attorney")), (), 10),
            Query((f("p", "name_last"),), per, Like(f("p", "role_types"), "%judge%"), (), 10),
            Query((f("proc", "organization_id"), Aggregator(AggregatorOp.MAX, f("proc", "date_filed"))),
                  proc, None, (f("proc", "organization_id"),), None),
        ]
        for query in ok:
            with self.subTest(where=query.where or query.select[-1]):
                self.assertTrue(typecheck(query, S))
        # Still rejected: ordering on a set, comparing modal content, summing text, and a
        # year against a date. `date_issued >= 2008` is not a loose way of writing
        # 2008-01-01 -- SQLite sorts every number before every string, so that predicate is
        # silently true for every row. A date is the one scalar that does not take affinity.
        for bad in [
            Query((f("p", "name_last"),), per, Comparison(Op.GT, f("p", "role_types"), "judge"), (), 10),
            Query((f("doc", "title"),), doc, Comparison(Op.EQ, f("doc", "media", "images"), "x"), (), 10),
            Query((Aggregator(AggregatorOp.SUM, f("doc", "title")),), doc, None, (), None),
            Query((f("doc", "title"),), doc, Comparison(Op.GE, f("doc", "date_issued"), 2008), (), 10),
        ]:
            with self.subTest(bad=bad.where or bad.select[0]):
                with self.assertRaises(TypeCheckError):
                    typecheck(bad, S)

    def test_dataform_iso_date_range_typechecks(self):
        structural = registry_to_schema(schema.load("dataform"))
        query = Query(
            (f("ev", "envelope", "id"),), t("event", "ev"),
            Between(f("ev", "date"), "2008-01-01", "2020-12-31"), (), 10,
        )
        self.assertTrue(typecheck(query, structural))

    def test_dataform_open_ended_iso_date_comparison_typechecks(self):
        structural = registry_to_schema(schema.load("dataform"))
        query = Query(
            (f("doc", "envelope", "id"),), t("document", "doc"),
            Comparison(Op.GE, f("doc", "date_issued"), "2022-01-01"), (), 10,
        )
        self.assertTrue(typecheck(query, structural))

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


class TestDates(unittest.TestCase):
    """A date is a type, not a formatting convention. `date_filed >= "2020-01-01"` used
    to compile and then fail typecheck, because a quoted string is text and text is not
    ordered."""

    STRUCT = registry_to_schema(REG)

    def where(self, condition) -> Query:
        return Query((f("cluster", "id"),), t("cluster"), condition, (), 10)

    def test_date_columns_carry_a_date_type(self):
        self.assertEqual(self.STRUCT["cluster"]["date_filed"], DateTimeType())
        self.assertEqual(REG.get("cluster.date_filed").type, "DATE")
        self.assertNotEqual(self.STRUCT["cluster"]["case_name"], DateTimeType())

    def test_date_literal_round_trips(self):
        query = self.where(Comparison(Op.GE, f("cluster", "date_filed"), Date("2020-01-01")))
        wire = serde.encode(query)
        self.assertEqual(wire["where"]["field2"], {"kind": "Date", "value": "2020-01-01"})
        self.assertEqual(serde.decode(wire), query)

    def test_between_dates_round_trips(self):
        query = self.where(Between(f("cluster", "date_filed"),
                                   Date("2023-01-01"), Date("2023-12-31")))
        self.assertEqual(serde.decode(serde.encode(query)), query)

    def test_ordering_comparisons_typecheck_on_a_date(self):
        for literal in (Date("2020-01-01"), "2020-01-01"):
            with self.subTest(literal=literal):
                query = self.where(Comparison(Op.GE, f("cluster", "date_filed"), literal))
                self.assertEqual(checks.validate(query, REG), [])
                self.assertTrue(typecheck(query, self.STRUCT))

    def test_ordering_is_still_refused_on_text(self):
        query = self.where(Comparison(Op.GE, f("cluster", "case_name"), "Graham"))
        with self.assertRaises(TypeCheckError):
            typecheck(query, self.STRUCT)

    def test_a_bare_string_is_only_read_as_a_date_next_to_a_date(self):
        """The coercion is narrow on purpose: it never retypes a text column."""
        query = self.where(Comparison(Op.EQ, f("cluster", "case_name"), "2020-01-01"))
        self.assertTrue(typecheck(query, self.STRUCT))     # text = text, unchanged
        mismatch = self.where(Comparison(Op.GE, f("cluster", "date_filed"), "last tuesday"))
        with self.assertRaises(TypeCheckError):
            typecheck(mismatch, self.STRUCT)

    def test_the_calendar_decides_what_is_a_date(self):
        wire = serde.encode(self.where(
            Comparison(Op.GE, f("cluster", "date_filed"), Date("2020-01-01"))))
        wire["where"]["field2"]["value"] = "2020-13-45"    # right shape, not a day
        self.assertIn("bad_date", codes(serde.decode_errors(wire)))

    def test_a_date_mistake_is_reported_to_the_repair_loop(self):
        """checks.validate is the list the compiler hands back to the model, so a date
        error has to appear there rather than only in the later typecheck."""
        bad = self.where(Comparison(Op.GE, f("cluster", "date_filed"), "last tuesday"))
        self.assertIn("bad_date_literal", codes(checks.validate(bad, REG)))
        wrong_column = self.where(Comparison(Op.EQ, f("cluster", "case_name"),
                                             Date("2020-01-01")))
        self.assertIn("date_on_non_date_field", codes(checks.validate(wrong_column, REG)))
        in_list = self.where(InList(f("cluster", "date_filed"), ("whenever",)))
        self.assertIn("bad_date_literal", codes(checks.validate(in_list, REG)))

    def test_the_prompt_and_few_shots_teach_the_date_node(self):
        prompt = compiler.build_system_prompt(REG)
        self.assertIn('{"kind":"Date"', prompt)
        self.assertIn("DATE", prompt)
        dated = [ast for _, ast in compiler.FEW_SHOTS
                 if any(isinstance(n, Date) for n in _literals(ast))]
        self.assertTrue(dated, "no few-shot demonstrates a Date literal")
        for _, ast in compiler.FEW_SHOTS:
            self.assertEqual(checks.validate(ast, REG), [])

    def test_the_pretty_printer_marks_a_date(self):
        printed = pp_query(self.where(
            Comparison(Op.GE, f("cluster", "date_filed"), Date("2020-01-01"))))
        self.assertIn('date "2020-01-01"', printed)


def _literals(node):
    """Every literal reachable from a condition tree, for the few-shot check."""
    for condition in serde.walk_condition(node.where):
        match condition:
            case Comparison(_, left, right): yield from (left, right)
            case Between(_, low, high): yield from (low, high)
            case InList(_, values): yield from values


class TestExactFilterExecution(unittest.TestCase):
    """The plan's exact predicates have to actually filter.

    ExactFilter carries two renderings: `predicate` is printed BQL for the node card,
    `sql`/`params` is the parameterised form. The executor read the first one, with a
    regex written for a third format (`EXACT(...)`, which is the node *label*), so every
    exact predicate silently passed every row -- dates, court ids, everything -- and only
    the `degraded` counter said so.
    """

    def setUp(self):
        from optimizer.optimizer import optimize, to_json
        from query_language.typechecker import typecheck
        self.reg = schema.load("dataform")
        self.struct = registry_to_schema(self.reg)
        self.optimize, self.to_json, self.typecheck = optimize, to_json, typecheck

    def plan(self, where):
        query = Query((f("doc", "title"),), t("document", "doc"), where, (), 10)
        self.typecheck(query, self.struct)
        return self.to_json(self.optimize(query, self.struct))

    @staticmethod
    def rows():
        from data_ingestion.dataform.models import Document, RecordEnvelope
        from runtime.executor import Row

        def doc(title, date, kind="opinion"):
            return Row(doc=Document(
                envelope=RecordEnvelope(id=title, source_system="courtlistener",
                                        source_id=title, source_record_id=title),
                title=title, doc_type=kind, date_issued=date))

        return [doc("old-2019", "2019-06-01"), doc("new-2021", "2021-03-04"),
                doc("edge-2020", "2020-01-01"), doc("older-1999", "1999-12-31")]

    def kept(self, where):
        from runtime.executor import execute
        out = execute(self.plan(where), rows=self.rows())
        degraded = sum(stage.get("degraded", 0) for stage in out["funnel"]["stages"])
        self.assertEqual(degraded, 0, "the executor could not evaluate the predicate")
        return sorted(r["title"] for r in out["results"])

    def test_a_date_predicate_filters(self):
        for literal in (Date("2020-01-01"), "2020-01-01"):
            with self.subTest(literal=literal):
                self.assertEqual(
                    self.kept(Comparison(Op.GE, f("doc", "date_issued"), literal)),
                    ["edge-2020", "new-2021"])   # inclusive at the boundary

    def test_between_dates_filters(self):
        self.assertEqual(
            self.kept(Between(f("doc", "date_issued"), Date("2020-01-01"), Date("2020-12-31"))),
            ["edge-2020"])

    def test_the_other_exact_shapes_filter_too(self):
        self.assertEqual(self.kept(Like(f("doc", "title"), "%2021%")), ["new-2021"])
        self.assertEqual(self.kept(InList(f("doc", "doc_type"), ("opinion",))),
                         ["edge-2020", "new-2021", "old-2019", "older-1999"])
        self.assertEqual(
            self.kept(And((Comparison(Op.GE, f("doc", "date_issued"), Date("2020-01-01")),
                           Comparison(Op.EQ, f("doc", "doc_type"), "opinion")))),
            ["edge-2020", "new-2021"])


class TestWireValidation(unittest.TestCase):
    def test_query_requires_v2_fields(self):
        errors = serde.decode_errors({"kind": "Query", "select": [], "source": "cluster"})
        self.assertIn("empty_select", codes(errors))
        self.assertIn("missing_key", codes(errors))

    def test_legacy_column_field_is_rejected(self):
        wire = serde.encode(q())
        wire["select"][0] = {"kind": "FieldRef", "source": "cluster", "column": "id"}
        # The legacy key itself is ignored; what fails is the missing v2 `path`.
        self.assertIn("bad_field_path", codes(serde.decode_errors(wire)))

    def test_unknown_keys_are_ignored(self):
        wire = serde.encode(q())
        wire["order_by"] = []
        wire["select"][0]["note"] = "extra"
        self.assertEqual(serde.decode_errors(wire), [])

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

    def test_nested_join_cannot_reference_an_outer_alias(self):
        registry = schema.load("dataform")
        inner = Join(
            eq(f("doc", "proceeding_id"), f("proc", "envelope", "id")),
            t("document", "doc"), t("organization", "court"),
        )
        source = Join(
            eq(f("proc", "organization_id"), f("court", "envelope", "id")),
            t("proceeding", "proc"), inner,
        )
        errors = checks.validate(
            Query((f("proc", "envelope", "id"),), source, None, (), 10), registry,
        )
        self.assertIn("bad_join_scope", codes(errors))

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
        fitted, _ = client.fit_context(messages, client.MAX_TOKENS)
        self.assertLess(client.estimate_tokens(fitted) + client.MAX_TOKENS,
                        client.CONTEXT_TOKENS)

    def test_dataform_schema_uses_nested_paths_and_its_own_examples(self):
        registry = schema.load("dataform")
        self.assertTrue(registry.has("document.media.text.plain_text"))
        self.assertTrue(registry.has("document.media.images"))
        for _, ast in compiler.few_shots_for(registry):
            self.assertEqual(checks.validate(ast, registry), [])
        prompt = compiler.build_system_prompt(registry)
        self.assertIn("document.media.images", prompt)
        self.assertNotIn("cluster.scan_pages", prompt)

    def test_terse_query_selects_only_relevant_few_shots(self):
        registry = schema.load("dataform")
        selected = compiler.select_few_shots("6th circuit 2019 abortion", registry)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0][0], "Fifth Circuit 2021 voting rights cases.")
        messages = compiler.build_messages("6th circuit 2019 abortion", registry)
        self.assertEqual(len(messages), 4)  # system + one relevant pair + live question

    def test_disposition_query_selects_matching_shape_first(self):
        registry = schema.load("dataform")
        selected = compiler.select_few_shots(
            "Second Circuit securities-fraud opinions since 2010 that reversed a motion to dismiss.",
            registry,
        )
        self.assertIn("affirmed summary judgment", selected[0][0])


def scripted(*replies):
    """A stand-in for `client.chat`: one reply per attempt, recording what was sent.

    Patching that one function is the whole injection story. Production code takes no
    chat callbacks, because a test seam is not a feature.
    """
    sent = []

    def fn(messages, **kwargs):
        sent.append((messages, kwargs))
        return client.ChatResponse(
            text=replies[min(len(sent) - 1, len(replies) - 1)], model="scripted")

    fn.sent = sent
    return fn


class TestCompilerLoop(unittest.TestCase):
    def compile(self, *replies, **kwargs):
        """Compile "x" against scripted model replies. Returns (result, the fake chat)."""
        fn = scripted(*replies)
        with mock.patch.object(client, "chat", fn):
            result = compiler.compile_question("x", registry=REG, use_cache=False, **kwargs)
        return result, fn

    def test_json_extraction_variants(self):
        expected = {"kind": "Query"}
        for value in ('{"kind":"Query"}', '```json\n{"kind":"Query"}\n```',
                      'Sure\n{"kind":"Query"}\nDone',
                      '<think>reason</think>{"kind":"Query"}'):
            with self.subTest(value=value[:10]):
                self.assertEqual(compiler.extract_json(value)[0], expected)

    def test_first_try_success(self):
        good = json.dumps(serde.encode(q(where=eq(f("cluster", "id"), 1))))
        result, fn = self.compile(good)
        self.assertTrue(result.ok)
        self.assertEqual(result.ast, serde.decode(result.query))
        self.assertIn("select", result.printed)
        self.assertEqual(len(fn.sent), 1)
        self.assertEqual(fn.sent[0][1]["model"], config.MODEL)

    def test_compiler_never_routes(self):
        """Routing belongs to relevance.py and the driver; compiling one question is
        exactly one model call."""
        good = json.dumps(serde.encode(q()))
        with mock.patch.object(relevance, "classify") as classify:
            result, fn = self.compile(good)
        self.assertTrue(result.ok)
        classify.assert_not_called()
        self.assertEqual(len(fn.sent), 1)

    def test_repairs_legacy_payload(self):
        bad = json.dumps({
            "kind": "Query",
            "select": [{"kind": "FieldRef", "source": "cluster", "column": "id"}],
            "source": "cluster", "where": None, "limit": 10,
        })
        result, _ = self.compile(bad, json.dumps(serde.encode(q())))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.attempts), 2)

    def test_repairs_schema_error(self):
        bad = json.dumps(serde.encode(q(where=eq(f("cluster", "scan_pages"), "x"))))
        good = json.dumps(serde.encode(q(where=Fuzzy(f("cluster", "scan_pages"), "photo"))))
        result, _ = self.compile(bad, good)
        self.assertTrue(result.ok)
        self.assertIn("exact_on_modal_field", codes(result.attempts[0]["errors"]))

    def test_unambiguous_invented_alias_is_reconciled_without_a_retry(self):
        registry = schema.load("dataform")
        wire = serde.encode(Query(
            (f("doc", "envelope", "id"),),
            t("document", "doc"),
            eq(f("org", "jurisdiction"), "ca2"),
            (), 10,
        ))
        ast, errors = compiler.check_payload(wire, registry)
        self.assertEqual(errors, [])
        self.assertIsNotNone(ast)
        self.assertEqual(ast.where.field1.source, "doc")

    def test_table_name_is_reconciled_to_its_declared_alias(self):
        registry = schema.load("dataform")
        wire = serde.encode(Query(
            (f("doc", "envelope", "id"),),
            Join(
                eq(f("doc", "issuing_body_id"), f("organization", "envelope", "id")),
                t("document", "doc"), t("organization", "court"),
            ),
            eq(f("court", "jurisdiction"), "ca6"), (), 10,
        ))
        ast, errors = compiler.check_payload(wire, registry)
        self.assertEqual(errors, [])
        self.assertIsNotNone(ast)
        self.assertIn("court.envelope.id", pp_query(ast))

    def test_ambiguous_invented_alias_still_fails_validation(self):
        registry = schema.load("dataform")
        wire = serde.encode(Query(
            (f("doc", "envelope", "id"),),
            Join(
                eq(f("doc", "issuing_body_id"), f("org", "envelope", "id")),
                t("document", "doc"), t("organization", "org"),
            ),
            eq(f("invented", "jurisdiction"), "ca2"),
            (), 10,
        ))
        _, errors = compiler.check_payload(wire, registry)
        self.assertIn("table_not_in_scope", codes(errors))

    def test_compile_and_repair_calls_keep_full_thinking_and_are_labeled(self):
        good = json.dumps(serde.encode(q()))
        result, fn = self.compile("no json", good)
        self.assertTrue(result.ok)
        self.assertEqual(fn.sent[0][1]["max_tokens"], compiler.MAX_TOKENS)
        self.assertEqual(fn.sent[1][1]["max_tokens"], compiler.MAX_TOKENS)
        self.assertTrue(fn.sent[0][1]["enable_thinking"])
        self.assertTrue(fn.sent[1][1]["enable_thinking"])
        self.assertEqual(fn.sent[0][1]["purpose"], "compile")
        self.assertEqual(fn.sent[1][1]["purpose"], "repair 1")

    def test_repair_turn_carries_the_error_paths(self):
        _, fn = self.compile("no json", max_attempts=2)
        first, second = (messages for messages, _ in fn.sent)
        self.assertEqual(len(second), len(first) + 2)
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertIn("invalid_json", second[-1]["content"])

    def test_gives_structured_failure(self):
        result, _ = self.compile("no json", max_attempts=2)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.attempts), 2)
        self.assertIn("could not compile after 2 attempts", result.message())
        json.dumps(result.errors)

    def test_aggregation_is_now_expressible_but_ordering_is_not(self):
        self.assertEqual(compiler.cannot_express("How many cases by court?"), [])
        self.assertTrue(compiler.cannot_express("Five most recent cases"))

    def test_semantic_coverage_rejects_copied_few_shot_constraints(self):
        question = ("Find 6th Circuit cases that went up to the Supreme Court from "
                    "2008 onwards but not after 2020")
        copied = serde.encode(compiler.DATAFORM_FEW_SHOTS[1][1])
        errors = compiler.semantic_coverage_errors(question, copied)
        self.assertIn("missing_question_constraint", codes(errors))
        self.assertIn("copied_question_constraint", codes(errors))

    def test_semantic_coverage_repairs_a_wrong_but_valid_query(self):
        question = ("Find 6th Circuit cases that went up to the Supreme Court from "
                    "2008 onwards but not after 2020")
        bad = json.dumps(serde.encode(compiler.DATAFORM_FEW_SHOTS[1][1]))
        good_wire = json.loads(json.dumps(serde.encode(compiler.DATAFORM_FEW_SHOTS[-2][1]))
                               .replace('"ca7"', '"ca6"'))
        fn = scripted(bad, json.dumps(good_wire))
        with mock.patch.object(client, "chat", fn):
            result = compiler.compile_question(
                question, registry=schema.load("dataform"), use_cache=False,
            )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.attempts), 2)
        self.assertIn("copied_question_constraint", codes(result.attempts[0]["errors"]))
        self.assertIn('"ca6"', result.printed)

    def test_terse_search_requires_circuit_year_and_topic(self):
        empty = serde.encode(Query(
            (f("proc", "envelope", "id"),), t("proceeding", "proc"), None, (), 10,
        ))
        errors = compiler.semantic_coverage_errors("6th circuit 2019 abortion", empty)
        messages = " ".join(error["message"] for error in errors)
        self.assertIn("ca6", messages)
        self.assertIn("2019-01-01", messages)
        self.assertIn("abortion", messages)

    def test_terse_search_few_shot_covers_every_constraint(self):
        wire = json.loads(
            json.dumps(serde.encode(compiler.DATAFORM_FEW_SHOTS[-1][1]))
            .replace('"ca5"', '"ca6"')
            .replace("2021-", "2019-")
            .replace("voting rights", "abortion")
        )
        self.assertEqual(
            compiler.semantic_coverage_errors("6th circuit 2019 abortion", wire), [],
        )

    def test_word_ordinal_and_disposition_constraints_are_required(self):
        question = ("Second Circuit securities-fraud opinions since 2010 that "
                    "reversed a motion to dismiss.")
        empty = serde.encode(Query(
            (f("doc", "envelope", "id"),), t("document", "doc"), None, (), 10,
        ))
        messages = " ".join(
            error["message"] for error in compiler.semantic_coverage_errors(question, empty)
        )
        for expected in ("ca2", "2010", "securities fraud", "reversed motion to dismiss"):
            self.assertIn(expected, messages)

    def test_since_rejects_an_invented_calendar_year_upper_bound(self):
        wire = serde.encode(Query(
            (f("doc", "envelope", "id"),), t("document", "doc"),
            Between(f("doc", "date_issued"), "2022-01-01", "2022-12-31"), (), 10,
        ))
        errors = compiler.semantic_coverage_errors("opinions since 2022", wire)
        self.assertIn("invented_question_constraint", codes(errors))

    def test_explicit_date_range_allows_an_upper_bound(self):
        wire = serde.encode(Query(
            (f("doc", "envelope", "id"),), t("document", "doc"),
            Between(f("doc", "date_issued"), "2022-01-01", "2024-12-31"), (), 10,
        ))
        errors = compiler.semantic_coverage_errors(
            "opinions since 2022 but not after 2024", wire,
        )
        self.assertNotIn("invented_question_constraint", codes(errors))

    def test_unrequested_proceeding_constraint_is_rejected(self):
        wire = serde.encode(Query(
            (f("doc", "envelope", "id"),),
            Join(eq(f("doc", "proceeding_id"), f("proc", "envelope", "id")),
                 t("document", "doc"), t("proceeding", "proc")),
            eq(f("proc", "proceeding_type"), "case"), (), 10,
        ))
        errors = compiler.semantic_coverage_errors("Sixth Circuit opinions", wire)
        self.assertIn("invented_question_constraint", codes(errors))

    def test_unambiguous_question_constraints_are_reconciled_locally(self):
        wrong = Query(
            (f("doc", "envelope", "id"),),
            Join(eq(f("doc", "proceeding_id"), f("proc", "envelope", "id")),
                 t("document", "doc"), t("proceeding", "proc")),
            And((
                eq(f("doc", "jurisdiction"), "ky6"),
                Between(f("doc", "date_issued"), "2022-01-01", "2022-12-31"),
                eq(f("proc", "proceeding_type"), "case"),
            )), (), 10,
        )
        fixed = compiler._reconcile_question_constraints(
            "6th Circuit opinions since 2022", wrong,
        )
        self.assertEqual(fixed.source, t("document", "doc"))
        rendered = pp_query(fixed)
        self.assertIn('doc.jurisdiction = "ca6"', rendered)
        self.assertIn('doc.date_issued >= "2022-01-01"', rendered)
        self.assertNotIn("2022-12-31", rendered)
        self.assertNotIn("proceeding", rendered)

    def test_before_after_and_bare_years_have_distinct_semantics(self):
        base = Query(
            (f("doc", "envelope", "id"),), t("document", "doc"),
            Comparison(Op.GE, f("doc", "date_issued"), "2020-01-01"), (), 10,
        )
        before = pp_query(compiler._reconcile_question_constraints(
            "opinions before 2020", base,
        ))
        after = pp_query(compiler._reconcile_question_constraints(
            "opinions after 2020", base,
        ))
        bare = pp_query(compiler._reconcile_question_constraints(
            "2020 opinions", base,
        ))
        self.assertIn('doc.date_issued < "2020-01-01"', before)
        self.assertIn('doc.date_issued > "2020-12-31"', after)
        self.assertIn('between "2020-01-01" and "2020-12-31"', bare)

    def test_after_year_accepts_and_canonicalizes_next_year_boundary(self):
        base = Query(
            (f("doc", "envelope", "id"),), t("document", "doc"),
            Comparison(Op.GE, f("doc", "date_issued"), "2021-01-01"), (), 10,
        )
        fixed = compiler._reconcile_question_constraints("opinions after 2020", base)
        self.assertIn('doc.date_issued > "2020-12-31"', pp_query(fixed))

    def test_duplicate_date_repairs_are_collapsed(self):
        base = Query(
            (f("doc", "envelope", "id"),), t("document", "doc"),
            And((
                Comparison(Op.GE, f("doc", "date_issued"), "2019-01-01"),
                Comparison(Op.LE, f("doc", "date_issued"), "2019-12-31"),
            )), (), 10,
        )
        fixed = compiler._reconcile_question_constraints("2019 opinions", base)
        self.assertEqual(pp_query(fixed).count("between"), 1)

    def test_supreme_review_relationship_is_built_deterministically(self):
        registry = schema.load("dataform")
        question = "Find 6th Circuit cases that went up to the Supreme Court in 2019."
        self.assertNotIn("Supreme Court", compiler._question_for_model(question, registry))
        base = Query(
            (f("doc", "envelope", "id"), f("doc", "title")),
            t("document", "doc"),
            And((
                eq(f("doc", "jurisdiction"), "ky6"),
                Comparison(Op.GE, f("doc", "date_issued"), "2019-01-01"),
            )), (), 10,
        )
        fixed = compiler._reconcile_question_constraints(question, base)
        wire = serde.encode(fixed)
        rendered = pp_query(fixed)
        self.assertIn("join proceeding as proc", rendered)
        self.assertIn("join event as ev", rendered)
        self.assertIn("Supreme Court", rendered)
        self.assertIn('between "2019-01-01" and "2019-12-31"', rendered)
        self.assertIn('doc.jurisdiction = "ca6"', rendered)
        self.assertEqual(checks.validate(fixed, registry), [])
        self.assertEqual(compiler.semantic_coverage_errors(question, wire), [])
        self.assertTrue(typecheck(fixed, registry_to_schema(registry)))

    def test_bad_court_join_is_replaced_by_document_jurisdiction(self):
        registry = schema.load("dataform")
        wrong = Query(
            (f("doc", "envelope", "id"),),
            Join(
                eq(f("court", "envelope", "id"), f("court", "jurisdiction")),
                t("document", "doc"), t("organization", "court"),
            ),
            eq(f("court", "jurisdiction"), "ca1"), (), 10,
        )
        self.assertTrue(checks.validate(wrong, registry))
        fixed = compiler._reconcile_question_constraints("First Circuit opinions", wrong)
        self.assertEqual(fixed.source, t("document", "doc"))
        self.assertIn('doc.jurisdiction = "ca1"', pp_query(fixed))
        self.assertEqual(checks.validate(fixed, registry), [])

    def test_explicit_topic_is_restored_when_model_drops_it(self):
        base = Query(
            (f("doc", "envelope", "id"),), t("document", "doc"),
            eq(f("doc", "jurisdiction"), "ca1"), (), 10,
        )
        fixed = compiler._reconcile_question_constraints(
            "First Circuit opinions before 2020 involving habeas relief", base,
        )
        self.assertIn('fuzzy(doc.summary, "habeas relief")', pp_query(fixed))

    def test_complex_compile_keeps_thinking_and_a_large_output_ceiling(self):
        self.assertTrue(compiler.THINKING)
        self.assertGreaterEqual(compiler.MAX_TOKENS, 8192)


class TestRouter(unittest.TestCase):
    def test_router_returns_a_route_and_derives_is_legal(self):
        fn = scripted('{"route": "compile"}')
        with mock.patch.object(client, "chat", fn):
            result = relevance.classify("cases about qualified immunity")
        self.assertEqual(result.route, "compile")
        self.assertTrue(result.is_legal)
        messages, kwargs = fn.sent[0]
        self.assertIn("legal_request_router", messages[0]["content"])
        self.assertEqual(kwargs["model"], config.ROUTER_MODEL)
        self.assertEqual(kwargs["temperature"], 0.0)
        self.assertIs(kwargs["enable_thinking"], False)
        self.assertEqual(kwargs["reasoning_budget"], 0)
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    def test_router_defaults_to_the_main_hosted_model(self):
        """No separate local router: the router runs on the same hosted model as
        everything else unless AMICUS_ROUTER_MODEL names another one."""
        self.assertEqual(config.ROUTER_MODEL, config.MODEL)

    def test_reject_is_not_legal(self):
        self.assertFalse(relevance.RelevanceResult("reject", "m").is_legal)
        self.assertTrue(relevance.RelevanceResult("answer", "m").is_legal)

    def test_invalid_router_shape_fails_closed(self):
        for reply in ('{"related": true}', '{"route": "other"}', 'no json at all'):
            with self.subTest(reply=reply), self.assertRaises(client.ModelError):
                relevance.decode(reply)

    def test_mock_router_handles_all_three_routes(self):
        self.assertEqual(relevance._mock_route("Find cases about immunity"), "compile")
        self.assertEqual(relevance._mock_route("What is qualified immunity?"), "answer")
        self.assertEqual(relevance._mock_route("Give me a chocolate cake recipe"), "reject")


class TestLegalAnswer(unittest.TestCase):
    def test_super_answer_uses_separate_plain_text_prompt(self):
        fn = scripted("General legal information.")
        with mock.patch.object(client, "chat", fn):
            response = legal_answer.answer_question("What is negligence?")
        self.assertEqual(response.text, "General legal information.")
        messages, kwargs = fn.sent[0]
        self.assertEqual(kwargs["model"], config.MODEL)
        self.assertTrue(kwargs["enable_thinking"])
        self.assertEqual(kwargs["reasoning_budget"], 256)
        self.assertIn("rather than legal advice", messages[0]["content"])

    def test_empty_answer_is_an_error(self):
        with mock.patch.object(client, "chat", scripted("   ")):
            with self.assertRaises(client.ModelError):
                legal_answer.answer_question("What is negligence?")


class TestPipeline(unittest.TestCase):
    """The driver owns routing: three routes, three envelope shapes."""

    def run_question(self, route: str, *, replies=("{}",)):
        decision = relevance.RelevanceResult(route, "router-test", 5)
        with (mock.patch.object(relevance, "classify", return_value=decision),
              mock.patch.object(client, "chat", scripted(*replies))):
            return driver.run("a question", schema_name="courtlistener", use_cache=False)

    def test_reject_never_reaches_the_compiler(self):
        out = self.run_question("reject")
        self.assertFalse(out["ok"])
        self.assertEqual(out["mode"], "reject")
        self.assertIs(out["is_legal"], False)
        self.assertIn("legal research", out["message"])
        self.assertEqual([s["status"] for s in out["stages"]],
                         ["failed", "skipped", "skipped", "skipped", "skipped"])

    def test_answer_route_returns_prose_and_no_query(self):
        with mock.patch.object(legal_answer, "answer_question",
                               return_value=client.ChatResponse("Prose.", "super-test")):
            out = self.run_question("answer")
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "answer")
        self.assertEqual(out["answer"], "Prose.")
        self.assertIsNone(out["query"])
        self.assertIsNone(out["plan"])

    def test_compile_route_plans_the_query(self):
        good = json.dumps(serde.encode(q(where=Fuzzy(f("cluster", "scan_pages"), "photo"))))
        out = self.run_question("compile", replies=(good,))
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "compile")
        self.assertEqual(out["query"]["kind"], "Query")
        self.assertIn("select", out["bql"])
        stages = {s["name"]: s["status"] for s in out["stages"]}
        self.assertEqual(stages["relevance"], "ok")
        self.assertEqual(stages["compile"], "ok")
        self.assertEqual(stages["typecheck"], "ok")
        self.assertEqual(stages["optimize"], "ok")
        self.assertEqual(stages["execute"], "stub")   # runtime.executor is not built yet
        self.assertTrue(out["plan"]["snapshots"])

    def test_router_failure_fails_open_to_compile(self):
        good = json.dumps(serde.encode(q()))
        with (mock.patch.object(relevance, "classify",
                                side_effect=client.ModelError("router down")),
              mock.patch.object(client, "chat", scripted(good))):
            out = driver.run("a question", schema_name="courtlistener", use_cache=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "compile")
        self.assertEqual(out["stages"][0]["status"], "failed")

    def test_a_failed_compile_reports_the_compile_error_not_the_router(self):
        with (mock.patch.object(relevance, "classify",
                                side_effect=client.ModelError("router down")),
              mock.patch.object(client, "chat", scripted("no json"))):
            out = driver.run("a question", schema_name="courtlistener", use_cache=False)
        self.assertFalse(out["ok"])
        self.assertIn("could not compile", out["message"])


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

    def test_streams_final_content_and_discards_reasoning(self):
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

        class Usage:
            prompt_tokens = 3239
            completion_tokens = 491
            completion_tokens_details = type("D", (), {"reasoning_tokens": 300})

        class Chunk2:
            """The trailing usage-only chunk: no choices, just the token counts."""
            choices = []
            usage = Usage()

        class FakeCompletions:
            def create(self, **kwargs):
                seen["request"] = kwargs
                return [Chunk(),
                        Chunk(Delta(reasoning_content="private chain of thought")),
                        Chunk(Delta(content='{"kind":')),
                        Chunk(Delta(content='"Query"}')),
                        Chunk2()]

        class FakeSdk:
            chat = type("Chat", (), {"completions": FakeCompletions()})

        with (mock.patch.object(client, "_sdk", lambda *a: FakeSdk()),
              mock.patch.object(config, "TRACE", False)):
            response = client.chat([{"role": "user", "content": "hello"}], purpose="compile")

        self.assertEqual(response.text, '{"kind":"Query"}')
        self.assertTrue(response.thought)
        self.assertNotIn("private chain", response.text)
        self.assertEqual(seen["request"]["model"], config.MODEL)
        self.assertIs(seen["request"]["stream"], True)
        self.assertEqual(seen["request"]["stream_options"], {"include_usage": True})
        self.assertEqual(seen["request"]["extra_body"], {
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning_budget": client.REASONING_BUDGET,
        })

    def test_a_streamed_call_reports_its_tokens_and_where_they_went(self):
        """A streamed response carries no usage unless it is asked for, and the split
        between thinking and answering is the number worth having."""
        class Usage:
            prompt_tokens = 3239
            completion_tokens = 491
            completion_tokens_details = type("D", (), {"reasoning_tokens": 300})

        response = client.ChatResponse(
            text="{}", model="m", purpose="compile", tokens_in=3239, tokens_out=491,
            reasoning_tokens=300, latency_ms=4500, ttfb_ms=400, thinking_ms=2600)
        self.assertEqual(response.answer_tokens, 191)
        self.assertEqual(response.writing_ms, 1500)
        self.assertAlmostEqual(response.tokens_per_s, 491 / 4.1, places=1)
        telemetry = response.telemetry()
        self.assertEqual(telemetry["thinking_ms"], 2600)
        self.assertEqual(telemetry["reasoning_tokens"], 300)
        self.assertEqual(telemetry["purpose"], "compile")
        self.assertIn("think", response.line())

    def test_reasoning_tokens_are_split_by_character_share_when_unreported(self):
        """NVIDIA returns completion_tokens_details=None, so the split is derived from
        how much of the stream was trace -- never from a chars-per-token constant."""
        bare = type("U", (), {"completion_tokens_details": None, "completion_tokens": 66})()
        tokens, estimated = client._reasoning_tokens(bare, 263, 12)
        self.assertTrue(estimated)
        self.assertEqual(tokens, round(66 * 263 / 275))
        self.assertLess(tokens, 66)          # the answer always keeps some of the total

        reported = type("U", (), {"completion_tokens": 66,
                                  "completion_tokens_details": type("D", (), {"reasoning_tokens": 42})})()
        self.assertEqual(client._reasoning_tokens(reported, 263, 12), (42, False))

        no_trace = type("U", (), {"completion_tokens_details": None, "completion_tokens": 66})()
        self.assertEqual(client._reasoning_tokens(no_trace, 0, 120), (0, False))

    def test_a_missing_key_is_an_operator_error(self):
        with mock.patch.object(config, "API_KEY", ""), mock.patch.object(config, "MOCK", False):
            with self.assertRaises(client.ModelError) as caught:
                client.api_key()
        self.assertIn("NVIDIA_API_KEY", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
