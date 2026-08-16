"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { fileKind, fileTitle, type ResultFile } from "@/lib/results";
import { fieldLabel } from "@/lib/humanize";
import FileViewer, { KIND_LABEL, KindIcon } from "./FileViewer";

/**
 * Centered modal that hosts a FileViewer. Portaled to <body> so `fixed`
 * isn't trapped by transformed/animated ancestors. Closes on backdrop click
 * or Escape, and locks body scroll while open. When `file` goes null the
 * panel stays mounted for a short exit animation before unmounting.
 */
export default function FileModal({
  file,
  onClose,
}: {
  file: ResultFile | null;
  onClose: () => void;
}) {
  // detect the open -> closed transition during render (no effect lag), and
  // remember the last non-null file so the exit animation has content
  const [prevFile, setPrevFile] = useState(file);
  const [lastFile, setLastFile] = useState(file);
  const [exiting, setExiting] = useState(false);
  if (file !== prevFile) {
    setPrevFile(file);
    if (file) setLastFile(file);
    setExiting(!file && !!prevFile);
  }

  const open = !!file;
  const mounted = open || exiting;

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // unmount after the exit animation; timer fallback in case animationend
  // never fires (e.g. background tab)
  useEffect(() => {
    if (!exiting) return;
    const t = window.setTimeout(() => setExiting(false), 150);
    return () => window.clearTimeout(t);
  }, [exiting]);

  useEffect(() => {
    if (!mounted) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [mounted]);

  const shown = open ? file : lastFile;
  if (!mounted || !shown || typeof document === "undefined") return null;

  const kind = fileKind(shown);
  const title = fileTitle(shown);

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={onClose}
      className="fixed inset-0 z-[250] flex items-center justify-center p-4 sm:p-8"
    >
      {/* backdrop is a sibling of the panel (not an ancestor) so its opacity
          animation never makes the panel translucent */}
      <div
        aria-hidden
        className={
          "absolute inset-0 bg-neutral-900/50 " +
          (exiting ? "animate-fade-out" : "animate-fade-in")
        }
      />
      <div
        onClick={(e) => e.stopPropagation()}
        onAnimationEnd={() => exiting && setExiting(false)}
        className={
          "relative flex max-h-full w-full max-w-3xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl " +
          (exiting ? "animate-modal-out" : "animate-modal-in")
        }
      >
        <header className="flex items-center gap-3 border-b border-neutral-200 px-4 py-3">
          <KindIcon kind={kind} className="shrink-0 text-neutral-500" />
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-sm font-semibold text-neutral-900">
              {title}
            </h2>
            {shown.field && (
              <p className="truncate text-xs text-neutral-400">
                {fieldLabel(shown.field)}
              </p>
            )}
          </div>
          <span className="shrink-0 rounded-md bg-neutral-100 px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide text-neutral-500">
            {KIND_LABEL[kind]}
          </span>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="ml-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-neutral-500 hover:bg-neutral-100 hover:text-neutral-900"
          >
            ✕
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-auto p-4">
          <FileViewer file={shown} />
        </div>
      </div>
    </div>,
    document.body,
  );
}
