"use client";

import { useState } from "react";
import { fileTitle, type ResultFile } from "@/lib/results";

/**
 * Plain <img> (not next/image) so any DGX-hosted URL works without
 * remotePatterns config. Shown inside the file modal, so no separate lightbox.
 */
export default function ImageViewer({ file }: { file: ResultFile }) {
  const [failed, setFailed] = useState(false);
  const title = fileTitle(file);

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
      <div className="overflow-hidden rounded-xl border border-neutral-200 bg-neutral-50">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={file.url}
          alt={title}
          onError={() => setFailed(true)}
          className="mx-auto max-h-[70vh] w-auto max-w-full object-contain"
        />
      </div>
      {file.page != null && (
        <p className="mt-2 text-xs text-neutral-500">Record page {file.page}</p>
      )}
    </>
  );
}
