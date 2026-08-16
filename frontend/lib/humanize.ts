import {
  fieldKey,
  isFieldRef,
  sourceTables,
  type BqlQuery,
  type Comparison,
  type ComparisonOp,
  type Condition,
  type Expression,
  type FieldRef,
  type Literal,
} from "@/lib/bql";

/* ---------------------------------------- */
/* dictionaries - friendly names. everything has a fallback so an unknown */
/* table / field / value still renders (just more literally)               */
/* ---------------------------------------- */

/** friendly names for the tables the query touches */
export const TABLE_LABELS: Record<string, string> = {
  cluster: "Cases",
  docket: "Dockets",
  opinion: "Opinions",
  citation: "Citations",
  audio: "Oral arguments",
  scan: "Scanned records",
};

/** friendly field names, keyed by `table.column` */
const FIELD_LABELS: Record<string, string> = {
  "docket.court_id": "Court",
  "citation.cited_cite": "Cites",
  "opinion.plain_text": "Opinion text",
  "cluster.scan_pages": "Scanned record",
  "docket.argument": "Oral argument",
  "docket.argument_transcript": "Oral argument transcript",
  "cluster.case_name": "Case name",
  "cluster.id": "Case ID",
  "cluster.date_filed": "Date filed",
  "cluster.precedential_status": "Precedential status",
  "docket.docket_number": "Docket number",
  "docket.date_terminated": "Date terminated",
  "opinion.author": "Author",
  "opinion.type": "Opinion type",
};

/** verb used when a text field is matched by meaning */
const FIELD_VERBS: Record<string, string> = {
  "opinion.plain_text": "discusses",
  "cluster.scan_pages": "contains",
  "docket.argument": "includes",
  "docket.argument_transcript": "includes",
};

/** connector for `=` on this field, default "is". empty when the label already reads like a verb */
const EQUALS_CONNECTORS: Record<string, string> = {
  "citation.cited_cite": "",
};

const COURT_NAMES: Record<string, string> = {
  scotus: "U.S. Supreme Court",
  ca1: "First Circuit",
  ca2: "Second Circuit",
  ca3: "Third Circuit",
  ca4: "Fourth Circuit",
  ca5: "Fifth Circuit",
  ca6: "Sixth Circuit",
  ca7: "Seventh Circuit",
  ca8: "Eighth Circuit",
  ca9: "Ninth Circuit",
  ca10: "Tenth Circuit",
  ca11: "Eleventh Circuit",
  cadc: "D.C. Circuit",
  cafc: "Federal Circuit",
};

const KNOWN_CITATIONS: Record<string, string> = {
  "490 U.S. 386": "Graham v. Connor",
  "467 U.S. 837": "Chevron U.S.A. v. NRDC",
  "509 U.S. 579": "Daubert v. Merrell Dow",
};

/* ---------------------------------------- */
/* field / value humanizing                 */
/* ---------------------------------------- */

