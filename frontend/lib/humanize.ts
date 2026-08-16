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

/**
 * Labels keyed by full `alias.path` first, then by alias-free path, so the
 * model's alias choice (`doc`, `d`, `document`, ...) never changes the wording.
 */
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

const PATH_LABELS: Record<string, string> = {
  // dataform envelope
  "envelope.id": "Document ID",
  "envelope.source_system": "Source system",
  "envelope.external_ids": "External IDs",
  // dataform document
  doc_type: "Document type",
  title: "Title",
  citation: "Citation",
  jurisdiction: "Jurisdiction",
  issuing_body_id: "Issuing body",
  date_issued: "Date issued",
  status: "Status",
  summary: "Summary",
  "media.text.plain_text": "Document text",
  "media.images": "Document images",
  "media.audio": "Document audio",
  source_pdf_url: "Source PDF",
  hierarchy_path: "Hierarchy path",
  proceeding_id: "Proceeding",
  cites: "Cites",
  // organization / person / position
  org_type: "Organization type",
  short_name: "Short name",
  parent_org_id: "Parent organization",
  role_types: "Role",
  name_first: "First name",
  name_last: "Last name",
  political_affiliations: "Political affiliation",
  photo_ref: "Photo",
  person_id: "Person",
  organization_id: "Organization",
  date_start: "Start date",
  date_end: "End date",
  // proceeding / citation / event
  proceeding_type: "Proceeding type",
  number: "Number",
  party_ids: "Parties",
  date_filed: "Date filed",
  document_ids: "Documents",
  citing_document_id: "Citing document",
  cited_document_id: "Cited document",
  citation_string: "Citation",
  treatment: "Treatment",
  event_type: "Event type",
  date: "Date",
  description: "Description",
  outcome: "Outcome",
  year: "Year",
  filepath: "File",
  disposition: "Disposition",
};

const FIELD_VERBS: Record<string, string> = {
  "opinion.plain_text": "discusses",
  "cluster.scan_pages": "contains",
  "docket.argument": "includes",
  "docket.argument_transcript": "includes",
  "media.text.plain_text": "discusses",
  "media.images": "contains",
  "media.audio": "includes",
  summary: "discusses",
  description: "discusses",
};

const EQUALS_CONNECTORS: Record<string, string> = {
  "citation.cited_cite": "",
};

/** `doc.envelope.source_system` -> `envelope.source_system` */
function pathOf(key: string) {
  const dot = key.indexOf(".");
  return dot === -1 ? key : key.slice(dot + 1);
}

function lastSegment(key: string) {
  return key.split(".").pop() ?? key;
}

/** `date_issued` -> `Date issued` */
function sentenceCase(text: string) {
  const words = text.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const SOURCE_NAMES: Record<string, string> = {
  courtlistener: "CourtListener",
  ecfr: "eCFR",
  govinfo: "GovInfo",
  congress: "Congress.gov",
  oyez: "Oyez",
};

/** Enum-ish tokens whose canonical form has its own casing. */
const TOKEN_NAMES: Record<string, string> = {
  cfr_section: "CFR section",
  regulation_docket: "Regulation docket",
  party_entity: "Party entity",
  federal: "Federal",
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
  return (
    FIELD_LABELS[key] ??
    PATH_LABELS[pathOf(key)] ??
    PATH_LABELS[lastSegment(key)] ??
    sentenceCase(lastSegment(key))
  );
}

function fieldVerb(key: string) {
  return FIELD_VERBS[key] ?? FIELD_VERBS[pathOf(key)] ?? FIELD_VERBS[lastSegment(key)] ?? "mentions";
}

function isCourtField(key: string) {
  const leaf = lastSegment(key);
  return leaf === "court_id" || leaf === "jurisdiction";
}

/** Lowercase snake_case identifiers (`oral_argument`, `ca2`, `opinion`) — never prose. */
const ENUM_TOKEN = /^[a-z][a-z0-9_]*$/;

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
  const leaf = lastSegment(key);
  if (isCourtField(key)) {
    return COURT_NAMES[value] ?? TOKEN_NAMES[value] ?? value.toUpperCase();
  }
  if (leaf === "source_system") return SOURCE_NAMES[value] ?? sentenceCase(value);
  if (key === "citation.cited_cite") {
    const name = KNOWN_CITATIONS[value];
    return name ? `${name}, ${value}` : value;
  }
  if (isDateValue(value)) return formatDate(value);
  // ids, urls, and free text pass through untouched; bare enum tokens get prose casing
  if (/(^|_)(id|ids|url|path|ref)$/.test(leaf)) return value;
  if (ENUM_TOKEN.test(value)) return TOKEN_NAMES[value] ?? sentenceCase(value);
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

export function joinList(items: string[], conjunction: "or" | "and") {
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
      const verb = fieldVerb(key);
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

/** "Document ID" -> "document ID": lowercase only the lead so acronyms survive mid-sentence. */
export function inSentence(label: string) {
  return label.charAt(0).toLowerCase() + label.slice(1);
}

export function resultNoun(query: BqlQuery): string {
  const table = sourceTables(query.source)[0];
  return (table && TABLE_LABELS[table]?.toLowerCase()) || "results";
}
