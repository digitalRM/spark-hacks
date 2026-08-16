/**
 * Search results as delivered by the runtime. Files live on the DGX Spark and
 * are referenced by URL only — the frontend never receives bytes directly, it
 * just points the right viewer at the link.
 */

export type FileKind = "audio" | "image" | "pdf" | "text";

export type ResultFile = {
  /** Absolute (or same-origin) URL the DGX serves the file from. */
  url: string;
  /** Optional; inferred from `mime` / the URL extension when omitted. */
  kind?: FileKind;
  mime?: string;
  /** Human title, e.g. "Oral argument (Mar 4, 2021)". Falls back to the filename. */
  title?: string;
  /** Which schema field this file came from, e.g. "docket.argument". */
  field?: string;
  /** Short excerpt / caption the runtime already extracted (shown before the file loads). */
  snippet?: string;
  /** For audio: seconds where the match occurs, so the player can jump there. */
  timestamp?: number;
  /** For pdf/image: page of interest. */
  page?: number;
  /** Bytes, if known. */
  size?: number;
};

export type SearchResult = {
  id: string;
  /** e.g. the case name. */
  title: string;
  /** e.g. "9th Cir. · Mar 4, 2021 · No. 19-15566". */
  subtitle?: string;
  /** 0..1 relevance, if the runtime scores. */
  score?: number;
  /** Plain-English reasons this matched (one per satisfied criterion). */
  highlights?: string[];
  files: ResultFile[];
};

export type RunResponse = {
  results: SearchResult[];
  /** Total available if the runtime paginates. */
  total?: number;
  /** Runtime timing, ms. */
  tookMs?: number;
};

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
  html: "text",
};

/** Best-effort file kind from explicit field, MIME type, then URL extension. */
export function fileKind(f: ResultFile): FileKind | "unknown" {
  if (f.kind) return f.kind;
  const mime = f.mime?.toLowerCase() ?? "";
  if (mime.startsWith("audio/")) return "audio";
  if (mime.startsWith("image/")) return "image";
  if (mime === "application/pdf") return "pdf";
  if (mime.startsWith("text/") || mime === "application/json") return "text";
  const ext = extensionOf(f.url);
  return (ext && EXT_KIND[ext]) || "unknown";
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
