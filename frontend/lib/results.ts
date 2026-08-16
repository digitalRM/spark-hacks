/**
 * Search results as delivered by the runtime (`runtime/executor.py`), plus the
 * view model the result cards render. Files live on the DGX Spark / CourtListener
 * and are referenced by URL only — the frontend never receives bytes, it just
 * points the right viewer at the link.
 */

// ---- wire: what `runtime.executor.execute(plan)` returns ---------------------

/** One citable piece of evidence a semantic filter attached to a survivor. */
export type ExecEvidence = {
  /** e.g. "summary", "page 12", "oral argument" */
  label: string | null;
  url: string | null;
  /** The model's one-sentence rationale for the match. */
  quote: string;
  confidence: number | null;
};

export type ExecResult = {
  id: string;
  title: string;
  /** Primary link: envelope.source_url → CourtListener cluster page → source PDF. */
  url: string | null;
  /** Projected columns, keyed by the label from the plan's Project node
   *  (e.g. "doc.summary"). Absent when the plan had no Project root. */
  columns?: Record<string, unknown>;
  evidence: ExecEvidence[];
};

export type ExecFunnelStage = {
  node_id: string;
  rows_in: number;
  rows_out: number;
  model_calls: number;
  seconds: number;
  provenance: string;
  remote_calls: number;
  cache_hits: number;
  degraded: boolean;
};

export type ExecResponse = {
  total: number;
  results: ExecResult[];
  funnel: {
    provenance: string;
    seconds: number;
    model_calls: number;
    remote_calls: number;
    rows_in: number;
    rows_out: number;
    stages: ExecFunnelStage[];
  };
  provenance: string;
};

// ---- view model: what the cards render ------------------------------------

export type FileKind = "audio" | "image" | "pdf" | "text" | "link";

export type ResultFile = {
  url: string;
  /** Optional; inferred from `mime` / the URL extension when omitted. */
  kind?: FileKind;
  mime?: string;
  /** Human title; falls back to the filename. */
  title?: string;
  /** Which schema field / evidence label this came from. */
  field?: string;
  /** Excerpt / rationale to show alongside. */
  snippet?: string;
  /** Audio: seconds where the match occurs. */
  timestamp?: number;
  /** PDF/image: page of interest. */
  page?: number;
  size?: number;
};

export type SearchResult = {
  id: string;
  title: string;
  subtitle?: string;
  /** Primary link for the whole record. */
  url?: string;
  /** 0..1, best evidence confidence. */
  score?: number;
  /** Plain-English reasons this matched (the model rationales). */
  highlights?: string[];
  /** Projected fields worth showing as text (e.g. the summary). */
  fields?: { label: string; value: string }[];
  files: ResultFile[];
};

export type RunResponse = {
  results: SearchResult[];
  total?: number;
  tookMs?: number;
  /** Per-operator funnel from the executor, for the details view. */
  funnel?: ExecResponse["funnel"];
};

// ---- executor → view model ---------------------------------------------------

/** Turn one executor row into what a card renders. Pure; safe on partial data. */
export function toSearchResult(r: ExecResult): SearchResult {
  const evidence = Array.isArray(r.evidence) ? r.evidence : [];
  const highlights = evidence
    .map((e) => (e.quote ?? "").trim())
    .filter((q, i, arr) => q && arr.indexOf(q) === i);
  const confidences = evidence
    .map((e) => e.confidence)
    .filter((c): c is number => typeof c === "number");
  const score = confidences.length ? Math.max(...confidences) : undefined;

  // Files: the primary link, plus every evidence link that points somewhere
  // different (dedupe by URL, keep the first label/quote seen).
  const files: ResultFile[] = [];
  const seen = new Set<string>();
  const push = (f: ResultFile) => {
    if (!f.url || seen.has(f.url)) return;
    seen.add(f.url);
    files.push(f);
  };
  if (r.url) push({ url: r.url, title: sourceTitle(r.url), field: "source" });
  for (const e of evidence) {
    if (!e.url) continue;
    const page = pageFromUrl(e.url);
    push({
      url: e.url,
      title: e.label ? capitalize(e.label) : sourceTitle(e.url),
      field: e.label ?? undefined,
      snippet: e.quote || undefined,
      page: page ?? undefined,
    });
  }

  // Projected columns worth reading: strings that aren't just the id/title.
  // Summaries come back as HTML fragments (<p>…</p>) — show them as text.
  const fields: { label: string; value: string }[] = [];
  for (const [label, value] of Object.entries(r.columns ?? {})) {
    if (typeof value !== "string" || !value.trim()) continue;
    if (value === r.id || value === r.title) continue;
    const text = stripHtml(value);
    if (!text) continue;
    fields.push({ label: humanColumn(label), value: text });
  }

  const subtitleBits: string[] = [];
  const dateish = Object.entries(r.columns ?? {}).find(
    ([k, v]) => /date/i.test(k) && typeof v === "string" && v,
  );
  if (dateish) subtitleBits.push(String(dateish[1]));
  const court = Object.entries(r.columns ?? {}).find(
    ([k, v]) => /court/i.test(k) && typeof v === "string" && v,
  );
  if (court) subtitleBits.push(String(court[1]));
  if (!subtitleBits.length && r.url) subtitleBits.push(hostOf(r.url));

  return {
    id: r.id,
    title: r.title || "(untitled)",
    subtitle: subtitleBits.join(" · ") || undefined,
    url: r.url ?? undefined,
    score,
    highlights,
    fields,
    files,
  };
}

