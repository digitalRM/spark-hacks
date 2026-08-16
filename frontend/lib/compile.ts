import type { BqlQuery } from "@/lib/bql";

export type CompileEnvelope = {
  bql_version: string;
  schema: string;
  question: string;
  query: BqlQuery;
  bql: string;
  warnings?: string[];
};

type CompileFailure = {
  message?: string;
};

/** Natural language -> validated JSON AST -> deterministic BQL, via our server route. */
export async function compileQuery(
  question: string,
  signal?: AbortSignal,
): Promise<CompileEnvelope> {
  const response = await fetch("/api/compile", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });
  const payload = (await response.json()) as CompileEnvelope | CompileFailure;
  if (
    !response.ok ||
    !("query" in payload) ||
    payload.query?.kind !== "Query" ||
    typeof payload.bql !== "string"
  ) {
    throw new Error(
      "message" in payload && payload.message
        ? payload.message
        : `Compiler returned ${response.status} ${response.statusText}`,
    );
  }
  return payload;
}
