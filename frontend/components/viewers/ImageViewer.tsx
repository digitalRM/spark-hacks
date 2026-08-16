"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { fileTitle, type ResultFile } from "@/lib/results";

/**
 * Inline image with a click-to-enlarge lightbox. Plain <img> (not next/image)
 * so any DGX-hosted URL works without remotePatterns config. The lightbox is
 * portaled to <body> so `position: fixed` isn't trapped by transformed/animated
 * ancestors (AutoHeight, fade-in) and it truly covers the viewport.
 */
export default function ImageViewer({ file }: { file: ResultFile }) {
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  const title = fileTitle(file);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (failed) {
    return (
      <p className="text-sm text-neutral-500">
        Couldn&apos;t load this image.{" "}
        <a href={file.url} target="_blank" rel="noreferrer" className="underline">
          Open directly
        </a>
      </p>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="group relative block w-full overflow-hidden rounded-xl border border-neutral-200 bg-neutral-50"
        aria-label={`Enlarge ${title}`}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={file.url}
          alt={title}
          onError={() => setFailed(true)}
          className="mx-auto max-h-[360px] w-auto max-w-full object-contain transition-transform duration-300 group-hover:scale-[1.01]"
        />
        <span className="pointer-events-none absolute bottom-2 right-2 rounded-md bg-white/85 px-2 py-0.5 text-[11px] font-medium text-neutral-600 opacity-0 backdrop-blur-sm transition-opacity group-hover:opacity-100">
          Click to enlarge
        </span>
      </button>
      {file.page != null && (
        <p className="mt-2 text-xs text-neutral-500">Record page {file.page}</p>
      )}

      {open &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            role="dialog"
            aria-modal="true"
            aria-label={title}
            onClick={() => setOpen(false)}
            className="animate-fade-in fixed inset-0 z-[300] flex items-center justify-center bg-neutral-900/70 p-6 backdrop-blur-sm"
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={file.url}
              alt={title}
              className="max-h-full max-w-full rounded-lg shadow-2xl"
              onClick={(e) => e.stopPropagation()}
            />
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close"
              className="absolute right-4 top-4 flex h-9 w-9 items-center justify-center rounded-full bg-white/90 text-neutral-700 hover:bg-white"
            >
              ✕
            </button>
          </div>,
          document.body,
        )}
    </>
  );
}
