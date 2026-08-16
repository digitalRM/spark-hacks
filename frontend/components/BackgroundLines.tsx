"use client";

import { RefObject, useEffect, useState } from "react";

type Props = {
  /** When false, the lines fade out. */
  active: boolean;
  /** Element the lines flow into (left edge) and out of (right edge). */
  targetRef: RefObject<HTMLElement | null>;
  /** Number of lines. */
  count?: number;
  /** How far (px) the lines reach past the box border into the box. */
  reach?: number;
};

/** Duration of the centre-out dissolve when deactivated. */
const WIPE_MS = 550;

/**
 * File cards flowing into the box. `line` is the line index, `side` which
 * half it rides (both travel edge → box), `dur` travel time (s), `offset`
 * start offset (s) so the stream is already populated on load.
 */
const CARDS: { side: "l" | "r"; line: number; dur: number; offset: number }[] = [
  { side: "l", line: 1, dur: 8, offset: 0 },
  { side: "l", line: 3, dur: 9.5, offset: 4.2 },
  { side: "l", line: 6, dur: 7, offset: 2.1 },
  { side: "l", line: 8, dur: 8.8, offset: 6.3 },
  { side: "r", line: 2, dur: 9, offset: 3.4 },
  { side: "r", line: 4, dur: 7.6, offset: 0.9 },
  { side: "r", line: 5, dur: 8.4, offset: 5.5 },
  { side: "r", line: 7, dur: 7.2, offset: 2.7 },
];

/**
 * A little document (paper with a folded corner and text lines) sitting in a
 * white, bordered, rounded container — roughly `p-1 border border-neutral-200 rounded-xl`.
 */
function FileCard() {
  return (
    <g transform="scale(1.3)">
      {/* container */}

      <g transform="translate(-9 -11)">
        {/* paper — slightly rounded corners, folded top-right */}
        <path
          d="M 3 1 H 12 L 17 6 V 19 A 2 2 0 0 1 15 21 H 3 A 2 2 0 0 1 1 19 V 3 A 2 2 0 0 1 3 1 Z"
          fill="#fff"
          stroke="#c4c4c4"
          strokeWidth="1"
          strokeLinejoin="round"
        />
        {/* folded corner */}
        <path
          d="M 12 1 V 4.5 A 1.5 1.5 0 0 0 13.5 6 H 17"
          fill="none"
          stroke="#c4c4c4"
          strokeWidth="1"
          strokeLinejoin="round"
        />
        {/* text lines */}
        <g stroke="#d9d9d9" strokeWidth="1" strokeLinecap="round">
          <line x1="4" y1="10" x2="13" y2="10" />
          <line x1="4" y1="13" x2="14" y2="13" />
          <line x1="4" y1="16" x2="11" y2="16" />
        </g>
      </g>
    </g>
  );
}

type Box = { left: number; right: number; top: number; bottom: number };
type Size = { w: number; h: number };

/**
 * Thin lines that sweep in from the left edge of the viewport, converge into
 * the left side of the target box, then fan back out from its right side to
 * the right edge of the viewport. Curves are cubic Béziers with horizontal
 * tangents at both ends so they enter/exit the box cleanly.
 */