export function fieldLabel(key: string) {
  return (
    FIELD_LABELS[key] ?? key.split(".").pop()?.replace(/_/g, " ") ?? key
  );
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ");

function isDateField(key: string) {
  return /date|filed|decided|terminated|argued/i.test(key.split(".").pop() ?? "");
}
function isDateValue(v: Literal) {
  return typeof v === "string" && ISO_DATE.test(v);
}
function formatDate(iso: string) {
  const [y, m, d] = iso.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}, ${y}`;
}

export function humanizeValue(key: string, v: Literal): string {
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return String(v);
  if (key === "docket.court_id") return COURT_NAMES[v] ?? v.toUpperCase();
  if (key === "citation.cited_cite") {
    const name = KNOWN_CITATIONS[v];
    return name ? `${name}, ${v}` : v;
  }
  if (isDateValue(v)) return formatDate(v);
  return v;
}

function humanizeExpression(e: Expression): string {
  return isFieldRef(e) ? fieldLabel(fieldKey(e)) : humanizeValue("", e);
}

/* ---------------------------------------- */
/* conditions -> criteria tree              */
/* ---------------------------------------- */

export type Leaf = {
  kind: "leaf";
  label: string; // e.g. "Court"
  /** word(s) between label and value: "is", "is before", "discusses", "" ... */
  connector: string;
  value: string; // e.g. "Ninth Circuit"
  /** quote the value (free text) instead of treating it as a name/number */
  quote: boolean;
  exact: boolean;
};
export type Group = { kind: "group"; op: "all" | "any"; children: Criterion[] };
export type Negation = { kind: "not"; child: Criterion };
export type Unknown = { kind: "unknown"; raw: string };
export type Criterion = Leaf | Group | Negation | Unknown;

const FLIP: Record<ComparisonOp, ComparisonOp> = {
  "<": ">",
  "<=": ">=",
  "=": "=",
  ">": "<",
  ">=": "<=",
};

function comparisonWords(op: ComparisonOp, dateLike: boolean, key: string) {
  switch (op) {
    case "=":
      return EQUALS_CONNECTORS[key] ?? "is";
    case "<":
      return dateLike ? "is before" : "is less than";
    case "<=":
      return dateLike ? "is on or before" : "is at most";
    case ">":
      return dateLike ? "is after" : "is more than";
    case ">=":
      return dateLike ? "is on or after" : "is at least";
  }
}

function describeComparison(c: Comparison): Criterion {
  let { op, field1: a, field2: b } = c;
  // normalise so the FieldRef sits on the left when theres exactly one
  if (!isFieldRef(a) && isFieldRef(b)) {
    [a, b] = [b, a];
    op = FLIP[op];
  }
  if (isFieldRef(a) && isFieldRef(b)) {
    // field-to-field (mostly join conditions)
    return {
      kind: "leaf",
      label: fieldLabel(fieldKey(a)),
      connector: op === "=" ? "matches" : `is ${op}`,
      value: fieldLabel(fieldKey(b)),
      quote: false,
      exact: true,
    };
  }
  if (isFieldRef(a)) {
    const key = fieldKey(a);
    const lit = b as Literal;
    const dateLike = isDateField(key) || isDateValue(lit);
    return {
      kind: "leaf",
      label: fieldLabel(key),
      connector: comparisonWords(op, dateLike, key),
      value: humanizeValue(key, lit),
      quote: false,
      exact: true,
    };
  }
  // literal-vs-literal: wierd, but render something sensible
  return {
    kind: "leaf",
    label: humanizeExpression(a),
    connector: op,
    value: humanizeExpression(b),
    quote: false,
    exact: true,
  };
}

function joinList(items: string[], conj: "or" | "and") {
  if (items.length <= 1) return items.join("");
  if (items.length === 2) return `${items[0]} ${conj} ${items[1]}`;
  return `${items.slice(0, -1).join(", ")}, ${conj} ${items[items.length - 1]}`;
}

/** sql like -> plain words */
function describeLike(field: FieldRef, pattern: string): Leaf {
  const key = fieldKey(field);
  const starts = pattern.startsWith("%");
  const ends = pattern.endsWith("%");
  const core = pattern.replace(/^%+|%+$/g, "").replace(/%/g, "…");
  let connector: string;
  if (starts && ends) connector = "contains";
  else if (ends) connector = "starts with";
  else if (starts) connector = "ends with";
  else connector = "matches";
  return {
    kind: "leaf",
    label: fieldLabel(key),
    connector,
    value: core,
    quote: true,
    exact: true,
  };
}

/** recursively describe any condition. never throws - unknown shapes become `Unknown` */
export function describeCondition(c: Condition): Criterion {
  switch (c?.type) {
    case "comparison":
      return describeComparison(c);
    case "in_list": {
      const key = fieldKey(c.field);
      return {
        kind: "leaf",
        label: fieldLabel(key),
        connector: "is one of",
        value: joinList(
          c.values.map((v) => humanizeValue(key, v)),
          "or",
        ),
        quote: false,
        exact: true,
      };
    }
    case "between": {
      const key = fieldKey(c.field);
      return {
        kind: "leaf",
        label: fieldLabel(key),
        connector: "is between",
        value: `${humanizeValue(key, c.low)} and ${humanizeValue(key, c.high)}`,
        quote: false,
        exact: true,
      };
    }
    case "like":
      return describeLike(c.field, c.pattern);
    case "fuzzy": {
      const keys = c.field.map(fieldKey);
      const verb = FIELD_VERBS[keys[0]] ?? "mentions";
      let value = c.text;
      // avoid "contains 'contains a ...'" when the phrase already starts with the verb
      const lead = new RegExp(`^${verb}\\s+`, "i");
      if (lead.test(value)) value = value.replace(lead, "");
      return {
        kind: "leaf",
        label: joinList(keys.map(fieldLabel), "or"),
        connector: verb,
        value,
        quote: true,
        exact: false,
      };
    }
    case "and":
    case "or":
      return {
        kind: "group",
        op: c.type === "and" ? "all" : "any",
        children: c.children.map(describeCondition),
      };
    case "not":
      return { kind: "not", child: describeCondition(c.child) };
    default:
      return { kind: "unknown", raw: JSON.stringify(c) };
  }
}

/* ---------------------------------------- */
/* query-level helpers                      */
/* ---------------------------------------- */

/** top-level criteria list. a top-level and/or gets flattened into its children (op reported); anything else is a 1-item list */
export function topLevelCriteria(q: BqlQuery): {
  op: "all" | "any";
  items: Criterion[];
} {
  if (!q.where) return { op: "all", items: [] };
  const root = describeCondition(q.where);
  if (root.kind === "group") return { op: root.op, items: root.children };
  return { op: "all", items: [root] };
}

/** which tables the query draws on */
export function tablesUsed(q: BqlQuery) {
  return sourceTables(q.source);
}

/** friendly labels for selected columns (literals rendered as-is) */
export function selectedLabels(q: BqlQuery): string[] {
  return q.select.map((e) =>
    isFieldRef(e) ? fieldLabel(fieldKey(e)) : humanizeValue("", e),
  );
}

/** what kind of thing comes back - from the first selected table */
export function resultNoun(q: BqlQuery): string {
  const first = q.select.find(isFieldRef);
  const table = first?.source ?? sourceTables(q.source)[0];
  return (table && TABLE_LABELS[table]?.toLowerCase()) || "results";
}
