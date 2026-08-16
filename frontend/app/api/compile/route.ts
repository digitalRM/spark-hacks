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

function pythonExecutable(root: string): string {
  const configured = process.env.PYTHON_BIN?.trim();
  if (configured) {
    const looksLikePath = configured.includes("/") || configured.includes("\\");
    return path.isAbsolute(configured) || !looksLikePath
      ? configured
      : path.resolve(root, configured);
  }
  const virtualenvCandidates = [
    path.join(root, ".venv", "bin", "python"),
    path.join(root, ".venv", "Scripts", "python.exe"),
  ];
  return virtualenvCandidates.find(existsSync) ?? "python3";
}

type CompilerFailure = {
  stdout?: string;
  stderr?: string;
  code?: number | string;
  killed?: boolean;
};

function safeFailureMessage(reason: unknown, failure: CompilerFailure): string {
  const detail = [failure.stderr, reason instanceof Error ? reason.message : ""]
    .filter(Boolean)
    .join("\n");
  if (failure.code === "ENOENT" || /no such file|spawn .*enoent/i.test(detail)) {
    return "Python was not found. From the spark-hacks root, create .venv and install both requirements files.";
  }
  if (/openai python sdk|no module named ['\"]?openai/i.test(detail)) {
    return "The Python OpenAI SDK is missing. Install query_language/requirements.txt into the configured Python environment.";
  }
  if (/NVIDIA_API_KEY is not set/i.test(detail)) {
    return "NVIDIA_API_KEY is not configured on this machine. Add it to frontend/.env.local and restart the dev server.";
  }
  if (/HTTP (401|403)|authenticationerror|permissiondenied/i.test(detail)) {
    return "NVIDIA rejected the configured API key. Check or regenerate NVIDIA_API_KEY, then restart the dev server.";
  }
  if (/nemotron-3\.5-lightning|:8001/i.test(detail)) {
    return "The legal relevance checker on Spark :8001 is unavailable. Check SPARK_HOST and the Lightning server.";
  }
  if (failure.code === "ETIMEDOUT" || failure.killed) {
    return "The compiler timed out before NVIDIA returned a result.";
  }
  if (/repository root could not be located/i.test(detail)) {
    return "The spark-hacks repository root could not be located. Set SPARK_HACKS_ROOT in frontend/.env.local.";
  }
  return "The compiler is unavailable. Complete the Python and frontend/.env.local setup, then restart the dev server.";
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
    const python = pythonExecutable(root);
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
    const failure = reason as CompilerFailure;
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
      { message: safeFailureMessage(reason, failure) },
      { status: 502 },
    );
  }
}
