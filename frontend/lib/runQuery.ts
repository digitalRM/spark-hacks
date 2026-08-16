import type { BqlQuery } from "@/lib/bql";
import type { RunResponse } from "@/lib/results";
import { DUMMY_RESULTS } from "@/lib/dummyResults";

/**
 * Execute a compiled query on the runtime.
 *
 * Set `AMICUS_RUNTIME_URL` in the repo-root `.env` (exported here as
 * `NEXT_PUBLIC_AMICUS_RUNTIME_URL` by `scripts/web.sh`) and this POSTs `{ query }` —
 * the BQL wire JSON — to `${url}/run`, expecting a `RunResponse` whose file entries
 * are URLs served by the DGX Spark. Unset, it waits `fallbackMs` and returns the
 * bundled fixtures, because `runtime/executor.py` is still a stub.
 */
export async function runQuery(
  query: BqlQuery,
  { signal, fallbackMs = 10_000 }: { signal?: AbortSignal; fallbackMs?: number } = {},
): Promise<RunResponse> {
  const base = process.env.NEXT_PUBLIC_AMICUS_RUNTIME_URL;
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