export default function BackgroundLines({
  active,
  targetRef,
  count = 10,
  reach = 12,
}: Props) {
  const [box, setBox] = useState<Box | null>(null);
  const [size, setSize] = useState<Size | null>(null);

  // Track the box only while active. Once inactive the geometry is frozen so
  // the lines don't chase the box as it slides up — they just dissolve in place.
  useEffect(() => {
    const el = targetRef.current;
    if (!el || !active) return;

    let raf = 0;
    const measure = () => {
      raf = 0;
      const r = el.getBoundingClientRect();
      setBox({ left: r.left, right: r.right, top: r.top, bottom: r.bottom });
      setSize({ w: window.innerWidth, h: window.innerHeight });
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(measure);
    };

    schedule();
    const ro = new ResizeObserver(schedule);
    ro.observe(el);
    ro.observe(document.documentElement);
    window.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, { passive: true });
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener("resize", schedule);
      window.removeEventListener("scroll", schedule);
    };
  }, [targetRef, active]);

  const ready = box !== null && size !== null;

  let paths: { left: string; right: string; strong: boolean }[] = [];
  if (ready) {
    const { w, h } = size;
    // Vertical spread at the viewport edges vs. inside the box.
    const edgeTop = h * 0.06;
    const edgeBot = h * 0.94;
    const inset = Math.min(10, (box.bottom - box.top) * 0.15);
    const boxTop = box.top + inset;
    const boxBot = box.bottom - inset;

    paths = Array.from({ length: count }, (_, i) => {
      const t = count === 1 ? 0.5 : i / (count - 1);
      const yEdge = edgeTop + (edgeBot - edgeTop) * t;
      const yBox = boxTop + (boxBot - boxTop) * t;

      // Left: viewport edge → just inside the box's left edge. Control points
      // share an x so the curve leaves/arrives horizontally.
      const lEnd = box.left + reach;
      const lcx = box.left * 0.5;
      const left = `M 0 ${yEdge.toFixed(1)} C ${lcx.toFixed(1)} ${yEdge.toFixed(1)}, ${lcx.toFixed(1)} ${yBox.toFixed(1)}, ${lEnd.toFixed(1)} ${yBox.toFixed(1)}`;

      // Right: just inside the box's right edge → viewport edge.
      const rStart = box.right - reach;
      const rcx = box.right + (w - box.right) * 0.5;
      const right = `M ${rStart.toFixed(1)} ${yBox.toFixed(1)} C ${rcx.toFixed(1)} ${yBox.toFixed(1)}, ${rcx.toFixed(1)} ${yEdge.toFixed(1)}, ${w.toFixed(1)} ${yEdge.toFixed(1)}`;

      return { left, right, strong: i % 3 === 0 };
    });
  }

  return (
    <svg
      aria-hidden
      className="pointer-events-none fixed inset-0 -z-10 h-full w-full transition-opacity ease-out"
      style={{
        // Fade in on mount; on deactivate the mask wipe below does the work,
        // and opacity only drops after it has finished (keeps things tidy on
        // re-activation).
        opacity: active && ready ? 1 : 0,
        transitionDuration: active ? "500ms" : "0ms",
        transitionDelay: active ? "0ms" : `${WIPE_MS}ms`,
      }}
      viewBox={size ? `0 0 ${size.w} ${size.h}` : undefined}
      preserveAspectRatio="none"
    >
      <defs>
        {/* Soften the lines toward the far left/right edges of the viewport. */}
        <linearGradient id="bg-lines-fade" x1="0" x2="1" y1="0" y2="0">
          <stop offset="0" stopColor="#fff" stopOpacity="0" />
          <stop offset="0.12" stopColor="#fff" />
          <stop offset="0.88" stopColor="#fff" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
        {/* Black (= hidden) in the middle, soft toward its own ends. Scaled
            up from the centre on deactivate so the lines dissolve outward. */}
        <linearGradient id="bg-lines-wipe" x1="0" x2="1" y1="0" y2="0">
          <stop offset="0" stopColor="#000" stopOpacity="0" />
          <stop offset="0.4" stopColor="#000" />
          <stop offset="0.6" stopColor="#000" />
          <stop offset="1" stopColor="#000" stopOpacity="0" />
        </linearGradient>
        <mask id="bg-lines-m">
          <rect
            x="0"
            y="0"
            width="100%"
            height="100%"
            fill="url(#bg-lines-fade)"
          />
          <rect
            x="0"
            y="0"
            width="100%"
            height="100%"
            fill="url(#bg-lines-wipe)"
            style={{
              transformBox: "fill-box",
              transformOrigin: "50% 50%",
              transform: `scaleX(${active ? 0 : 3})`,
              transition: active
                ? "none"
                : `transform ${WIPE_MS}ms cubic-bezier(0.4, 0, 0.6, 1)`,
            }}
          />
        </mask>
      </defs>
      <g mask="url(#bg-lines-m)">
        <g fill="none">
          {paths.map((p, i) => (
            <g key={i}>
              <path
                id={`bg-line-l-${i}`}
                d={p.left}
                stroke={p.strong ? "#d4d4d4" : "#e5e5e5"}
                strokeWidth={p.strong ? 1.5 : 1}
                vectorEffect="non-scaling-stroke"
              />
              <path
                id={`bg-line-r-${i}`}
                d={p.right}
                stroke={p.strong ? "#d4d4d4" : "#e5e5e5"}
                strokeWidth={p.strong ? 1.5 : 1}
                vectorEffect="non-scaling-stroke"
              />
            </g>
          ))}
        </g>

        {/* Case-file cards travelling along the lines into the box from both sides. */}
        {ready &&
          // Never on the outermost (top/bottom) lines.
          CARDS.filter((c) => c.line > 0 && c.line < count - 1).map((c) => {
            const dur = c.dur;
            const begin = -c.offset;
            return (
              <g key={`card-${c.side}-${c.line}`} opacity="0">
                <animateMotion
                  dur={`${dur}s`}
                  begin={`${begin}s`}
                  repeatCount="indefinite"
                  calcMode="spline"
                  // Left paths run edge → box; right paths run box → edge,
                  // so ride those backwards to also travel into the box.
                  keyPoints={c.side === "l" ? "0;1" : "1;0"}
                  keyTimes="0;1"
                  keySplines="0.35 0 0.65 1"
                >
                  <mpath href={`#bg-line-${c.side}-${c.line}`} />
                </animateMotion>
                {/* Fade in leaving the edge, dissolve as it enters the box. */}
                <animate
                  attributeName="opacity"
                  values="0;1;1;0"
                  keyTimes="0;0.12;0.86;1"
                  dur={`${dur}s`}
                  begin={`${begin}s`}
                  repeatCount="indefinite"
                />
                <FileCard />
              </g>
            );
          })}
      </g>
    </svg>
  );
}
