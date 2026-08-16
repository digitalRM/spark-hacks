"use client";

import { useCallback, useState } from "react";
import { fileKind, fileTitle, type SearchResult } from "@/lib/results";
import {
  ArrowUpRightIcon,
  KIND_LABEL,
  KindIcon,
} from "@/components/viewers/FileViewer";
import FileModal from "@/components/viewers/FileModal";

const FIELD_PREVIEW_CHARS = 420;

/**
 * One matched record from the executor: title, meta line, the model's rationale
 * for the match, the projected fields the query asked for (e.g. the summary),
 * then a row of evidence / source tiles. Clicking a tile opens its viewer.
 */
export default function ResultCard({
  result,
  index,
}: {
  result: SearchResult;
  index: number;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const openFile = open != null ? result.files[open] : null;
  const close = useCallback(() => setOpen(null), []);

  return (
    <article className="rounded-2xl bg-white">
      <header className="mb-3 flex items-start gap-3">
        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-neutral-100 text-xs font-medium text-neutral-500">
          {index + 1}
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-[15px] font-semibold leading-snug tracking-tight">
            {result.url ? (
              <a
                href={result.url}
                target="_blank"
                rel="noreferrer"
                className="hover:underline"
              >
                {result.title}
              </a>
            ) : (
              result.title
            )}
          </h3>
          {result.subtitle && (
            <p className="mt-0.5 text-xs text-neutral-500">{result.subtitle}</p>
          )}
        </div>
        {result.score != null && (
          <span
            title="Model confidence in the match"
            className="shrink-0 rounded-md bg-neutral-100 px-2 py-0.5 font-mono text-[11px] tabular-nums text-neutral-600"
          >
            {Math.round(result.score * 100)}%
          </span>
        )}
      </header>

      <div className="rounded-2xl border border-neutral-200 p-3 px-4">
        {/* why it matched */}
        {result.highlights && result.highlights.length > 0 && (
          <ul className="flex flex-col gap-1 text-[13px] leading-relaxed text-neutral-700">
            {result.highlights.map((h, i) => (
              <li key={i} className="flex gap-2">
                <span className="mt-[9px] h-1 w-1 shrink-0 rounded-full bg-violet-400" />
                <span>{h}</span>
              </li>
            ))}
          </ul>
        )}

        {/* projected fields, e.g. the summary the query selected */}
        {result.fields && result.fields.length > 0 && (
          <dl
            className={
              "flex flex-col gap-3 " +
              (result.highlights?.length ? "mt-3 border-t border-neutral-100 pt-3" : "")
            }
          >
            {result.fields.map((f) => {
              const long = f.value.length > FIELD_PREVIEW_CHARS;
              const isOpen = !!expanded[f.label];
              const shown =
                long && !isOpen ? f.value.slice(0, FIELD_PREVIEW_CHARS) + "…" : f.value;
              return (
                <div key={f.label}>
                  <dt className="text-[11px] font-semibold uppercase tracking-wide text-neutral-400">
                    {f.label}
                  </dt>
                  <dd className="mt-1 whitespace-pre-line text-[13.5px] leading-relaxed text-neutral-800">
                    {shown}
                    {long && (
                      <button
                        type="button"
                        onClick={() =>
                          setExpanded((e) => ({ ...e, [f.label]: !isOpen }))
                        }
                        className="ml-1 text-xs font-medium text-neutral-500 hover:text-neutral-900"
                      >
                        {isOpen ? "Show less" : "Show more"}
                      </button>
                    )}
                  </dd>
                </div>
              );
            })}
          </dl>
        )}

        {/* evidence + source tiles */}
        {result.files.length > 0 && (
          <div className="-mx-1 mt-4">
            <div className="flex flex-wrap gap-2">
              {result.files.map((f, i) => {
                const kind = fileKind(f);
                const isLink = kind === "link";
                const tileClass =
                  "group flex items-center gap-2 rounded-[12px] border border-neutral-200 bg-white px-2.5 py-1.5 text-left text-xs text-neutral-700 transition-colors hover:border-neutral-400";
                const inner = (
                  <>
                    <div className="relative -ml-0.5 h-4 w-4 shrink-0">
                      <KindIcon
                        kind={kind}
                        className={
                          "absolute inset-0 text-neutral-500 " +
                          (isLink
                            ? "transition-all duration-200 ease-out group-hover:scale-75 group-hover:opacity-0 group-hover:blur-[2px]"
                            : "")
                        }
                      />
                      {isLink && (
                        <ArrowUpRightIcon className="absolute inset-0 scale-75 text-neutral-700 opacity-0 blur-[2px] transition-all duration-200 ease-out group-hover:scale-100 group-hover:opacity-100 group-hover:blur-none" />
                      )}
                    </div>
                    <span className="max-w-[240px] truncate font-medium">
                      {fileTitle(f)}
                    </span>
                    {f.page != null && (
                      <span className="text-neutral-400">p. {f.page}</span>
                    )}
                    <span className="shrink-0 rounded-md bg-neutral-100 px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide text-neutral-500">
                      {KIND_LABEL[kind]}
                    </span>
                  </>
                );
                return isLink ? (
                  <a
                    key={i}
                    href={f.url}
                    target="_blank"
                    rel="noreferrer"
                    title={f.snippet ?? f.url}
                    className={tileClass}
                  >
                    {inner}
                  </a>
                ) : (
                  <button
                    key={i}
                    type="button"
                    onClick={() => setOpen(i)}
                    aria-haspopup="dialog"
                    title={f.snippet ?? f.url}
                    className={tileClass}
                  >
                    {inner}
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
