import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { promisify } from "node:util";

export const runtime = "nodejs";
export const maxDuration = 300;

const execFileAsync = promisify(execFile);
const ALLOWED_SCHEMAS = new Set(["courtlistener", "dataform"]);
const MAX_QUESTION_LENGTH = 4_000;

function repositoryRoot(): string {
  const configured = process.env.SPARK_HACKS_ROOT;
  const candidates = [
    configured,
    process.cwd(),
    path.resolve(process.cwd(), ".."),
  ].filter((candidate): candidate is string => Boolean(candidate));

  const root = candidates.find((candidate) =>
    existsSync(path.join(candidate, "query_language", "cli.py")),
  );
  if (!root) throw new Error("spark-hacks repository root could not be located");
  return root;
}

function parseCompilerJson(stdout: string): unknown {
  const start = stdout.indexOf("{");
  const end = stdout.lastIndexOf("}");
  if (start < 0 || end < start) throw new Error("compiler returned no JSON payload");
  return JSON.parse(stdout.slice(start, end + 1));
}

export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return Response.json({ message: "request body must be JSON" }, { status: 400 });
  }

  const question =
    typeof body === "object" && body !== null && "question" in body
      ? (body as { question?: unknown }).question
      : undefined;
  if (typeof question !== "string" || !question.trim()) {
    return Response.json({ message: "question must be a non-empty string" }, { status: 400 });
  }
  if (question.length > MAX_QUESTION_LENGTH) {
    return Response.json(
      { message: `question must be at most ${MAX_QUESTION_LENGTH} characters` },
      { status: 413 },
    );
  }

  const requestedSchema =
    typeof body === "object" && body !== null && "schema" in body
      ? (body as { schema?: unknown }).schema
      : undefined;
  const schema =
    typeof requestedSchema === "string"
      ? requestedSchema
      : (process.env.AMICUS_SCHEMA ?? "dataform");
  if (!ALLOWED_SCHEMAS.has(schema)) {
    return Response.json({ message: `unsupported schema ${JSON.stringify(schema)}` }, { status: 400 });
  }

  try {
    const root = repositoryRoot();
    const python = process.env.PYTHON_BIN ?? "python3";
    const timeout = Number(process.env.BQL_COMPILE_TIMEOUT_MS ?? 300_000);
    const { stdout } = await execFileAsync(
      python,
      [
        "-m",
        "query_language.cli",
        "--schema",
        schema,
        "compile",
        question.trim(),
        "--json",
      ],
      {
        cwd: root,
        env: process.env,
        timeout: Number.isFinite(timeout) ? timeout : 300_000,
        maxBuffer: 2 * 1024 * 1024,
      },
    );
    return Response.json(parseCompilerJson(stdout));
  } catch (reason) {
    const failure = reason as { stdout?: string; code?: number | string };
    if (failure.stdout) {
      try {
        const payload = parseCompilerJson(failure.stdout);
        return Response.json(payload, { status: 422 });
      } catch {
        // Fall through to a deliberately non-sensitive operational error.
      }
    }
    console.error("BQL compiler route failed", reason);
    return Response.json(
      {
        message:
          failure.code === "ETIMEDOUT"
            ? "the compiler timed out"
            : "the compiler is unavailable; check Python and model configuration",
      },
      { status: 502 },
    );
  }
}
