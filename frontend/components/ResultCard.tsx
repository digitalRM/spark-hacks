"use client";

import { useCallback, useState } from "react";
import { fileKind, fileTitle, type SearchResult } from "@/lib/results";
import { KIND_LABEL, KindIcon } from "@/components/viewers/FileViewer";
import FileModal from "@/components/viewers/FileModal";

/**
 * One matched case: title, meta line, why-it-matched highlights, then a row of
 * file tiles. Clicking a tile opens that file's viewer in a modal.
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
  const close = useCallback(() => setOpen(null), []);

  return (
    <article className="rounded-2xl bg-white">
      <header className="flex items-start gap-3 mb-3">
        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-neutral-100 text-xs font-medium text-neutral-500">
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

      <div className="border border-neutral-200 rounded-2xl p-3 px-4">
        {result.highlights && result.highlights.length > 0 && (
          <ul className=" flex flex-col gap-1 text-[13px] leading-relaxed text-neutral-700">
            {result.highlights.map((h, i) => (
              <li key={i} className="flex gap-2">
                <span className="mt-[9px] h-1 w-1 shrink-0 rounded-full bg-violet-400" />
                <span>{h}</span>
              </li>
            ))}
          </ul>
        )}

        {result.files.length > 0 && (
          <div className="mt-4 -mx-1">
            {/* file tiles */}
            <div className="flex flex-wrap gap-2">
              {result.files.map((f, i) => {
                const kind = fileKind(f);
                return (
                  <button
                    key={i}
                    type="button"
                    onClick={() => setOpen(i)}
                    aria-haspopup="dialog"
                    className="group flex items-center gap-2 rounded-[12px] border border-neutral-200 bg-white px-2.5 py-1.5 text-left text-xs text-neutral-700 transition-colors hover:border-neutral-400"
                  >
                    <div className="-ml-0.5">
                      <KindIcon kind={kind} className="text-neutral-500" />
                    </div>
                    <span className="truncate font-medium">{fileTitle(f)}</span>
                    <span className="shrink-0 rounded-md bg-neutral-100 px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide text-neutral-500">
                      {KIND_LABEL[kind]}
                    </span>
                  </button>
                );
              })}
            </div>

          </div>
        )}
      </div>

      <FileModal file={openFile} onClose={close} />
    </article>
  );
}
