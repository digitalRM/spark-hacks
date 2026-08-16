"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

/**
 * animates its own height to match content whenever the content resizes (eg tall
 * loading state -> short result). measured w/ ResizeObserver, transitioned via css
 */
export default function AutoHeight({
  children,
  durationMs = 550,
  className = "",
}: {
  children: ReactNode;
  durationMs?: number;
  className?: string;
}) {
  const innerRef = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number | null>(null);

  useEffect(() => {
    const el = innerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      setHeight(entry.contentRect.height);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <div
      className={`overflow-hidden ${className}`}
      style={{
        height: height === null ? "auto" : height,
        transition: `height ${durationMs}ms cubic-bezier(0.22, 1, 0.36, 1)`,
      }}
    >
      <div ref={innerRef}>{children}</div>
    </div>
  );
}
