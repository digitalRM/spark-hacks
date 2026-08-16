"use client";

import { useState } from "react";
import { fileTitle, formatBytes, type ResultFile } from "@/lib/results";

/**
 * Inline PDF via the browser's native viewer in an <iframe>, opened at the
 * page of interest when known. Falls back to a link if embedding fails.
 */
export default function PdfViewer({ file }: { file: ResultFile }) {
  const [tall, setTall] = useState(true);
  const title = fileTitle(file);
  const src = `${file.url}#toolbar=0&navpanes=0${file.page ? `&page=${file.page}` : ""}`;

  return (
    <div className="flex flex-col gap-2">
      <div
        className="overflow-hidden rounded-xl border border-neutral-200 bg-neutral-50 transition-[height] duration-500"
        style={{ height: tall ? 720 : 420 }}
      >
        <iframe title={title} src={src} className="h-full w-full" />
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-neutral-500">
        {file.page != null && <span>Opens at page {file.page}</span>}
        {formatBytes(file.size) && <span>{formatBytes(file.size)}</span>}
        <button
          type="button"
          onClick={() => setTall((t) => !t)}
          className="font-medium text-neutral-700 hover:underline"
        >
          {tall ? "Shorter" : "Taller"}
        </button>
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
