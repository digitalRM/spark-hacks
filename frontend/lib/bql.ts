/**
 * ts mirror of the bql ast in `query_language/grammar.py`, in the json wire format
 * from `query_language/serialize.py`. keep in sync!
 * every dataclass = object tagged w/ a snake_case `type`; literals are plain json scalars; tuples are arrays
 */

export type Literal = string | number | boolean;

export type FieldRef = { type: "field_ref"; source: string; column: string };
export type Expression = FieldRef | Literal;

export type ComparisonOp = "<" | "<=" | "=" | ">" | ">=";
export type Comparison = {
  type: "comparison";
  op: ComparisonOp;
  field1: Expression;
  field2: Expression;
};
export type InList = { type: "in_list"; field: FieldRef; values: Literal[] };
export type Between = {
  type: "between";
  field: FieldRef;
  low: Literal;
  high: Literal;
};
export type Like = { type: "like"; field: FieldRef; pattern: string };
export type Exact = Comparison | InList | Between | Like;

/** semantic match over one or more fields */
export type Fuzzy = { type: "fuzzy"; field: FieldRef[]; text: string };

export type And = { type: "and"; children: Condition[] };
export type Or = { type: "or"; children: Condition[] };
export type Not = { type: "not"; child: Condition };

export type Condition = Exact | Fuzzy | And | Or | Not;

export type Join = {
  type: "join";
  condition: Condition;
  left: Source;
  right: Source;
};
export type Source = string | Join;

export type BqlQuery = {
  select: Expression[];
  source: Source;
  where: Condition | null;
  limit: number | null;
};

// ---- helpers ----

export function isFieldRef(e: unknown): e is FieldRef {
  return (
    typeof e === "object" &&
    e !== null &&
    (e as { type?: unknown }).type === "field_ref"
  );
}

export function isJoin(s: Source): s is Join {
  return typeof s === "object" && s !== null && s.type === "join";
}

/** `table.column` for a FieldRef */
export function fieldKey(f: FieldRef) {
  return `${f.source}.${f.column}`;
}

/** every base table a source tree references, left-to-right, deduped */
export function sourceTables(s: Source): string[] {
  const out: string[] = [];
  const walk = (x: Source) => {
    if (isJoin(x)) {
      walk(x.left);
      walk(x.right);
    } else if (!out.includes(x)) {
      out.push(x);
    }
  };
  walk(s);
  return out;
}
