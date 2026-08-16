import type { RunResponse } from "@/lib/results";
import ResultCard from "@/components/ResultCard";

/** The list of matched cases with a small summary line on top. */
export default function ResultsList({ response }: { response: RunResponse }) {
  const { results, total, tookMs } = response;
  const n = total ?? results.length;

  return (
    <div className="flex flex-col gap-3 p-4">
      <p className="text-xs text-neutral-500">
        <span className="font-medium text-neutral-700">
          {n} {n === 1 ? "case" : "cases"}
        </span>
        {results.length < n && ` · showing ${results.length}`}
        {tookMs != null && ` · ${(tookMs / 1000).toFixed(1)}s`}
      </p>
      {results.length === 0 ? (
        <p className="rounded-xl border border-neutral-200 py-10 text-center text-sm text-neutral-400">
          No cases matched. Try loosening a condition.
        </p>
      ) : (
        results.map((r, i) => (
          <div
            key={r.id}
            className="animate-pop-in"
            style={{ animationDelay: `${i * 90}ms` }}
          >
            <ResultCard result={r} index={i} />
          </div>
        ))
      )}
    </div>
  );
}
