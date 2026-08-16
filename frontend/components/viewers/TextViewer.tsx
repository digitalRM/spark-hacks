"use client";

import { useEffect, useState } from "react";
import type { ResultFile } from "@/lib/results";

const PREVIEW_CHARS = 900;

/**
 * Fetches a text file and shows it in a readable block: collapsed to an
 * excerpt by default, expandable to the full document. Highlights the
 * runtime's snippet if it occurs in the text.
 */
export default function TextViewer({ file }: { file: ResultFile }) {
  // Keyed by URL so a new file starts from a clean loading state without
  // synchronous resets inside the effect.
  const [state, setState] = useState<{
    url: string;
    text: string | null;
    error: string | null;
  }>({ url: file.url, text: null, error: null });
  const [expanded, setExpanded] = useState(false);
  const text = state.url === file.url ? state.text : null;
  const error = state.url === file.url ? state.error : null;

  useEffect(() => {
    const ctrl = new AbortController();
    const url = file.url;
    fetch(url, { signal: ctrl.signal })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((t) => setState({ url, text: t, error: null }))
      .catch((e) => {
        if (e?.name !== "AbortError")
          setState({ url, text: null, error: "Couldn't load this text." });
      });
    return () => ctrl.abort();
  }, [file.url]);

  if (error) {
    return (
      <p className="text-sm text-neutral-500">
        {error}{" "}
        <a href={file.url} target="_blank" rel="noreferrer" className="underline">
          Open directly
        </a>
      </p>
    );
  }
  if (text === null) {
    return <p className="py-6 text-center text-sm text-neutral-400">Loading…</p>;
  }

  const long = text.length > PREVIEW_CHARS;
  const shown = expanded || !long ? text : text.slice(0, PREVIEW_CHARS) + "…";
  const parts = splitOnSnippet(shown, file.snippet);

  return (
    <div className="flex flex-col gap-2">
      <pre className="max-h-[520px] overflow-auto whitespace-pre-wrap rounded-xl border border-neutral-200 bg-neutral-50 p-4 font-mono text-[12.5px] leading-relaxed text-neutral-800">
        {parts.map((p, i) =>
          p.hit ? (
            <mark key={i} className="rounded bg-violet-100 px-0.5 text-violet-900">
              {p.text}
            </mark>
          ) : (
            <span key={i}>{p.text}</span>
          ),
        )}
      </pre>
      <div className="flex items-center gap-4 text-xs text-neutral-500">
        {long && (
          <button
            type="button"
            onClick={() => setExpanded((e) => !e)}
            className="font-medium text-neutral-700 hover:underline"
          >
            {expanded ? "Show less" : "Show full text"}
          </button>
        )}
        <a
          href={file.url}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-neutral-700 hover:underline"
        >
          Open in new tab ↗
        </a>
      </div>
    </div>
  );
}

function splitOnSnippet(text: string, snippet?: string) {
  if (!snippet) return [{ text, hit: false }];
  const i = text.indexOf(snippet);
  if (i < 0) return [{ text, hit: false }];
  return [
    { text: text.slice(0, i), hit: false },
    { text: snippet, hit: true },
    { text: text.slice(i + snippet.length), hit: false },
  ];
}
