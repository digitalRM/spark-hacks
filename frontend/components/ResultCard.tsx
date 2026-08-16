"use client";

import { useState } from "react";
import { fileKind, fileTitle, type SearchResult } from "@/lib/results";
import { fieldLabel } from "@/lib/humanize";
import FileViewer, { KIND_LABEL, KindIcon } from "@/components/viewers/FileViewer";
import AutoHeight from "@/components/AutoHeight";

/**
 * One matched case: title, meta line, why-it-matched highlights, then a row of
 * file tiles. Clicking a tile opens that file's viewer inline beneath the row.
 */
export default function ResultCard({
  result,
  index,
}: {
  result: SearchResult;
  index: number;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const openFile = open != null ? result.files[open] : null;

  return (
    <article className="rounded-2xl border border-neutral-200 bg-white p-4">
      <header className="flex items-start gap-3">
        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-neutral-100 text-xs font-medium text-neutral-500">
          {index + 1}
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-[15px] font-semibold leading-snug tracking-tight">
            {result.title}
          </h3>
          {result.subtitle && (
            <p className="mt-0.5 text-xs text-neutral-500">{result.subtitle}</p>
          )}
        </div>
        {result.score != null && (
          <span
            title="Relevance"
            className="shrink-0 rounded-md bg-neutral-100 px-2 py-0.5 font-mono text-[11px] tabular-nums text-neutral-600"
          >
            {Math.round(result.score * 100)}%
          </span>
        )}
      </header>

      {result.highlights && result.highlights.length > 0 && (
        <ul className="mt-3 flex flex-col gap-1 pl-9 text-[13px] leading-relaxed text-neutral-700">
          {result.highlights.map((h, i) => (
            <li key={i} className="flex gap-2">
              <span className="mt-[9px] h-1 w-1 shrink-0 rounded-full bg-violet-400" />
              <span>{h}</span>
            </li>
          ))}
        </ul>
      )}

      {result.files.length > 0 && (
        <div className="mt-4 pl-9">
          {/* file tiles */}
          <div className="flex flex-wrap gap-2">
            {result.files.map((f, i) => {
              const kind = fileKind(f);
              const active = open === i;
              return (
                <button
                  key={i}
                  type="button"
                  onClick={() => setOpen(active ? null : i)}
                  aria-expanded={active}
                  className={
                    "flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-left text-xs transition-colors " +
                    (active
                      ? "border-neutral-900 bg-neutral-900 text-white"
                      : "border-neutral-200 bg-white text-neutral-700 hover:border-neutral-400")
                  }
                >
                  <KindIcon
                    kind={kind}
                    className={active ? "text-white" : "text-neutral-500"}
                  />
                  <span className="max-w-[220px] truncate font-medium">
                    {fileTitle(f)}
                  </span>
                  <span
                    className={
                      "rounded px-1 text-[10px] uppercase tracking-wide " +
                      (active ? "bg-white/15" : "bg-neutral-100 text-neutral-500")
                    }
                  >
                    {KIND_LABEL[kind]}
                  </span>
                </button>
              );
            })}
          </div>

          {/* inline viewer */}
          <AutoHeight durationMs={400}>
            {openFile && (
              <div key={open} className="animate-fade-in mt-3">
                <div className="mb-2 flex items-center justify-between text-xs text-neutral-500">
                  <span>
                    {fileTitle(openFile)}
                    {openFile.field && (
                      <>
                        {" "}
                        · <span className="text-neutral-400">{fieldLabel(openFile.field)}</span>
                      </>
                    )}
                  </span>
                  <button
                    type="button"
                    onClick={() => setOpen(null)}
                    className="rounded-md px-1.5 py-0.5 hover:bg-neutral-100"
                  >
                    Close
                  </button>
                </div>
                <FileViewer file={openFile} />
              </div>
            )}
          </AutoHeight>
        </div>
      )}
    </article>
  );
}
