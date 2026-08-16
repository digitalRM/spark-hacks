"use client";

import { useEffect, useState } from "react";

const PHRASES = [
  "Reading the dockets",
  "Scanning opinions",
  "Checking citations",
  "Reviewing the record",
  "Listening to oral argument",
  "Weighing the criteria",
  "Cross-referencing sources",
  "Ranking the matches",
];

const SHIMMER_MS = 2400; // one gradient pass
const PASSES = 2; // per phrase
const FADE_MS = 200;
/** how long "done!" sticks around before fading */
const DONE_HOLD_MS = 700;

/**
 * rotating "thinking" status. each phrase gets exactly two shimmer passes (anim restarts per phrase),
 * then fade out, swap, fade in. when `done` flips true it swaps to "done!", holds a bit, then fades for good
 */
export default function ThinkingLabel({
  done = false,
  className = "",
}: {
  done?: boolean;
  className?: string;
}) {
  const [i, setI] = useState(0);
  const [visible, setVisible] = useState(true);
  const [showDone, setShowDone] = useState(false);

  // cycle phrases while thinking
  useEffect(() => {
    if (done) return;
    let swap = 0;
    const tick = window.setInterval(() => {
      setVisible(false);
      swap = window.setTimeout(() => {
        setI((n) => (n + 1) % PHRASES.length);
        setVisible(true);
      }, FADE_MS);
    }, SHIMMER_MS * PASSES + FADE_MS);
    return () => {
      window.clearInterval(tick);
      window.clearTimeout(swap);
    };
  }, [done]);

  // done: fade out -> "done!" -> hold -> fade away for good
  useEffect(() => {
    if (!done) return;
    const t0 = window.setTimeout(() => setVisible(false), 0);
    const a = window.setTimeout(() => {
      setShowDone(true);
      setVisible(true);
    }, FADE_MS);
    const b = window.setTimeout(() => setVisible(false), FADE_MS + DONE_HOLD_MS);
    return () => {
      window.clearTimeout(t0);
      window.clearTimeout(a);
      window.clearTimeout(b);
    };
  }, [done]);

  return (
    <span
      role="status"
      aria-live="polite"
      className={
        "inline-block scale-125 bg-white/85 px-3 tracking-tighter backdrop-blur-sm transition-opacity " +
        (visible ? "opacity-100" : "opacity-0") +
        " " +
        className
      }
      style={{ transitionDuration: `${FADE_MS}ms` }}
    >
      {showDone ? (
        <span className="text-xs font-medium tracking-tight text-neutral-700">
          Done!
        </span>
      ) : (
        // key restarts the shimmer anim on evry new phrase
        <span
          key={i}
          className="shimmer-text text-xs font-medium tracking-tight"
          style={{ ["--shimmer-ms" as string]: `${SHIMMER_MS}ms` }}
        >
          {PHRASES[i]}
        </span>
      )}
    </span>
  );
}
