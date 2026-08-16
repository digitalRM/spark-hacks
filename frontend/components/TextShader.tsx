"use client";

import { useEffect, useRef } from "react";
import { LEGAL_CORPUS } from "@/lib/legalCorpus";

type Props = {
  /** animate while true, otherwise freezes on last frame */
  active?: boolean;
  /** soft elliptical clearing in the centre (px half-width / half-height) so a label can sit there. `feather` = fade band past the edge, px */
  hole?: { rx: number; ry: number; feather?: number };
  /** when true a "white circle" grows from centre, glyphs it hits get pushed outward radially and fade. takes `revealMs` to cover the card */
  reveal?: boolean;
  revealMs?: number;
  className?: string;
};

// palette (light theme): dim neutral -> blue -> violet as the field gets hotter
const DIM = [212, 212, 212]; // neutral-300
const MID = [96, 165, 250]; // blue-400
const HOT = [139, 92, 246]; // violet-500

const FONT_PX = 11;
const CELL_W = 7; // approx advance of 11px mono
const CELL_H = 15;

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const smooth = (t: number) => t * t * (3 - 2 * t);
const clamp01 = (t: number) => (t < 0 ? 0 : t > 1 ? 1 : t);

/**
 * "text shader": grid of mono glyphs streaming legal text. a moving field (layered sines + slow ripple
 * from top-right where the flow lines come in) drives each glyph's opacity/colour - basically a fragment shader per char instead of per pixel
 */
