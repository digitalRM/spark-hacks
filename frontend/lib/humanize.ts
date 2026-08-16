import {
  fieldKey,
  fieldRefFor,
  isAggregator,
  isFieldRef,
  isUnnest,
  sourceTables,
  type BqlQuery,
  type Comparison,
  type ComparisonOp,
  type Condition,
  type Expression,
  type FieldRef,
  type Literal,
} from "@/lib/bql";

/** Friendly names have literal fallbacks, so new schema fields remain renderable. */
export const TABLE_LABELS: Record<string, string> = {
  cluster: "Cases",
  docket: "Dockets",
  opinion: "Opinions",
  citation: "Citations",
  audio: "Oral arguments",
  scan: "Scanned records",
  document: "Documents",
  proceeding: "Proceedings",
  organization: "Organizations",
  person: "People",
  position: "Positions",
  financial_disclosure: "Financial disclosures",
};

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
  "doc.envelope.id": "Document ID",
  "doc.envelope.source_system": "Source system",
  "doc.title": "Title",
  "doc.media.text.plain_text": "Document text",
  "doc.media.images": "Document images",
  "doc.media.audio": "Document audio",
};

const FIELD_VERBS: Record<string, string> = {
  "opinion.plain_text": "discusses",
  "cluster.scan_pages": "contains",
  "docket.argument": "includes",
  "docket.argument_transcript": "includes",
  "doc.media.text.plain_text": "discusses",
  "doc.media.images": "contains",
  "doc.media.audio": "includes",
};

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

export function fieldLabel(key: string) {
  return FIELD_LABELS[key] ?? key.split(".").pop()?.replace(/_/g, " ") ?? key;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ");

function isDateField(key: string) {
  return /date|filed|decided|terminated|argued/i.test(key.split(".").pop() ?? "");
}

function isDateValue(value: Literal) {
  return typeof value === "string" && ISO_DATE.test(value);
}

function formatDate(iso: string) {
  const [year, month, day] = iso.split("-").map(Number);
  return `${MONTHS[month - 1]} ${day}, ${year}`;
}

export function humanizeValue(key: string, value: Literal): string {
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return String(value);
  if (key === "docket.court_id") return COURT_NAMES[value] ?? value.toUpperCase();
  if (key === "citation.cited_cite") {
    const name = KNOWN_CITATIONS[value];
    return name ? `${name}, ${value}` : value;
  }
  if (isDateValue(value)) return formatDate(value);
  return value;
}

function humanizeExpression(expression: Expression): string {
  if (isFieldRef(expression)) return fieldLabel(fieldKey(expression));
  if (isUnnest(expression)) return `each ${fieldLabel(fieldKey(expression.ref))}`;
  if (isAggregator(expression)) {
    const argument = expression.arg ? humanizeExpression(expression.arg) : "all rows";
    return `${expression.op} of ${argument}`;
  }
  return humanizeValue("", expression);
}

export type Leaf = {
  kind: "leaf";
  label: string;
  connector: string;
  value: string;
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
  "!=": "!=",
  ">": "<",
  ">=": "<=",
};

function comparisonWords(op: ComparisonOp, dateLike: boolean, key: string) {
  switch (op) {
    case "=":
      return EQUALS_CONNECTORS[key] ?? "is";
    case "!=":
      return "is not";
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

function describeComparison(comparison: Comparison): Criterion {
  let { op, field1: left, field2: right } = comparison;
  if (!isFieldRef(left) && isFieldRef(right)) {
    [left, right] = [right, left];
    op = FLIP[op];
  }
  if (isFieldRef(left) && isFieldRef(right)) {
    return {
      kind: "leaf",
      label: fieldLabel(fieldKey(left)),
      connector: op === "=" ? "matches" : `is ${op}`,
      value: fieldLabel(fieldKey(right)),
      quote: false,
      exact: true,
    };
  }
  if (isFieldRef(left) && !isFieldRef(right) && !isUnnest(right) && !isAggregator(right)) {
    const key = fieldKey(left);
    const dateLike = isDateField(key) || isDateValue(right);
    return {
      kind: "leaf",
      label: fieldLabel(key),
      connector: comparisonWords(op, dateLike, key),
      value: humanizeValue(key, right),
      quote: false,
      exact: true,
    };
  }
  return {
    kind: "leaf",
    label: humanizeExpression(left),
    connector: op,
    value: humanizeExpression(right),
    quote: false,
    exact: true,
  };
}

function joinList(items: string[], conjunction: "or" | "and") {
  if (items.length <= 1) return items.join("");
  if (items.length === 2) return `${items[0]} ${conjunction} ${items[1]}`;
  return `${items.slice(0, -1).join(", ")}, ${conjunction} ${items.at(-1)}`;
}

function describeLike(field: FieldRef, pattern: string): Leaf {
  const key = fieldKey(field);
  const starts = pattern.startsWith("%");
  const ends = pattern.endsWith("%");
  const core = pattern.replace(/^%+|%+$/g, "").replace(/%/g, "…");
  const connector = starts && ends
    ? "contains"
    : ends
      ? "starts with"
      : starts
        ? "ends with"
        : "matches";
  return {
    kind: "leaf",
    label: fieldLabel(key),
    connector,
    value: core,
    quote: true,
    exact: true,
  };
}

/** Recursively describe any canonical v2 condition. */
export function describeCondition(condition: Condition): Criterion {
  switch (condition.kind) {
    case "Comparison":
      return describeComparison(condition);
    case "InList": {
      const key = fieldKey(condition.field);
      return {
        kind: "leaf",
        label: fieldLabel(key),
        connector: "is one of",
        value: joinList(condition.values.map((value) => humanizeValue(key, value)), "or"),
        quote: false,
        exact: true,
      };
    }
    case "Between": {
      const key = fieldKey(condition.field);
      return {
        kind: "leaf",
        label: fieldLabel(key),
        connector: "is between",
        value: `${humanizeValue(key, condition.low)} and ${humanizeValue(key, condition.high)}`,
        quote: false,
        exact: true,
      };
    }
    case "Like":
      return describeLike(condition.field, condition.pattern);
    case "Fuzzy": {
      const ref = fieldRefFor(condition.field);
      const key = ref ? fieldKey(ref) : "content";
      const verb = FIELD_VERBS[key] ?? "mentions";
      const lead = new RegExp(`^${verb}\\s+`, "i");
      const value = lead.test(condition.text) ? condition.text.replace(lead, "") : condition.text;
      return {
        kind: "leaf",
        label: fieldLabel(key),
        connector: verb,
        value,
        quote: true,
        exact: false,
      };
    }
    case "And":
    case "Or":
      return {
        kind: "group",
        op: condition.kind === "And" ? "all" : "any",
        children: condition.children.map(describeCondition),
      };
    case "Not":
      return { kind: "not", child: describeCondition(condition.child) };
  }
}

export function topLevelCriteria(query: BqlQuery): {
  op: "all" | "any";
  items: Criterion[];
} {
  if (!query.where) return { op: "all", items: [] };
  const root = describeCondition(query.where);
  if (root.kind === "group") return { op: root.op, items: root.children };
  return { op: "all", items: [root] };
}

export function tablesUsed(query: BqlQuery) {
  return sourceTables(query.source);
}

export function selectedLabels(query: BqlQuery): string[] {
  return query.select.map(humanizeExpression);
}

export function resultNoun(query: BqlQuery): string {
  const table = sourceTables(query.source)[0];
  return (table && TABLE_LABELS[table]?.toLowerCase()) || "results";
}
