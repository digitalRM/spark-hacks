import type { BqlQuery, FieldRef } from "@/lib/bql";

export const DUMMY_QUERY =
  "9th Circuit qualified-immunity cases citing Graham v. Connor, where the scanned record contains a photographic exhibit, and a judge expressed skepticism at oral argument.";

/** Example searches cycled through the input placeholder. */
export const EXAMPLE_QUERIES = [
  DUMMY_QUERY,
  "Second Circuit securities-fraud opinions since 2020 that reversed a motion to dismiss.",
  "Delaware Chancery decisions applying Revlon duties to a stock-for-stock merger.",
  "Supreme Court cases citing Chevron that were decided 5–4.",
  "Texas appellate opinions where an expert witness was excluded under Daubert.",
  "Federal district court orders granting class certification in wage-and-hour cases.",
];

const field = (source: string, ...path: string[]): FieldRef => ({
  kind: "FieldRef",
  source,
  path,
});

/** Canonical AST v2 fixture retained for isolated visual development and tests. */
export const DUMMY_BQL_AST: BqlQuery = {
  kind: "Query",
  select: [field("cluster", "id"), field("cluster", "case_name")],
  source: {
    kind: "Join",
    condition: {
      kind: "Comparison",
      op: "=",
      field1: field("cluster", "docket_id"),
      field2: field("docket", "id"),
    },
    left: { kind: "TableRef", name: "cluster", alias: "cluster" },
    right: { kind: "TableRef", name: "docket", alias: "docket" },
  },
  where: {
    kind: "And",
    children: [
      {
        kind: "Comparison",
        op: "=",
        field1: field("docket", "court_id"),
        field2: "ca9",
      },
      {
        kind: "Fuzzy",
        field: field("cluster", "scan_pages"),
        text: "contains a photographic exhibit",
      },
    ],
  },
  group_by: [],
  limit: 10,
};

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };
