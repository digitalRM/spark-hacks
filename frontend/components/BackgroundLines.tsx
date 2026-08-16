"use client";

import { RefObject, useEffect, useState } from "react";

type Props = {
  /** false = lines fade out */
  active: boolean;
  /** element the lines flow into (left edge) and out of (right edge) */
  targetRef: RefObject<HTMLElement | null>;
  /** number of lines */
  count?: number;
  /** px the lines reach past the box border into the box */
  reach?: number;
};

/** duration of the centre-out dissolve on deactivate */
const WIPE_MS = 550;

/**
 * one hue per line, top -> bottom. `line` is the stroke, cards use `stroke` for
 * outlines, `fill` for paper, `text` for the litle text rules
 */
type Hue = { line: string; stroke: string; fill: string; text: string };
const HUES: Hue[] = [
  { line: "#7dd3fc", stroke: "#38bdf8", fill: "#e0f2fe", text: "#7dd3fc" }, // sky
  { line: "#93c5fd", stroke: "#60a5fa", fill: "#dbeafe", text: "#93c5fd" }, // blue
  { line: "#a5b4fc", stroke: "#818cf8", fill: "#e0e7ff", text: "#a5b4fc" }, // indigo
  { line: "#c4b5fd", stroke: "#a78bfa", fill: "#ede9fe", text: "#c4b5fd" }, // violet
  { line: "#d8b4fe", stroke: "#c084fc", fill: "#f3e8ff", text: "#d8b4fe" }, // purple
  { line: "#d8b4fe", stroke: "#c084fc", fill: "#f3e8ff", text: "#d8b4fe" }, // purple
  { line: "#c4b5fd", stroke: "#a78bfa", fill: "#ede9fe", text: "#c4b5fd" }, // violet
  { line: "#a5b4fc", stroke: "#818cf8", fill: "#e0e7ff", text: "#a5b4fc" }, // indigo
  { line: "#93c5fd", stroke: "#60a5fa", fill: "#dbeafe", text: "#93c5fd" }, // blue
  { line: "#7dd3fc", stroke: "#38bdf8", fill: "#e0f2fe", text: "#7dd3fc" }, // sky
];
const hueFor = (i: number) => HUES[i % HUES.length];

