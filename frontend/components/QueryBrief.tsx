import type { ReactNode } from "react";
import type { BqlQuery } from "@/lib/bql";
import {
  TABLE_LABELS,
  inSentence,
  joinList,
  resultNoun,
  selectedLabels,
  tablesUsed,
  topLevelCriteria,
  type Criterion,
} from "@/lib/humanize";

function MatchChip({ exact }: { exact: boolean }) {
  return exact ? (
    <span
      title="Must match this exactly."
      className="shrink-0 rounded-md bg-neutral-100 px-2 py-0.5 text-[11px] font-medium text-neutral-600"
    >
      Exact
    </span>
  ) : (
    <span
      title="Matched on meaning — the wording in the document can differ."
      className="shrink-0 rounded-md bg-violet-50 px-2 py-0.5 text-[11px] font-medium text-violet-700"
    >
      By meaning
    </span>
  );
}

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <h3 className="text-[13px] font-semibold text-neutral-400">{children}</h3>
  );
}

/** one line of plain english for a leaf criterion */
function LeafText({ c }: { c: Extract<Criterion, { kind: "leaf" }> }) {
  return (
    <>
      <span className="text-neutral-500">{c.label}</span>
      {c.connector && (
        <>
          {" "}
          <span className={c.exact ? "text-neutral-400" : ""}>
            {c.connector}
          </span>
        </>
      )}{" "}
      <span className="font-medium">
        {c.quote ? <>&ldquo;{c.value}&rdquo;</> : c.value}
      </span>
    </>
  );
}

/**
 * renders any criterion. leaves = sentence + chip, groups = nested "all of / any of" list, negations get a "not" chip,
 * unknown shapes fall back to raw json so nothing ever fails to render
 */
function CriterionBody({ c, negated = false }: { c: Criterion; negated?: boolean }) {
  const notChip = negated && (
    <span className="mr-2 rounded-md bg-rose-50 px-1.5 py-0.5 text-[11px] font-medium text-rose-700">
      Not
    </span>
  );

  switch (c.kind) {
    case "leaf":
      return (
        <div className="flex flex-1 items-start gap-3">
          <p className="flex-1 text-[15px] leading-relaxed">
            {notChip}
            <LeafText c={c} />
          </p>
          <MatchChip exact={c.exact} />
        </div>
      );

    case "not":
      // "not" on a leaf reads inline, on a group it labels the group
      return <CriterionBody c={c.child} negated={!negated} />;

    case "group":
      return (
        <div className="flex flex-1 flex-col gap-2">
          <p className="text-[13px] text-neutral-500">
            {notChip}
            {negated
              ? c.op === "all"
                ? "all of the following are true:"
                : "any of the following is true:"
              : c.op === "all"
                ? "all of the following:"
                : "any of the following:"}
          </p>
          <ul className="flex flex-col divide-y divide-neutral-100 rounded-lg border border-neutral-200 bg-neutral-50/50 mb-2">
            {c.children.map((child, i) => (
              <li key={i} className="flex items-start gap-3 px-3 py-2">
                <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-neutral-300" />
                <CriterionBody c={child} />
              </li>
            ))}
          </ul>
        </div>
      );

    case "unknown":
      return (
        <div className="flex flex-1 items-start gap-3">
          <p className="flex-1 text-[13px] leading-relaxed">
            {notChip}
            <span className="text-neutral-500">Condition </span>
            <code className="rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[12px] text-neutral-700">
              {c.raw}
            </code>
          </p>
          <span className="shrink-0 rounded-md bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700">
            Unrecognized
          </span>
        </div>
      );
  }
}

/**
 * plain-english summary of a compiled query for someone who dosnt care about joins or asts:
 * what comes back, what each result must satisfy, how strictly each condition is matched
 */
export default function QueryBrief({ query }: { query: BqlQuery }) {
  const { op, items } = topLevelCriteria(query);
  const columns = selectedLabels(query);
  const noun = resultNoun(query);
  const hasFuzzy = JSON.stringify(query.where ?? {}).includes('"Fuzzy"');

  return (
    <div className="flex flex-col gap-6">
      {/* what comes back */}
      <section className="-mt-2 flex flex-col gap-1.5">
        <SectionTitle>Results</SectionTitle>
        <p className="text-[15px] leading-relaxed">
          {query.limit != null ? (
            <>
              Up to{" "}
              <span className="font-medium">
                {query.limit} {noun}
              </span>
            </>
          ) : (
            <>
              <span className="font-medium">All matching {noun}</span>
            </>
          )}
          {columns.length > 0 && (
            <>
              , showing{" "}
              <span className="text-neutral-600">
                {joinList(columns.map(inSentence), "and")}
              </span>
            </>
          )}
          .
        </p>
      </section>

      {/* criteria */}
      <section className="flex flex-col gap-2 -mt-3">
        <SectionTitle>
          {items.length === 0
            ? "No conditions. Everything is returned"
            : op === "all"
              ? "Every result must meet all of these"
              : "Each result meets at least one of these"}
        </SectionTitle>
        {items.length > 0 && (
          <ol className="divide-y divide-neutral-100 rounded-2xl border border-neutral-200">
            {items.map((c, i) => (
              <li key={i} className="flex items-start gap-3 px-3 py-3">
                <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-neutral-100 text-[11px] font-medium text-neutral-500">
                  {i + 1}
                </span>
                <CriterionBody c={c} />
              </li>
            ))}
          </ol>
        )}
        {items.length > 0 && (
          <p className="px-1 text-xs leading-relaxed text-neutral-400 mt-2">
            <span className="font-medium text-neutral-500">Exact</span>{" "}
            conditions must match precisely.
            {hasFuzzy && (
              <>
                {" "}
                <span className="font-medium text-violet-600">By meaning</span>{" "}
                conditions are matched on what the text says, so the wording in
                the document can differ.
              </>
            )}
          </p>
        )}
      </section>
    </div>
  );
}

/** which sources the query pulls from. rendered below the card, not inside it */
export function SearchedAcross({
  query,
  done = false,
}: {
  query: BqlQuery;
  /** past tense once the search is finished */
  done?: boolean;
}) {
  const tables = tablesUsed(query);
  const STAGGER_MS = 220;
  return (
    <section className="flex flex-col gap-2">
      <SectionTitle>{done ? "Searched across" : "Searching across"}</SectionTitle>
      <div className="flex flex-wrap items-center gap-1.5">
        {tables.map((t, i) => (
          <span
            key={t}
            className="animate-pop-in rounded-md border border-neutral-200 bg-white px-2 py-0.5 text-xs text-neutral-700"
            style={{ animationDelay: `${i * STAGGER_MS}ms` }}
          >
            {TABLE_LABELS[t] ?? t}
          </span>
        ))}
        {tables.length > 1 && (
          <span
            className="animate-pop-in ml-1 text-xs text-neutral-400"
            style={{ animationDelay: `${tables.length * STAGGER_MS}ms` }}
          >
            linked automatically
          </span>
        )}
      </div>
    </section>
  );
}
