import type { BqlCondition, BqlQuery } from "@/lib/dummyBql";

type PlanNode =
  | { kind: "limit"; count: number; child: PlanNode }
  | { kind: "select"; columns: string[]; child: PlanNode }
  | {
      kind: "filter";
      op: "AND" | "OR";
      conditions: BqlCondition[];
      child: PlanNode;
    }
  | { kind: "join"; left: string; right: string; children: [PlanNode, PlanNode] }
  | { kind: "table"; name: string };

/** Turn the flat BQL AST into a top-down operator tree. */
export function buildPlan(q: BqlQuery): PlanNode {
  let source: PlanNode = { kind: "table", name: q.from };
  for (const j of q.joins) {
    source = {
      kind: "join",
      left: j.on.left,
      right: j.on.right,
      children: [source, { kind: "table", name: j.table }],
    };
  }

  let node: PlanNode = source;
  if (q.where.args.length > 0) {
    node = {
      kind: "filter",
      op: q.where.op,
      conditions: q.where.args,
      child: node,
    };
  }
  node = {
    kind: "select",
    columns: q.select.map((c) => `${c.table}.${c.column}`),
    child: node,
  };
  return { kind: "limit", count: q.limit, child: node };
}

function childrenOf(n: PlanNode): PlanNode[] {
  switch (n.kind) {
    case "table":
      return [];
    case "join":
      return n.children;
    default:
      return [n.child];
  }
}

function Card({
  title,
  children,
  emphasis,
}: {
  title: string;
  children?: React.ReactNode;
  emphasis?: boolean;
}) {
  return (
    <div
      className={
        "rounded-xl border px-3.5 py-2.5 text-left text-sm " +
        (emphasis
          ? "border-neutral-900 bg-neutral-900 text-white"
          : "border-neutral-200 bg-white")
      }
    >
      <div
        className={
          "text-[11px] font-semibold uppercase tracking-wide " +
          (emphasis ? "text-neutral-300" : "text-neutral-400")
        }
      >
        {title}
      </div>
      {children && <div className="mt-1">{children}</div>}
    </div>
  );
}

function ConditionRow({ c }: { c: BqlCondition }) {
  const fuzzy = c.fn === "FUZZY";
  return (
    <li className="flex items-baseline gap-2 whitespace-nowrap">
      <span
        className={
          "rounded px-1.5 py-px text-[10px] font-medium uppercase " +
          (fuzzy
            ? "bg-amber-50 text-amber-700"
            : "bg-neutral-100 text-neutral-600")
        }
      >
        {fuzzy ? "fuzzy" : "exact"}
      </span>
      <span className="font-mono text-[13px]">
        {c.field} {fuzzy ? "matches" : "="}{" "}
        <span className="text-neutral-500">&ldquo;{c.value}&rdquo;</span>
      </span>
    </li>
  );
}

function NodeCard({ node }: { node: PlanNode }) {
  switch (node.kind) {
    case "limit":
      return (
        <Card title="Limit">
          <span className="font-medium">
            {node.count} {node.count === 1 ? "row" : "rows"}
          </span>
        </Card>
      );
    case "select":
      return (
        <Card title="Select">
          <span className="font-mono text-[13px]">
            {node.columns.join(", ")}
          </span>
        </Card>
      );
    case "filter":
      return (
        <Card title={node.op === "AND" ? "Filter · all of" : "Filter · any of"}>
          <ul className="space-y-1">
            {node.conditions.map((c, i) => (
              <ConditionRow key={i} c={c} />
            ))}
          </ul>
        </Card>
      );
    case "join":
      return (
        <Card title="Join">
          <span className="whitespace-nowrap font-mono text-[13px]">
            {node.left} = {node.right}
          </span>
        </Card>
      );
    case "table":
      return (
        <Card title="Table" emphasis>
          <span className="font-mono text-[13px]">{node.name}</span>
        </Card>
      );
  }
}

function PlanTree({ node }: { node: PlanNode }) {
  const kids = childrenOf(node);
  return (
    <li className="plan-node">
      <NodeCard node={node} />
      {kids.length > 0 && (
        <ul className="plan-children">
          {kids.map((k, i) => (
            <PlanTree key={i} node={k} />
          ))}
        </ul>
      )}
    </li>
  );
}

export default function QueryPlan({ query }: { query: BqlQuery }) {
  const plan = buildPlan(query);
  return (
    <div className="overflow-x-auto py-2">
      <ul className="plan-root">
        <PlanTree node={plan} />
      </ul>
    </div>
  );
}
