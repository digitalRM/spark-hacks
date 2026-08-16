import type { BqlQuery } from "@/lib/bql";

/**
 * Where the Python API lives. Set once in the repo-root `.env` as `AMICUS_API_URL`;
 * `scripts/web.sh` exports it into this process as `NEXT_PUBLIC_AMICUS_API_URL`, which
 * Next inlines into the browser bundle. Nothing secret ever passes through here.
 */
const API_URL = (
  process.env.NEXT_PUBLIC_AMICUS_API_URL ?? "http://127.0.0.1:8000"
).replace(/\/$/, "");

/** One stage of the pipeline. `stub` means the module behind that stage is not written yet. */
export type Stage = {
  name: "relevance" | "answer" | "compile" | "typecheck" | "optimize" | "execute";
  status: "ok" | "failed" | "stub" | "skipped";
  ms: number;
  message?: string;
  errors?: { path: string; code: string; message: string }[];
  detail?: Record<string, unknown>;
};

export type CompiledSearchEnvelope = {
  mode: "compile";
  ok: boolean;
  bql_version: string;
  schema: string;
  is_legal: true;
  question: string;
  query: BqlQuery;
  bql: string;
  stages: Stage[];
  plan: unknown | null;
  results: unknown | null;
  warnings?: string[];
};

/** The router sent this straight to Super: a legal question BQL cannot express. */
export type LegalAnswerEnvelope = {
  mode: "answer";
  ok: boolean;
  is_legal: true;
  question: string;
  answer: string;
  model: string;
  stages: Stage[];
};

export type CompileEnvelope = CompiledSearchEnvelope | LegalAnswerEnvelope;

type CompileFailure = { message?: string };

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
  const value = payload as { mode?: unknown; query?: { kind?: unknown }; bql?: unknown };
  return (
    value.mode === "compile" &&
    value.query?.kind === "Query" &&
    typeof value.bql === "string"
  );
}

/**
 * Natural language -> routed -> either a validated JSON AST rendered as BQL, or a
 * direct legal answer from Super.
 *
 * Talks to `api/server.py` directly; there is no Next route in between, so the API
 * must be running (`scripts/api.sh`) and its CORS origins must include this one.
 * A rejected or failed request comes back as 422 with a `message`.
 */
export async function compileQuery(
  question: string,
  signal?: AbortSignal,
): Promise<CompileEnvelope> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/compile`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
      signal,
    });
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "AbortError") throw reason;
    throw new Error(
      `The Amicus API is unreachable at ${API_URL}. Start it with 'python -m api.server'.`,
    );
  }

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
