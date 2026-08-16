import type { BqlQuery } from "@/lib/bql";
import type { RunResponse } from "@/lib/results";
import { DUMMY_RESULTS } from "@/lib/dummyResults";

/**
 * Execute a compiled query on the runtime.
 *
 * If `NEXT_PUBLIC_RUNTIME_URL` is set, POSTs `{ query }` (the BQL wire JSON) to
 * `${NEXT_PUBLIC_RUNTIME_URL}/run` and expects a `RunResponse` whose file
 * entries are URLs served by the DGX Spark. Otherwise waits `fallbackMs` and
 * returns bundled dummy results so the UI flow can be exercised.
 */
export async function runQuery(
  query: BqlQuery,
  { signal, fallbackMs = 10_000 }: { signal?: AbortSignal; fallbackMs?: number } = {},
): Promise<RunResponse> {
  const base = process.env.NEXT_PUBLIC_RUNTIME_URL;
  if (!base) {
    await new Promise<void>((resolve, reject) => {
      const t = setTimeout(resolve, fallbackMs);
      signal?.addEventListener("abort", () => {
        clearTimeout(t);
        reject(new DOMException("Aborted", "AbortError"));
      });
    });
    return { results: DUMMY_RESULTS, total: DUMMY_RESULTS.length, tookMs: fallbackMs };
  }

  const res = await fetch(`${base.replace(/\/$/, "")}/run`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ query }),
    signal,
  });
  if (!res.ok) throw new Error(`Runtime returned ${res.status} ${res.statusText}`);
  return (await res.json()) as RunResponse;
}
