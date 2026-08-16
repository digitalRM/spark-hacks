/** Anything `JSON.parse` can return — what this tree renders. */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

type NodeProps = {
  label: string;
  value: JsonValue;
  isRoot?: boolean;
};

function isBranch(v: JsonValue): v is JsonValue[] | { [k: string]: JsonValue } {
  return typeof v === "object" && v !== null;
}

function entriesOf(v: JsonValue[] | { [k: string]: JsonValue }) {
  return Array.isArray(v)
    ? v.map((item, i) => [String(i), item] as const)
    : Object.entries(v);
}

function Leaf({ label, value }: { label: string; value: JsonValue }) {
  const text = typeof value === "string" ? `"${value}"` : String(value);
  return (
    <div className="inline-flex items-baseline gap-2 whitespace-nowrap rounded-lg border border-neutral-200 bg-white px-3 py-1.5 text-sm">
      <span className="font-medium text-neutral-500">{label}</span>
      <span className="font-mono text-neutral-900">{text}</span>
    </div>
  );
}

function TreeNode({ label, value, isRoot }: NodeProps) {
  if (!isBranch(value)) {
    return <Leaf label={label} value={value} />;
  }

  const children = entriesOf(value);
  const kind = Array.isArray(value) ? `[${children.length}]` : "{ }";

  return (
    <div className="flex items-center">
      <div
        className={
          "shrink-0 rounded-lg border px-3 py-1.5 text-sm font-medium " +
          (isRoot
            ? "border-neutral-900 bg-neutral-900 text-white"
            : "border-neutral-300 bg-neutral-50 text-neutral-900")
        }
      >
        {label}
        <span className="ml-1.5 font-mono text-xs font-normal opacity-50">
          {kind}
        </span>
      </div>

      {children.length > 0 && (
        <ul className="tree-children">
          {children.map(([k, v]) => (
            <li key={k} className="tree-child">
              <TreeNode label={k} value={v} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function JsonTree({ data }: { data: JsonValue }) {
  return (
    <div className="overflow-x-auto py-2">
      <TreeNode label="query" value={data} isRoot />
    </div>
  );
}
