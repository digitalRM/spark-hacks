"use client";

import { useId } from "react";

/**
 * bunch of vertical lines, each permanently tinted, with a gradient "comet" (bright head, fading tail) sweeping down each one -
 * the compiled plan feeding into the search result card while it runs. fills its container, goes in the gap between the two cards
 */

type Props = {
  /** pulses run while true, whole thing fades out when false */
  active: boolean;
  count?: number;
  className?: string;
};

// blues -> purples -> blues accross the width, matches BackgroundLines
const COLORS = [
  "#38bdf8", // sky
  "#60a5fa", // blue
  "#818cf8", // indigo
  "#a78bfa", // violet
  "#c084fc", // purple
  "#a78bfa",
  "#818cf8",
  "#60a5fa",
];
const DURATIONS = [0.9, 1.2, 1.0, 1.35, 0.95, 1.25, 1.1, 1.3];
const DELAYS = [0, 0.45, 0.2, 0.7, 0.35, 0.6, 0.1, 0.8];

/** comet length in viewbox units (line is 0..100) */
const TAIL = 45;

export default function FlowLines({ active, count = 24, className = "" }: Props) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");

  const lines = Array.from({ length: count }, (_, i) => {
    const t = count === 1 ? 0.5 : i / (count - 1);
    const x = 3 + t * 94; // 3..97 (viewbox units)
    return {
      x,
      color: COLORS[Math.round(t * (COLORS.length - 1))],
      dur: DURATIONS[i % DURATIONS.length],
      delay: DELAYS[i % DELAYS.length],
      id: `${uid}-g${i}`,
    };
  });

  return (
    <svg
      aria-hidden
      className={`pointer-events-none transition-opacity duration-500 ${className}`}
      style={{ opacity: active ? 1 : 0 }}
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
    >
      <defs>
        {lines.map((l) => (
          // vertical gradient the length of the comet, translated down the line. pad -> transparent above/below so only the comet shows
          <linearGradient
            key={l.id}
            id={l.id}
            gradientUnits="userSpaceOnUse"
            x1="0"
            y1={-TAIL}
            x2="0"
            y2="0"
          >
            <stop offset="0" stopColor={l.color} stopOpacity="0" />
            <stop offset="0.75" stopColor={l.color} stopOpacity="0.85" />
            <stop offset="0.97" stopColor={l.color} stopOpacity="1" />
            <stop offset="1" stopColor={l.color} stopOpacity="0" />
            {active && (
              <animateTransform
                attributeName="gradientTransform"
                type="translate"
                from="0 0"
                to={`0 ${100 + TAIL}`}
                dur={`${l.dur}s`}
                begin={`-${l.delay}s`}
                repeatCount="indefinite"
              />
            )}
          </linearGradient>
        ))}
      </defs>

      {lines.map((l) => (
        <g key={l.id} fill="none">
          {/* always-on tinted rail */}
          <line
            x1={l.x}
            y1="0"
            x2={l.x}
            y2="100"
            stroke={l.color}
            strokeOpacity={0.3}
            strokeWidth={1.25}
            vectorEffect="non-scaling-stroke"
          />
          {/* gradient comet */}
          <line
            x1={l.x}
            y1="0"
            x2={l.x}
            y2="100"
            stroke={`url(#${l.id})`}
            strokeWidth={2.25}
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
        </g>
      ))}
    </svg>
  );
}