/**
 * file cards flowing into the box. `line` = line index, `side` = which half it rides
 * (both travel edge -> box), `dur` = travel time s, `offset` = start offset s so the
 * stream is already populated on load
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

/** little doc (paper w/ folded corner + text lines) tinted to its line's hue */
function FileCard({ hue }: { hue: Hue }) {
  return (
    <g transform="scale(1.3)">
      <g transform="translate(-9 -11)">
        {/* paper - slightly rounded corners, folded top-right */}
        <path
          d="M 3 1 H 12 L 17 6 V 19 A 2 2 0 0 1 15 21 H 3 A 2 2 0 0 1 1 19 V 3 A 2 2 0 0 1 3 1 Z"
          fill={hue.fill}
          stroke={hue.stroke}
          strokeWidth="1"
          strokeLinejoin="round"
        />
        {/* folded corner */}
        <path
          d="M 12 1 V 4.5 A 1.5 1.5 0 0 0 13.5 6 H 17"
          fill="none"
          stroke={hue.stroke}
          strokeWidth="1"
          strokeLinejoin="round"
        />
        {/* text lines */}
        <g stroke={hue.text} strokeWidth="1" strokeLinecap="round">
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
 * thin lines sweeping in from the left viewport edge, converging into the target box's
 * left side, then fanning back out from its right side to the right edge. cubic beziers
 * w/ horizontal tangents both ends so they enter/exit the box cleanly
 */
export default function BackgroundLines({
  active,
  targetRef,
  count = 10,
  reach = 12,
}: Props) {
  const [box, setBox] = useState<Box | null>(null);
  const [size, setSize] = useState<Size | null>(null);


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
  let areas: { left: string; right: string } | null = null;
  if (ready) {
    const { w, h } = size;
    // vertical spread at the viewport edges vs. inside the box
    const edgeTop = h * 0.06;
    const edgeBot = h * 0.94;
    const inset = Math.min(10, (box.bottom - box.top) * 0.15);
    const boxTop = box.top + inset;
    const boxBot = box.bottom - inset;

    paths = Array.from({ length: count }, (_, i) => {
      const t = count === 1 ? 0.5 : i / (count - 1);
      const yEdge = edgeTop + (edgeBot - edgeTop) * t;
      const yBox = boxTop + (boxBot - boxTop) * t;

      // left
      const lEnd = box.left + reach;
      const lcx = box.left * 0.5;
      const left = `M 0 ${yEdge.toFixed(1)} C ${lcx.toFixed(1)} ${yEdge.toFixed(1)}, ${lcx.toFixed(1)} ${yBox.toFixed(1)}, ${lEnd.toFixed(1)} ${yBox.toFixed(1)}`;

      // right
      const rStart = box.right - reach;
      const rcx = box.right + (w - box.right) * 0.5;
      const right = `M ${rStart.toFixed(1)} ${yBox.toFixed(1)} C ${rcx.toFixed(1)} ${yBox.toFixed(1)}, ${rcx.toFixed(1)} ${yEdge.toFixed(1)}, ${w.toFixed(1)} ${yEdge.toFixed(1)}`;

      return { left, right, strong: i % 3 === 0 };
    });

    const yEdgeB = edgeBot.toFixed(1);
    const yEdgeT = edgeTop.toFixed(1);
    const yBoxT = boxTop.toFixed(1);
    const yBoxB = boxBot.toFixed(1);
    const lEnd = (box.left + reach).toFixed(1);
    const lcx = (box.left * 0.5).toFixed(1);
    const rStart = (box.right - reach).toFixed(1);
    const rcx = (box.right + (w - box.right) * 0.5).toFixed(1);
    const W = w.toFixed(1);
    areas = {
      left: `M 0 ${yEdgeT} C ${lcx} ${yEdgeT}, ${lcx} ${yBoxT}, ${lEnd} ${yBoxT} L ${lEnd} ${yBoxB} C ${lcx} ${yBoxB}, ${lcx} ${yEdgeB}, 0 ${yEdgeB} Z`,
      right: `M ${rStart} ${yBoxT} C ${rcx} ${yBoxT}, ${rcx} ${yEdgeT}, ${W} ${yEdgeT} L ${W} ${yEdgeB} C ${rcx} ${yEdgeB}, ${rcx} ${yBoxB}, ${rStart} ${yBoxB} Z`,
    };
  }

  return (
    <svg
      aria-hidden
      className="pointer-events-none fixed inset-0 -z-10 h-full w-full transition-opacity ease-out"
      style={{

        opacity: active && ready ? 1 : 0,
        transitionDuration: active ? "500ms" : "0ms",
        transitionDelay: active ? "0ms" : `${WIPE_MS}ms`,
      }}
      viewBox={size ? `0 0 ${size.w} ${size.h}` : undefined}
      preserveAspectRatio="none"
    >
      <defs>
        {/* soften the lines toward the far left/right viewport edges */}
        <linearGradient id="bg-lines-fade" x1="0" x2="1" y1="0" y2="0">
          <stop offset="0" stopColor="#fff" stopOpacity="0" />
          <stop offset="0.12" stopColor="#fff" />
          <stop offset="0.88" stopColor="#fff" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
        {/* black (= hidden) in the middle, soft toward its ends. scaled up from
            the centre on deactivate so the lines disolve outward */}
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
      {areas && (
        <g fill="none" stroke="none">
          <path data-bg-clear-area d={areas.left} />
          <path data-bg-clear-area d={areas.right} />
        </g>
      )}
      <g mask="url(#bg-lines-m)">
        <g fill="none">
          {paths.map((p, i) => (
            <g key={i}>
              <path
                id={`bg-line-l-${i}`}
                d={p.left}
                stroke={hueFor(i).line}
                strokeWidth={p.strong ? 1.5 : 1}
                vectorEffect="non-scaling-stroke"
              />
              <path
                id={`bg-line-r-${i}`}
                d={p.right}
                stroke={hueFor(i).line}
                strokeWidth={p.strong ? 1.5 : 1}
                vectorEffect="non-scaling-stroke"
              />
            </g>
          ))}
        </g>

        {/* case-file cards riding the lines into the box from both sides */}
        {ready &&
          // never on the outermost (top/bottom) lines
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
                  // left paths run edge -> box, right paths run box -> edge,
                  // so ride those backwards to also travel into the box
                  keyPoints={c.side === "l" ? "0;1" : "1;0"}
                  keyTimes="0;1"
                  // hard ease-in: linger near the edge, then rush into the box
                  keySplines="0.7 0 0.84 0"
                >
                  <mpath href={`#bg-line-${c.side}-${c.line}`} />
                </animateMotion>
                {/* fade in leaving the edge, dissolve as it enters the box */}
                <animate
                  attributeName="opacity"
                  values="0;1;1;0"
                  keyTimes="0;0.12;0.97;1"
                  dur={`${dur}s`}
                  begin={`${begin}s`}
                  repeatCount="indefinite"
                />
                <FileCard hue={hueFor(c.line)} />
              </g>
            );
          })}
      </g>
    </svg>
  );
}
