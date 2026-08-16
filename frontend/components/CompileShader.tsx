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
const BEAM_SPEED = 55; // px per second the read-head moves down
const READ_BAND = 26; // px just behind the head where glyphs are still resolving
const SCRAMBLE = "abcdefghijklmnopqrstuvwxyz0123456789§¶.,;:'";
const TOKEN_FRAC = 0.22; // share of words that get a highlight once read

const smooth = (t: number) => t * t * (3 - 2 * t);
const clamp01 = (t: number) => (t < 0 ? 0 : t > 1 ? 1 : t);

/** cheap deterministic hash -> [0,1) */
function hash01(a: number, b: number, c = 0) {
  let h = (a * 374761393 + b * 668265263 + c * 2246822519) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

/** passages as one stream of wrapped lines: cite heading, text, blank line, next */
function layout(cols: number, lines: number): string[] {
  const out: string[] = [];
  let i = 0;
  while (out.length < lines) {
    const p = LEGAL_PASSAGES[i % LEGAL_PASSAGES.length];
    i++;
    out.push(p.cite.toUpperCase());
    let line = "";
    for (const word of p.text.split(" ")) {
      if (line.length + word.length + (line ? 1 : 0) > cols) {
        out.push(line);
        line = word;
      } else {
        line = line ? `${line} ${word}` : word;
      }
    }
    if (line) out.push(line);
    out.push("");
  }
  return out;
}

/**
 * "compile shader": wrapped legal text being parsed. a read-head sweeps down the block;
 * glyphs ahead of it are faint scrambled noise, they resolve into the real text as it
 * passes (with some words picking up a light token highlight), then settle and slowly
 * fade back to noise before the head comes round again. greys only - deliberately not
 * the blue/violet field used for thinking/results
 */
export default function CompileShader({ active = true, hole, className = "" }: Props) {
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
    let rows = 1;
    let x0 = 0;
    let lines: string[] = [];
    // word spans per row for the token highlights: [start, end) char indices
    let words: [number, number][][] = [];

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
      // a comfortable measure with side padding, centred
      cols = Math.max(20, Math.floor((w - 48) / charW));
      x0 = (w - cols * charW) / 2;
      rows = Math.ceil(h / CELL_H) + 1;
      lines = layout(cols, rows);
      words = lines.map((line) => {
        const spans: [number, number][] = [];
        const re = /\S+/g;
        let m: RegExpExecArray | null;
        while ((m = re.exec(line))) spans.push([m.index, m.index + m[0].length]);
        return spans;
      });
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const t0 = performance.now();
    let raf = 0;

    const frame = (now: number) => {
      const t = reduceMotion ? 3 : (now - t0) / 1000;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.font = font;
      ctx.textBaseline = "top";

      // read-head position; the cycle is a bit longer than the card so the top
      // has time to fade back to noise before the head returns
      const cycle = h + 160;
      const beam = (t * BEAM_SPEED) % cycle;
      const tick = Math.floor(t * 9); // scramble refresh rate
      const hp = holeRef.current;

      for (let r = 0; r < rows; r++) {
        const line = lines[r] ?? "";
        const y = r * CELL_H + 4;
        const cy = y + CELL_H / 2;
        // u: px since the head passed this row (wrapped into the cycle)
        const u = (((beam - cy) % cycle) + cycle) % cycle;
        const ahead = u > cycle - 40; // just about to be read: peak noise
        const reading = u <= READ_BAND;
        // settled text fades from neutral-400 back toward the noise level
        const settle = clamp01((u - READ_BAND) / (cycle * 0.72));

        // token highlights, drawn first so glyphs sit on top
        if (!reading && u > READ_BAND && settle < 0.75) {
          const spans = words[r] ?? [];
          for (let s = 0; s < spans.length; s++) {
            if (hash01(r, s, 7) >= TOKEN_FRAC) continue;
            const [a, b] = spans[s];
            const alpha = 0.06 * (1 - settle / 0.75) * (u < READ_BAND + 60 ? smooth((u - READ_BAND) / 60) : 1);
            ctx.fillStyle = `rgba(23,23,23,${alpha.toFixed(3)})`;
            ctx.fillRect(x0 + a * charW - 1, y - 1, (b - a) * charW + 2, CELL_H - 2);
          }
        }

        for (let c = 0; c < line.length; c++) {
          const real = line[c];
          if (real === " ") continue;
          const px = x0 + c * charW;

          // clearing around the centre label
          let holeK = 1;
          if (hp) {
            const dx = px + charW / 2 - w / 2;
            const dy = cy - h / 2;
            const f = hp.feather ?? 40;
            const e = Math.sqrt((dx * dx) / (hp.rx * hp.rx) + (dy * dy) / (hp.ry * hp.ry));
            if (e <= 1) continue;
            holeK = smooth(clamp01(((e - 1) * Math.min(hp.rx, hp.ry)) / f));
            if (holeK <= 0.02) continue;
          }

          let ch = real;
          let grey: number;
          let alpha: number;
          if (reading) {
            // resolving: flickers between noise and the real glyph, darkest here
            const k = u / READ_BAND; // 0 at the head -> 1 at the back of the band
            const settledYet = hash01(c, r, 3) < k * k;
            ch = settledYet ? real : SCRAMBLE[Math.floor(hash01(c, r, tick) * SCRAMBLE.length)];
            grey = 82; // neutral-600
            alpha = 0.55 + 0.35 * (1 - k);
          } else if (u < cycle * 0.72 + READ_BAND) {
            // read: real text, neutral-400 -> neutral-300 -> faint
            grey = Math.round(163 + (212 - 163) * settle);
            alpha = 0.9 - 0.62 * settle;
          } else {
            // not yet read: sparse light noise, a touch busier right ahead of the head
            const density = ahead ? 0.55 : 0.32;
            if (hash01(c, r, tick >> 1) > density) continue;
            ch = SCRAMBLE[Math.floor(hash01(c, r, tick) * SCRAMBLE.length)];
            grey = 212; // neutral-300
            alpha = ahead ? 0.55 : 0.32;
          }
          ctx.fillStyle = `rgba(${grey},${grey},${grey},${(alpha * holeK).toFixed(3)})`;
          ctx.fillText(ch, px, y);
        }
      }

      // the head itself: a faint horizontal rule with a soft glow band
      const g = ctx.createLinearGradient(0, beam - 18, 0, beam + 6);
      g.addColorStop(0, "rgba(23,23,23,0)");
      g.addColorStop(0.85, "rgba(23,23,23,0.05)");
      g.addColorStop(1, "rgba(23,23,23,0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, beam - 18, w, 24);
      ctx.fillStyle = "rgba(23,23,23,0.16)";
      ctx.fillRect(0, Math.round(beam), w, 1);

      // soft vignette so the block melts into the card edges
      const gv = ctx.createLinearGradient(0, 0, 0, h);
      gv.addColorStop(0, "rgba(255,255,255,0.5)");
      gv.addColorStop(0.14, "rgba(255,255,255,0)");
      gv.addColorStop(0.86, "rgba(255,255,255,0)");
      gv.addColorStop(1, "rgba(255,255,255,0.6)");
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
