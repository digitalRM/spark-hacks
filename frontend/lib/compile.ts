import type { BqlQuery } from "@/lib/bql";
import { DUMMY_BQL_AST } from "@/lib/dummyBql";

/**
 * natural language -> bql. if `NEXT_PUBLIC_COMPILER_URL` is set, posts `{ question }` to
 * `${NEXT_PUBLIC_COMPILER_URL}/compile` and expects the json from `query_language/serialize.py`
 * (see `lib/bql.ts`). otherwise returns the dummy ast after a short delay so the ui flow still works.
 */
export async function compileQuery(
  question: string,
  signal?: AbortSignal,
): Promise<BqlQuery> {
  const base = process.env.NEXT_PUBLIC_COMPILER_URL;
  if (!base) {
    await new Promise((r) => setTimeout(r, 600));
    return DUMMY_BQL_AST;
  }

  const res = await fetch(`${base.replace(/\/$/, "")}/compile`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });
  if (!res.ok) {
    throw new Error(`Compiler returned ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as BqlQuery;
}