export function toRunResponse(exec: ExecResponse): RunResponse {
  const results = (exec.results ?? []).map(toSearchResult);
  return {
    results,
    total: exec.total ?? results.length,
    tookMs: exec.funnel ? Math.round(exec.funnel.seconds * 1000) : undefined,
    funnel: exec.funnel,
  };
}

// ---- helpers -----------------------------------------------------------------

/** "doc.summary" → "Summary", "doc.envelope.id" → "Id" */
export function humanColumn(label: string): string {
  const last = label.split(".").pop() ?? label;
  return capitalize(last.replace(/_/g, " "));
}

/** Drop tags, decode the common entities, collapse whitespace. */
export function stripHtml(html: string): string {
  return html
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>\s*<p[^>]*>/gi, "\n\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function capitalize(s: string) {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function sourceTitle(url: string): string {
  const host = hostOf(url);
  if (host === "courtlistener.com") return "CourtListener";
  return host;
}

/** `…#page=12` or `…?page=12` → 12 */
export function pageFromUrl(url: string): number | null {
  const m = /[#?&]page=(\d+)/i.exec(url);
  return m ? Number(m[1]) : null;
}

// ---- kind inference ---------------------------------------------------------

const EXT_KIND: Record<string, FileKind> = {
  mp3: "audio",
  wav: "audio",
  m4a: "audio",
  ogg: "audio",
  oga: "audio",
  flac: "audio",
  aac: "audio",
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  avif: "image",
  bmp: "image",
  tif: "image",
  tiff: "image",
  svg: "image",
  pdf: "pdf",
  txt: "text",
  md: "text",
  json: "text",
  csv: "text",
  xml: "text",
};

/** Best-effort file kind from explicit field, MIME type, then URL extension.
 *  Anything else (an HTML page like a CourtListener opinion) is a `link`. */
export function fileKind(f: ResultFile): FileKind {
  if (f.kind) return f.kind;
  const mime = f.mime?.toLowerCase() ?? "";
  if (mime.startsWith("audio/")) return "audio";
  if (mime.startsWith("image/")) return "image";
  if (mime === "application/pdf") return "pdf";
  if (mime.startsWith("text/plain") || mime === "application/json") return "text";
  const ext = extensionOf(f.url);
  return (ext && EXT_KIND[ext]) || "link";
}

export function extensionOf(url: string): string | null {
  try {
    const path = new URL(url, "http://x").pathname;
    const m = /\.([a-z0-9]+)$/i.exec(path);
    return m ? m[1].toLowerCase() : null;
  } catch {
    return null;
  }
}

export function fileName(url: string): string {
  try {
    const path = new URL(url, "http://x").pathname;
    return decodeURIComponent(path.split("/").pop() || url);
  } catch {
    return url;
  }
}

export function fileTitle(f: ResultFile): string {
  return f.title ?? fileName(f.url);
}

export function formatBytes(n?: number): string | null {
  if (n == null) return null;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatTime(sec: number): string {
  if (!Number.isFinite(sec)) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}
