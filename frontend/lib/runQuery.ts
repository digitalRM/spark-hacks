import { API_URL, type Stage } from "@/lib/compile";
import { toRunResponse, type ExecResponse, type RunResponse } from "@/lib/results";

type ExecuteEnvelope = {
  ok: boolean;
  stage: Stage;
  results: ExecResponse | null;
  message?: string;
};

/**
 * Run an optimized plan (the `plan` field of a /compile envelope) on the
 * runtime: `POST {API_URL}/execute { plan }`. Resolves to the card view model.
 * Throws with the API's `message` on 422 / failure.
 */
export async function runQuery(
  plan: unknown,
  { signal }: { signal?: AbortSignal } = {},
): Promise<RunResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/execute`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ plan }),
      signal,
    });
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "AbortError") throw reason;
    throw new Error(`The Amicus API is unreachable at ${API_URL}.`);
  }

  const payload = (await res.json().catch(() => null)) as ExecuteEnvelope | null;
  if (!res.ok || !payload?.ok || !payload.results) {
    throw new Error(
      payload?.message ?? `Executor returned ${res.status} ${res.statusText}`,
    );
  }
  return toRunResponse(payload.results);
}
