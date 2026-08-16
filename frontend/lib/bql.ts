/** Canonical BQL AST v2 JSON wire format from query_language/serde.py. */

/** A calendar date. Its own node because a quoted string is text, and text has no order. */
export type DateLiteral = { kind: "Date"; value: string };

export type Literal = string | number | boolean | DateLiteral;

export type FieldRef = {
  kind: "FieldRef";
  source: string;
  path: string[];
};

export type Unnest = { kind: "Unnest"; ref: FieldRef };

export type Aggregator = {
  kind: "Aggregator";
  op: "count" | "sum" | "avg" | "min" | "max";
  arg: Expression | null;
};

export type Expression = Literal | FieldRef | Unnest | Aggregator;

export type ComparisonOp = "<" | "<=" | "=" | "!=" | ">" | ">=";
export type Comparison = {
  kind: "Comparison";
  op: ComparisonOp;
  field1: Expression;
  field2: Expression;
};

export type InList = { kind: "InList"; field: FieldRef; values: Literal[] };
export type Between = {
  kind: "Between";
  field: FieldRef;
  low: Literal;
  high: Literal;
};
export type Like = { kind: "Like"; field: FieldRef; pattern: string };
export type Fuzzy = { kind: "Fuzzy"; field: FieldRef | Unnest; text: string };
export type And = { kind: "And"; children: Condition[] };
export type Or = { kind: "Or"; children: Condition[] };
export type Not = { kind: "Not"; child: Condition };
export type Condition = Comparison | InList | Between | Like | Fuzzy | And | Or | Not;

export type TableRef = { kind: "TableRef"; name: string; alias: string };
export type Join = {
  kind: "Join";
  condition: Comparison;
  left: Source;
  right: Source;
};
export type Source = TableRef | Join;

export type BqlQuery = {
  kind: "Query";
  select: Expression[];
  source: Source;
  where: Condition | null;
  group_by: Expression[];
  limit: number | null;
};

export function isDateLiteral(value: unknown): value is DateLiteral {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { kind?: unknown }).kind === "Date" &&
    typeof (value as { value?: unknown }).value === "string"
  );
}

export function isFieldRef(value: unknown): value is FieldRef {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { kind?: unknown }).kind === "FieldRef"
  );
}

export function isUnnest(value: unknown): value is Unnest {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { kind?: unknown }).kind === "Unnest"
  );
}

export function isAggregator(value: unknown): value is Aggregator {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { kind?: unknown }).kind === "Aggregator"
  );
}

export function isJoin(value: Source): value is Join {
  return value.kind === "Join";
}

/** Alias-qualified nested field path, for example doc.media.text.plain_text. */
export function fieldKey(field: FieldRef): string {
  return [field.source, ...field.path].join(".");
}

/** Extract the underlying field from direct and element-grain expressions. */
export function fieldRefFor(expression: Expression): FieldRef | null {
  if (isFieldRef(expression)) return expression;
  if (isUnnest(expression)) return expression.ref;
  return null;
}

/** Every physical table referenced by a source tree, left-to-right, deduplicated. */
export function sourceTables(source: Source): string[] {
  const tables: string[] = [];
  const walk = (node: Source) => {
    if (isJoin(node)) {
      walk(node.left);
      walk(node.right);
    } else if (!tables.includes(node.name)) {
      tables.push(node.name);
    }
  };
  walk(source);
  return tables;
}
