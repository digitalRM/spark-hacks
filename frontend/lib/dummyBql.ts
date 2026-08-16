import type { BqlQuery, FieldRef } from "@/lib/bql";

// placeholder compiled-bql ast untill the compiler backend is wired up
export const DUMMY_QUERY =
  "9th Circuit qualified-immunity cases citing Graham v. Connor, where the scanned record contains a photographic exhibit, and a judge expressed skepticism at oral argument.";

/** example searches cycled thru the input placeholder */
export const EXAMPLE_QUERIES = [
  DUMMY_QUERY,
  "Second Circuit securities-fraud opinions since 2020 that reversed a motion to dismiss.",
  "Delaware Chancery decisions applying Revlon duties to a stock-for-stock merger.",
  "Supreme Court cases citing Chevron that were decided 5–4.",
  "Texas appellate opinions where an expert witness was excluded under Daubert.",
  "Federal district court orders granting class certification in wage-and-hour cases.",
];

const f = (source: string, column: string): FieldRef => ({
  type: "field_ref",
  source,
  column,
});

/**
 * same wire shape `query_language/serialize.py` emits for `grammar.example()`, but
 * hitting the whole grammar: nested joins, comparisons, between / in-list, multi-field fuzzy, nested `not (a or b)`
 */
export const DUMMY_BQL_AST: BqlQuery = {
  select: [f("cluster", "id"), f("cluster", "case_name")],
  source: {
    type: "join",
    condition: {
      type: "comparison",
      op: "=",
      field1: f("citation", "citing_opinion_id"),
      field2: f("opinion", "id"),
    },
    left: {
      type: "join",
      condition: {
        type: "comparison",
        op: "=",
        field1: f("opinion", "cluster_id"),
        field2: f("cluster", "id"),
      },
      left: {
        type: "join",
        condition: {
          type: "comparison",
          op: "=",
          field1: f("cluster", "docket_id"),
          field2: f("docket", "id"),
        },
        left: "cluster",
        right: "docket",
      },
      right: "opinion",
    },
    right: "citation",
  },
  where: {
    type: "and",
    children: [
      {
        type: "comparison",
        op: "=",
        field1: f("docket", "court_id"),
        field2: "ca9",
      },
      {
        type: "comparison",
        op: "=",
        field1: f("citation", "cited_cite"),
        field2: "490 U.S. 386",
      },
      {
        type: "between",
        field: f("cluster", "date_filed"),
        low: "2015-01-01",
        high: "2024-12-31",
      },
      {
        type: "fuzzy",
        field: [f("opinion", "plain_text")],
        text: "qualified immunity for excessive force",
      },
      {
        type: "fuzzy",
        field: [f("cluster", "scan_pages")],
        text: "contains a photographic exhibit",
      },
      {
        type: "fuzzy",
        field: [f("docket", "argument"), f("docket", "argument_transcript")],
        text: "a judge expressed skepticism",
      },
      {
        type: "not",
        child: {
          type: "or",
          children: [
            {
              type: "in_list",
              field: f("cluster", "precedential_status"),
              values: ["Unpublished", "Errata"],
            },
            {
              type: "like",
              field: f("cluster", "case_name"),
              pattern: "%In re%",
            },
          ],
        },
      },
    ],
  },
  limit: 10,
};

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };
