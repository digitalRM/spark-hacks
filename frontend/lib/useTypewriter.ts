"use client";

import { useEffect, useState, useSyncExternalStore } from "react";

const REDUCE_MOTION = "(prefers-reduced-motion: reduce)";
function subscribeReduceMotion(cb: () => void) {
  const mq = window.matchMedia(REDUCE_MOTION);
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}
const getReduceMotion = () => window.matchMedia(REDUCE_MOTION).matches;
const getReduceMotionServer = () => false;

type Options = {
  /** ms per character while typing */
  typeSpeed?: number;
  /** ms per character while deleting */
  deleteSpeed?: number;
  /** ms to hold a fully-typed phrase */
  holdMs?: number;
  /** ms to wait after clearing before typing the next phrase */
  gapMs?: number;
  /** when false, the animation freezes in place */
  active?: boolean;
};

/**
 * Cycles through `phrases` with a typewriter effect.
 * Returns the current text plus whether the cursor should be shown.
 */
export function useTypewriter(
  phrases: string[],
  {
    typeSpeed = 10,
    deleteSpeed = 5,
    holdMs = 2000,
    gapMs = 300,
    active = true,
  }: Options = {},
) {
  const [index, setIndex] = useState(0);
  const [length, setLength] = useState(0);
  const [phase, setPhase] = useState<"typing" | "holding" | "deleting">(
    "typing",
  );
  const [blink, setBlink] = useState(true);

  const phrase = phrases[index % phrases.length] ?? "";

  // Reduced-motion users get a static first phrase, no animation.
  const reduceMotion = useSyncExternalStore(
    subscribeReduceMotion,
    getReduceMotion,
    getReduceMotionServer,
  );

  // Main state machine.
  useEffect(() => {
    if (!active || reduceMotion || phrases.length === 0) return;

    let delay: number;
    let next: () => void;

    if (phase === "typing") {
      if (length < phrase.length) {
        // slight natural jitter, longer pause after punctuation
        const ch = phrase[length - 1];
        delay = typeSpeed + Math.random() * 10 + (/[,.;]/.test(ch ?? "") ? 60 : 0);
        next = () => setLength((l) => l + 1);
      } else {
        delay = holdMs;
        next = () => setPhase("deleting");
      }
    } else if (phase === "deleting") {
      if (length > 0) {
        delay = deleteSpeed;
        next = () => setLength((l) => l - 1);
      } else {
        delay = gapMs;
        next = () => {
          setIndex((i) => (i + 1) % phrases.length);
          setPhase("typing");
        };
      }
    } else {
      return;
    }

    const t = window.setTimeout(next, delay);
    return () => window.clearTimeout(t);
  }, [
    active,
    reduceMotion,
    phase,
    length,
    phrase,
    phrases.length,
    typeSpeed,
    deleteSpeed,
    holdMs,
    gapMs,
  ]);

  // Blink the cursor only while holding (steady while typing/deleting).
  const holding = phase === "typing" && length >= phrase.length;
  useEffect(() => {
    if (!active || !holding) return;
    const t = window.setInterval(() => setBlink((b) => !b), 530);
    return () => window.clearInterval(t);
  }, [active, holding]);

  if (reduceMotion) return { text: phrases[0] ?? "", cursor: false };
  return {
    text: phrase.slice(0, length),
    cursor: active && (!holding || blink),
  };
}