export default function TextShader({
  active = true,
  hole,
  reveal = false,
  revealMs = 1000,
  className = "",
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const activeRef = useRef(active);
  const holeRef = useRef(hole);
  // timestamp the reveal started (null = not revealing)
  const revealStartRef = useRef<number | null>(null);
  const revealMsRef = useRef(revealMs);
  useEffect(() => {
    activeRef.current = active;
    holeRef.current = hole;
    revealMsRef.current = revealMs;
    if (reveal && revealStartRef.current === null) {
      revealStartRef.current = performance.now();
    } else if (!reveal) {
      revealStartRef.current = null;
    }
  }, [active, hole, reveal, revealMs]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let w = 0;
    let h = 0;
    let cols = 0;
    let rows = 0;
    let dpr = 1;

    const resize = () => {
      const r = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = r.width;
      h = r.height;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      cols = Math.ceil(w / CELL_W) + 1;
      rows = Math.ceil(h / CELL_H) + 1;
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)")
      .matches;

    const corpus = LEGAL_CORPUS;
    const N = corpus.length;
    const t0 = performance.now();
    let raf = 0;

    const frame = (now: number) => {
      const t = (now - t0) / 1000;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.font = `${FONT_PX}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.textBaseline = "top";

      // text drifts left slowly like a ticker, each row is a diffrent slice of the corpus so it reads as pages not one line
      const scroll = Math.floor(t * 6); // chars per sec
      const ox = t * 0.6; // ripple origin drift
      const cx = 0.92 + 0.03 * Math.sin(ox); // ripple origin (top-right, 0..1)
      const cy = -0.05;

      // reveal: radius of the growing white circle (px). covers the far corner at progress 1, eased in so it starts gentle then sweeps
      const rs = revealStartRef.current;
      const rp =
        rs === null ? 0 : smooth(clamp01((now - rs) / revealMsRef.current));
      const maxR = Math.hypot(w / 2, h / 2) + 40;
      const R = rp * maxR;
      const FEATHER = 90; // px band over which a glyph gets pushed & faded
      const PUSH = 46; // px max outward push

      for (let r = 0; r < rows; r++) {
        const rowOffset = r * 97 + scroll; // 97 is coprime-ish with corpus length
        const y = r * CELL_H + 2;
        const ny = r / Math.max(1, rows - 1);
        for (let c = 0; c < cols; c++) {
          const ch = corpus[(rowOffset + c) % N];
          if (ch === " ") continue;
          const nx = c / Math.max(1, cols - 1);

          // --- field ---
          const dx = nx - cx;
          const dy = (ny - cy) * 0.55; // squash so ripples are wide ellipses
          const d = Math.sqrt(dx * dx + dy * dy);
          const ripple = 0.5 + 0.5 * Math.sin(d * 18 - t * 2.4);
          const wave =
            0.5 +
            0.5 * Math.sin(nx * 5.5 + t * 0.9) * Math.sin(ny * 4.2 - t * 0.7);
          const grain =
            0.5 + 0.5 * Math.sin((c * 12.9898 + r * 78.233) * 0.37 + t * 3.1);
          let v = ripple * 0.55 + wave * 0.35 + grain * 0.1;
          // fade toward the corner opposite the ripple origin so it "flows" from top-right
          v *= 1 - 0.35 * clamp01(d - 0.2);
          v = smooth(clamp01(v));

          // --- clearing around the centre label ---
          let hole = 1;
          const hp = holeRef.current;
          if (hp) {
            const px = c * CELL_W + CELL_W / 2 - w / 2;
            const py = y + CELL_H / 2 - h / 2;
            const f = hp.feather ?? 40;
            // normalised elliptical dist, 1 at the clearing edge
            const e = Math.sqrt(
              (px * px) / (hp.rx * hp.rx) + (py * py) / (hp.ry * hp.ry),
            );
            if (e <= 1) continue; // inside, draw nothing
            // approx px past the edge -> 0..1 across the feather band
            hole = smooth(clamp01(((e - 1) * Math.min(hp.rx, hp.ry)) / f));
            if (hole <= 0.02) continue;
          }

          // --- reveal: push outward from centre & fade as the circle passes ---
          let gx = c * CELL_W;
          let gy = y;
          let keep = 1;
          if (R > 0) {
            const px = gx + CELL_W / 2 - w / 2;
            const py = gy + CELL_H / 2 - h / 2;
            const dist = Math.hypot(px, py) || 1;
            // 0 outside the circle -> 1 once the edge has past by FEATHER px
            const s = smooth(clamp01((R - dist) / FEATHER));
            if (s >= 0.999) continue; // fully gone
            keep = 1 - s;
            const push = PUSH * s;
            gx += (px / dist) * push;
            gy += (py / dist) * push;
          }

          // --- shading ---
          const alpha = (0.18 + 0.82 * v) * hole * keep;
          const k = v < 0.6 ? v / 0.6 : (v - 0.6) / 0.4;
          const from = v < 0.6 ? DIM : MID;
          const to = v < 0.6 ? MID : HOT;
          const rr = Math.round(lerp(from[0], to[0], k));
          const gg = Math.round(lerp(from[1], to[1], k));
          const bb = Math.round(lerp(from[2], to[2], k));
          ctx.fillStyle = `rgba(${rr},${gg},${bb},${alpha.toFixed(3)})`;
          ctx.fillText(ch, gx, gy);
        }
      }

      // soft vignette so the field melts into the card edges
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, "rgba(255,255,255,0.35)");
      g.addColorStop(0.15, "rgba(255,255,255,0)");
      g.addColorStop(0.85, "rgba(255,255,255,0)");
      g.addColorStop(1, "rgba(255,255,255,0.6)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
      const gx = ctx.createLinearGradient(0, 0, w, 0);
      gx.addColorStop(0, "rgba(255,255,255,0.7)");
      gx.addColorStop(0.1, "rgba(255,255,255,0)");
      gx.addColorStop(0.9, "rgba(255,255,255,0)");
      gx.addColorStop(1, "rgba(255,255,255,0.7)");
      ctx.fillStyle = gx;
      ctx.fillRect(0, 0, w, h);

      if (activeRef.current && !reduceMotion) raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden
      className={`block h-full w-full ${className}`}
    />
  );
}
