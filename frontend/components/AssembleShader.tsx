"use client";

import { useEffect, useRef } from "react";
import { LEGAL_PASSAGES } from "@/lib/legalCorpus";

type Props = {
  /** animate while true, otherwise freezes on last frame */
  active?: boolean;
  /** soft elliptical clearing in the centre (px half-width / half-height) so a label can sit there. `feather` = fade band past the edge, px */
  hole?: { rx: number; ry: number; feather?: number };
  className?: string;
};

const FONT_PX = 11;
const CELL_H = 15;
const SCROLL = 20; // px/s the assembled block drifts upward
const ASSEMBLE_AT = 0.7; // fraction of height where a line locks into place
const FLIGHT = 1.1; // s a glyph takes to fly in
const STAGGER = 0.014; // s per column, left -> right within a line
const JITTER = 0.35; // s of per-glyph randomness on top of the stagger

const clamp01 = (t: number) => (t < 0 ? 0 : t > 1 ? 1 : t);
const smooth = (t: number) => t * t * (3 - 2 * t);
const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);

/** cheap deterministic hash -> [0,1) */
function hash01(a: number, b: number, c = 0) {
  let h = (a * 374761393 + b * 668265263 + c * 2246822519) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

/** endless stream of wrapped lines: cite heading, text, blank line, next passage */
function makeLineSource(cols: number) {
  const lines: string[] = [];
  let i = 0;
  const more = () => {
    const p = LEGAL_PASSAGES[i % LEGAL_PASSAGES.length];
    i++;
    lines.push(p.cite.toUpperCase());
    let line = "";
    for (const word of p.text.split(" ")) {
      if (line.length + word.length + (line ? 1 : 0) > cols) {
        lines.push(line);
        line = word;
      } else {
        line = line ? `${line} ${word}` : word;
      }
    }
    if (line) lines.push(line);
    lines.push("");
  };
  return (row: number) => {
    while (lines.length <= row) more();
    return lines[row];
  };
}

/**
 * "assemble shader": legal text being pulled together. lines drift slowly upward as a
 * block; each new line locks in around 70% down the card, its glyphs flying in from
 * scattered points (mostly from below and the sides), converging left -> right, landing
 * with a brief darker flash before settling to grey and riding up with the rest. greys
 * only, deliberately not the blue/violet field used for thinking/results
 */
export default function AssembleShader({ active = true, hole, className = "" }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const activeRef = useRef(active);
  const holeRef = useRef(hole);
  useEffect(() => {
    activeRef.current = active;
    holeRef.current = hole;
  }, [active, hole]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let w = 0;
    let h = 0;
    let dpr = 1;
    let charW = 6.6;
    let cols = 1;
    let x0 = 0;
    let lineAt: (row: number) => string = () => "";

    const font = `${FONT_PX}px ui-monospace, SFMono-Regular, Menlo, monospace`;

    const resize = () => {
      const r = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = r.width;
      h = r.height;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.font = font;
      charW = ctx.measureText("M").width || 6.6;
      cols = Math.max(20, Math.floor((w - 48) / charW));
      x0 = (w - cols * charW) / 2;
      lineAt = makeLineSource(cols);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const t0 = performance.now();
    let raf = 0;

    const frame = (now: number) => {
      // start a few seconds in so the card is already populated on mount
      const t = (reduceMotion ? 0 : (now - t0) / 1000) + 14;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.font = font;
      ctx.textBaseline = "top";

      const yLock = h * ASSEMBLE_AT;
      const scroll = t * SCROLL;
      // row r sits at y = r*CELL_H - scroll + h  (rows enter from below)
      const rowTop = Math.max(0, Math.floor((scroll - h) / CELL_H) - 1);
      const rowBot = Math.ceil((scroll + h) / CELL_H) + 2;
      const hp = holeRef.current;

      for (let r = rowTop; r <= rowBot; r++) {
        const line = lineAt(r);
        if (!line) continue;
        const y = r * CELL_H - scroll + h + 4;
        if (y < -CELL_H) continue;
        // when does this row reach the lock line?
        const tLine = (r * CELL_H + h + 4 - yLock) / SCROLL;

        for (let c = 0; c < line.length; c++) {
          const ch = line[c];
          if (ch === " ") continue;
          const tx = x0 + c * charW;
          const ty = y;
          const tArrive = tLine + c * STAGGER + hash01(c, r, 1) * JITTER;
          const p = clamp01((t - tArrive + FLIGHT) / FLIGHT); // 0 not launched -> 1 landed
          if (p <= 0) continue;

          // clearing around the centre label (measured at the resting spot)
          let holeK = 1;
          if (hp) {
            const dx = tx + charW / 2 - w / 2;
            const dy = ty + CELL_H / 2 - h / 2;
            const f = hp.feather ?? 40;
            const e = Math.sqrt((dx * dx) / (hp.rx * hp.rx) + (dy * dy) / (hp.ry * hp.ry));
            if (e <= 1) continue;
            holeK = smooth(clamp01(((e - 1) * Math.min(hp.rx, hp.ry)) / f));
            if (holeK <= 0.02) continue;
          }

          let gx = tx;
          let gy = ty;
          let grey: number;
          let alpha: number;
          if (p < 1) {
            // in flight: from a scattered start point (biased below / to the sides),
            // easing into the slot along a slight arc
            const a = hash01(c, r, 2) * Math.PI * 2;
            const dist = 70 + hash01(c, r, 3) * 190;
            const sx = Math.cos(a) * dist * 1.4;
            const sy = Math.abs(Math.sin(a)) * dist * 0.8 + 30; // start below the slot
            const k = easeOut(p);
            gx = tx + sx * (1 - k);
            gy = ty + sy * (1 - k) - Math.sin(p * Math.PI) * 10 * hash01(c, r, 4);
            grey = 140;
            alpha = 0.15 + 0.75 * k;
          } else {
            // landed: brief darker flash, then settle; the block fades as it rises
            const since = t - tArrive;
            const flash = clamp01(1 - since / 0.7);
            const age = clamp01((yLock - ty) / (yLock + 10)); // 0 at lock line -> 1 at top
            grey = Math.round(163 - 60 * flash + 40 * age);
            alpha = (0.9 - 0.45 * age) + 0.1 * flash;
          }
          ctx.fillStyle = `rgba(${grey},${grey},${grey},${(alpha * holeK).toFixed(3)})`;
          ctx.fillText(ch, gx, gy);
        }
      }

      // faint lock line so the "assembly point" reads
      ctx.fillStyle = "rgba(23,23,23,0.07)";
      ctx.fillRect(x0 - 8, Math.round(yLock + CELL_H + 4), cols * charW + 16, 1);

      // soft vignette so the block melts into the card edges
      const gv = ctx.createLinearGradient(0, 0, 0, h);
      gv.addColorStop(0, "rgba(255,255,255,0.7)");
      gv.addColorStop(0.2, "rgba(255,255,255,0)");
      gv.addColorStop(0.9, "rgba(255,255,255,0)");
      gv.addColorStop(1, "rgba(255,255,255,0.5)");
      ctx.fillStyle = gv;
      ctx.fillRect(0, 0, w, h);
      const gh = ctx.createLinearGradient(0, 0, w, 0);
      gh.addColorStop(0, "rgba(255,255,255,0.7)");
      gh.addColorStop(0.08, "rgba(255,255,255,0)");
      gh.addColorStop(0.92, "rgba(255,255,255,0)");
      gh.addColorStop(1, "rgba(255,255,255,0.7)");
      ctx.fillStyle = gh;
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
    <canvas ref={canvasRef} aria-hidden className={`block h-full w-full ${className}`} />
  );
}
