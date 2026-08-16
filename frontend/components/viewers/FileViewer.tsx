"use client";

import { fileKind, fileTitle, type FileKind, type ResultFile } from "@/lib/results";
import AudioViewer from "./AudioViewer";
import ImageViewer from "./ImageViewer";
import PdfViewer from "./PdfViewer";
import TextViewer from "./TextViewer";

/** Picks the right viewer for a file by kind. */
export default function FileViewer({ file }: { file: ResultFile }) {
  switch (fileKind(file)) {
    case "audio":
      return <AudioViewer file={file} />;
    case "image":
      return <ImageViewer file={file} />;
    case "pdf":
      return <PdfViewer file={file} />;
    case "text":
      return <TextViewer file={file} />;
    case "link":
    default:
      // An HTML page (e.g. the CourtListener opinion) or something we can't
      // embed: hand off to a new tab, with the rationale if we have one.
      return (
        <div className="flex flex-col gap-3 text-sm text-neutral-600">
          {file.snippet && <p className="italic">“{file.snippet}”</p>}
          <a
            href={file.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex w-fit items-center gap-1.5 rounded-lg bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-800"
          >
            Open {fileTitle(file)} ↗
          </a>
          <p className="break-all font-mono text-[11px] text-neutral-400">{file.url}</p>
        </div>
      );
  }
}

/** Small monochrome glyph per kind (16×16). */
export function KindIcon({
  kind,
  className = "",
}: {
  kind: FileKind;
  className?: string;
}) {
  const common = {
    width: 16,
    height: 16,
    viewBox: "0 0 16 16",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.5,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    className,
    "aria-hidden": true,
  };
  switch (kind) {
    case "audio":
      return (
        <svg {...common}>
          <path d="M2 6.5v3h2.5L8 12.5v-9L4.5 6.5H2z" />
          <path d="M10.5 5.5a3.5 3.5 0 0 1 0 5" />
          <path d="M12.5 3.5a6 6 0 0 1 0 9" />
        </svg>
      );
    case "image":
      return (
        <svg {...common}>
          <rect x="2" y="3" width="12" height="10" rx="1.5" />
          <circle cx="6" cy="6.5" r="1.2" />
          <path d="M14 11l-3.5-3.5L5 13" />
        </svg>
      );
    case "pdf":
      return (
        <svg {...common}>
          <path d="M4 1.5h5l3 3v10H4z" />
          <path d="M9 1.5v3h3" />
          <path d="M6 9h4M6 11.5h4" />
        </svg>
      );
    case "text":
      return (
        <svg {...common}>
          <path d="M3 3.5h10M3 6.5h10M3 9.5h7M3 12.5h5" />
        </svg>
      );
    case "link":
      return (
        <svg {...common}>
          <path d="M6.5 9.5l3-3" />
          <path d="M7 4.5l1-1a2.5 2.5 0 0 1 3.5 3.5l-1 1" />
          <path d="M9 11.5l-1 1A2.5 2.5 0 0 1 4.5 9l1-1" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <path d="M4 1.5h5l3 3v10H4z" />
          <path d="M9 1.5v3h3" />
        </svg>
      );
  }
}

/** Arrow to the top-right (16×16), for "opens in a new tab" affordances. */
export function ArrowUpRightIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      width={16}
      height={16}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden
    >
      <path d="M4.5 11.5l7-7" />
      <path d="M6 4.5h5.5V10" />
    </svg>
  );
}

export const KIND_LABEL: Record<FileKind, string> = {
  audio: "Audio",
  image: "Image",
  pdf: "PDF",
  text: "Text",
  link: "Link",
};
