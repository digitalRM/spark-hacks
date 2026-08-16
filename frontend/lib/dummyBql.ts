// Placeholder compiled-BQL AST until the compiler backend is wired up.
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

export type BqlColumn = { table: string; column: string };
export type BqlJoin = { table: string; on: { left: string; right: string } };
export type BqlCondition = {
  fn: "EXACT" | "FUZZY";
  field: string;
  value: string;
};
export type BqlQuery = {
  type: "query";
  select: BqlColumn[];
  from: string;
  joins: BqlJoin[];
  where: { op: "AND" | "OR"; args: BqlCondition[] };
  limit: number;
};

export const DUMMY_BQL_AST: BqlQuery = {
  type: "query",
  select: [
    { table: "cluster", column: "id" },
    { table: "cluster", column: "case_name" },
  ],
  from: "cluster",
  joins: [
    { table: "docket", on: { left: "cluster.docket_id", right: "docket.id" } },
    { table: "opinion", on: { left: "opinion.cluster_id", right: "cluster.id" } },
    {
      table: "citation",
      on: { left: "citation.citing_opinion_id", right: "opinion.id" },
    },
  ],
  where: {
    op: "AND",
    args: [
      { fn: "EXACT", field: "docket.court_id", value: "ca9" },
      { fn: "EXACT", field: "citation.cited_cite", value: "490 U.S. 386" },
      {
        fn: "FUZZY",
        field: "opinion.plain_text",
        value: "qualified immunity for excessive force",
      },
      {
        fn: "FUZZY",
        field: "cluster.scan_pages",
        value: "contains a photographic exhibit",
      },
      {
        fn: "FUZZY",
        field: "docket.argument",
        value: "a judge expressed skepticism",
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
