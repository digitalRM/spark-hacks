import type { BqlQuery } from "@/lib/bql";

export type CompiledSearchEnvelope = {
  mode: "compile";
  bql_version: string;
  schema: string;
  is_legal: true;
  question: string;
  query: BqlQuery;
  bql: string;
  warnings?: string[];
};

export type LegalAnswerEnvelope = {
  mode: "answer";
  is_legal: true;
  question: string;
  answer: string;
  model: string;
};

export type CompileEnvelope = CompiledSearchEnvelope | LegalAnswerEnvelope;

type CompileFailure = {
  message?: string;
};

function isLegalAnswer(payload: unknown): payload is LegalAnswerEnvelope {
  return (
    typeof payload === "object" &&
    payload !== null &&
    (payload as { mode?: unknown }).mode === "answer" &&
    typeof (payload as { answer?: unknown }).answer === "string"
  );
}

function isCompiledSearch(payload: unknown): payload is CompiledSearchEnvelope {
  if (typeof payload !== "object" || payload === null) return false;
  const value = payload as {
    mode?: unknown;
    query?: { kind?: unknown };
    bql?: unknown;
  };
  return (
    value.mode === "compile" &&
    value.query?.kind === "Query" &&
    typeof value.bql === "string"
  );
}

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
  const payload = (await response.json()) as unknown;
  if (!response.ok) {
    const failure = payload as CompileFailure;
    throw new Error(
      failure && typeof failure.message === "string"
        ? failure.message
        : `Compiler returned ${response.status} ${response.statusText}`,
    );
  }
  if (isLegalAnswer(payload) || isCompiledSearch(payload)) return payload;
  throw new Error("Compiler returned an invalid response");
}
