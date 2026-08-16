"use client";

import { useEffect, useRef } from "react";
import { LEGAL_PASSAGES } from "@/lib/legalCorpus";

type Props = {
  /** opacity of fully revealed glyphs (0..1) */
  strength?: number;
  /** half-width px of the hover reveal, wide ellipse ~0.4x as tall */
  hoverRadius?: number;
  /** seconds a revealed glyph lingers after cursor moves on */
  trail?: number;
  /**
   * auto roamers that reveal text like the cursor but slower/fainter.
   * "landing" = top+bottom bands, "sides" = left/right margins, `null` = off
   */
  roam?: "landing" | "sides" | null;
  /** Half-width (px) of each roamer's reveal (same ellipse shape as the cursor's). */
  roamRadius?: number;
  /** elements matching this get cleared out of the texture (`margin` px gap + `feather` px soft edge) */
  clearSelector?: string;
  margin?: number;
  feather?: number;
  /**
   * svg `<path>`s matching this (viewport coords, eg the flow-line envelopes) get filled
   * and cleared out, `lineMargin` px around + `lineFeather` px soft edge.
   * cutout follows the owning svg's opacity so it dissolves with the lines
   */
  areaSelector?: string;
  lineMargin?: number;
  lineFeather?: number;
  className?: string;
};

const FONT_PX = 10.5;
const LINE_H = 16;
const COL_CHARS = 58; // chars per column
const GUTTER_CHARS = 5;
const MASK_SCALE = 1 / 8; // cutout mask rendered at 1/8 res
const FEATHER_STEPS = 8;
const FRAME_MS = 33; // ~30 fps
const ELLIPSE = 0.4; // reveal height / width
const NOISE = 0.5; // how much per-glyph noise roughens the reveal edge

/** cheap deterministic hash -> [0,1), stable per glyph cell accross frames */
function hash01(a: number, b: number) {
  let h = (a * 374761393 + b * 668265263) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

function roundRectPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
) {
  const rr = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

/**
 * lays passages out as reporter-style columns: cite heading, text greedy-wrapped
 * to `cols`, blank line, next. fills `lines` rows, starts `skip` passages in so
 * adjacent columns dont repeat
 */
function layoutColumn(cols: number, lines: number, skip: number): string[] {
  const out: string[] = [];
  let i = skip % LEGAL_PASSAGES.length;
  while (out.length < lines) {
    const p = LEGAL_PASSAGES[i];
    i = (i + 1) % LEGAL_PASSAGES.length;
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
 * full-viewport bg: faint columns of legal text, invisible untill the cursor passes over.
 * reveal is per glyph - a wide ellipse of "heat" gets stamped under the cursor, each cell
 * cools over `trail` s, and per-glyph noise makes chars wink in/out individually instead
 * of a smooth disc. page content (`clearSelector`) + flow-line fans (`areaSelector`) get
 * cleared out w/ margin + feathered edge.
 *
 * glyphs rasterised once per resize to an offscreen canvas. each frame updates a small
 * Float32 heat grid (one per cell), writes it to a cell-res alpha mask, redraws the 1/8-res
 * cutout mask, composites.
 */
export default function BackgroundText({
  strength = 0.16,
  hoverRadius = 190,
  trail = 1.6,
  roam = null,
  roamRadius = 340,
  clearSelector = "[data-bg-clear]",
  margin = 28,
  feather = 44,
  areaSelector = "[data-bg-clear-area]",
  lineMargin = 12,
  lineFeather = 18,
  className = "",
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // read in the frame loop via ref so switching zones doesnt restart the effect (would drop the trail)
  const roamRef = useRef(roam);
  useEffect(() => {
    roamRef.current = roam;
  }, [roam]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const text = document.createElement("canvas"); // rasterised glyphs
    const cells = document.createElement("canvas"); // per-glyph reveal alpha
    const cut = document.createElement("canvas"); // low-res content cutouts
    const tctx = text.getContext("2d");
    const cctx = cells.getContext("2d");
    const kctx = cut.getContext("2d");
    if (!tctx || !cctx || !kctx) return;

    let w = 0;
    let h = 0;
    let dpr = 1;
    let mw = 1;
    let mh = 1;
    // glyph grid: each glyph sits at x = gx0 + col*charW, y = row*LINE_H
    let charW = 6.3;
    let gx0 = 0; // <= 0
    let gcols = 1;
    let grows = 1;
    let heat = new Float32Array(1);
    let noise = new Float32Array(1);
    let img: ImageData | null = null;

    const rasterizeText = () => {
      text.width = canvas.width;
      text.height = canvas.height;
      tctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      tctx.clearRect(0, 0, w, h);
      tctx.font = `${FONT_PX}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      tctx.textBaseline = "top";
      tctx.fillStyle = "#171717";

      charW = tctx.measureText("M").width;
      const colW = COL_CHARS * charW;
      const gutter = GUTTER_CHARS * charW;
      const stride = colW + gutter;
      const nCols = Math.ceil(w / stride) + 1;
      const nLines = Math.ceil(h / LINE_H) + 2;
      // centre the column grid so its symmetric on any width
      const x0 = (w - (nCols * stride - gutter)) / 2;

      for (let c = 0; c < nCols; c++) {
        // stagger vertical start per column so headings dont line up
        const yShift = -((c * 7) % 11) * LINE_H;
        const lines = layoutColumn(COL_CHARS, nLines + 11, c * 3);
        for (let l = 0; l < lines.length; l++) {
          const y = yShift + l * LINE_H;
          if (y > h) break;
          if (y < -LINE_H) continue;
          tctx.fillText(lines[l], x0 + c * stride, y);
        }
      }

      // cell grid aligned to the glyph grid (stride is a multiple of charW)
      const k = Math.ceil(x0 / charW);
      gx0 = x0 - k * charW;
      gcols = Math.max(1, Math.ceil((w - gx0) / charW));
      grows = Math.max(1, Math.ceil(h / LINE_H) + 1);
      heat = new Float32Array(gcols * grows);
      noise = new Float32Array(gcols * grows);
      for (let r = 0; r < grows; r++)
        for (let c = 0; c < gcols; c++) noise[r * gcols + c] = hash01(c, r);
      cells.width = gcols;
      cells.height = grows;
      img = cctx.createImageData(gcols, grows);
    };

    const resize = () => {
      const r = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = r.width;
      h = r.height;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      mw = Math.max(2, Math.round(w * MASK_SCALE));
      mh = Math.max(2, Math.round(h * MASK_SCALE));
      cut.width = mw;
      cut.height = mh;
      rasterizeText();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    // pointer tracking (mouse only). `prev` = where we last stamped so fast moves get
    // interpolated stamps insted of gaps
    const ptr = { x: 0, y: 0, in: false, moved: false };
    const prev = { x: 0, y: 0, valid: false };
    const onMove = (e: PointerEvent) => {
      if (e.pointerType !== "mouse") return;
      ptr.x = e.clientX;
      ptr.y = e.clientY;
      ptr.in = true;
      ptr.moved = true;
    };
    const onLeave = () => {
      ptr.in = false;
      prev.valid = false;
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    document.addEventListener("pointerleave", onLeave);
    window.addEventListener("blur", onLeave);

    let raf = 0;
    let last = -1e9;
    let maxHeat = 0;
    const pathCache = new Map<string, Path2D>();

    /** stamp a wide soft ellipse of heat centred at (x, y) css px */
    const stamp = (x: number, y: number, gain = 1, radius = hoverRadius) => {
      const rx = radius;
      const ry = radius * ELLIPSE;
      const c0 = Math.max(0, Math.floor((x - rx - gx0) / charW));
      const c1 = Math.min(gcols - 1, Math.ceil((x + rx - gx0) / charW));
      const r0 = Math.max(0, Math.floor((y - ry) / LINE_H));
      const r1 = Math.min(grows - 1, Math.ceil((y + ry) / LINE_H));
      for (let r = r0; r <= r1; r++) {
        const dy = ((r + 0.5) * LINE_H - y) / ry;
        for (let c = c0; c <= c1; c++) {
          const dx = (gx0 + (c + 0.5) * charW - x) / rx;
          const d = Math.sqrt(dx * dx + dy * dy);
          if (d >= 1) continue;
          const v = (1 - d * d) * gain; // soft dome, `gain` at centre
          const i = r * gcols + c;
          if (v > heat[i]) heat[i] = v;
        }
      }
      maxHeat = 1;
    };

    const updateHeat = (dt: number) => {
      // cool everything: heat halves every trail/2.3 s (~10% left after `trail`)
      const decay = Math.pow(0.1, dt / trail);
      let mx = 0;
      for (let i = 0; i < heat.length; i++) {
        const v = heat[i] * decay;
        heat[i] = v < 0.004 ? 0 : v;
        if (v > mx) mx = v;
      }
      maxHeat = mx;

      if (ptr.in && ptr.moved) {
        ptr.moved = false;
        if (prev.valid) {
          const dx = ptr.x - prev.x;
          const dy = ptr.y - prev.y;
          const dist = Math.hypot(dx, dy);
          const step = hoverRadius * ELLIPSE * 0.5;
          const n = Math.max(1, Math.ceil(dist / step));
          for (let s = 1; s <= n; s++) stamp(prev.x + (dx * s) / n, prev.y + (dy * s) / n);
        } else {
          stamp(ptr.x, ptr.y);
        }
        prev.x = ptr.x;
        prev.y = ptr.y;
        prev.valid = true;
      }
    };

    // two roamers drift on slow lissajous paths inside their zone (viewport fractions),
    // easing toward the path so a zone change glides them across instead of teleporting
    type Zone = { x0: number; x1: number; y0: number; y1: number };
    const ZONES: Record<"landing" | "sides", [Zone, Zone]> = {
      landing: [
        { x0: 0.04, x1: 0.96, y0: 0.02, y1: 0.16 },
        { x0: 0.04, x1: 0.96, y0: 0.84, y1: 0.98 },
      ],
      sides: [
        { x0: 0.02, x1: 0.16, y0: 0.08, y1: 0.92 },
        { x0: 0.84, x1: 0.98, y0: 0.08, y1: 0.92 },
      ],
    };
    const roamers = [
      { x: -1, y: -1, fx: 0.031, fy: 0.047, px: 0.0, py: 1.7 },
      { x: -1, y: -1, fx: 0.027, fy: 0.041, px: 2.4, py: 0.6 },
    ];
    const updateRoamers = (t: number, dt: number) => {
      const mode = roamRef.current;
      if (!mode) return;
      const zones = ZONES[mode];
      const ease = 1 - Math.pow(0.001, dt / 4); // ~4s to close 99.9% of the gap
      for (let i = 0; i < roamers.length; i++) {
        const R = roamers[i];
        const z = zones[i];
        const cx = (z.x0 + z.x1) / 2;
        const cy = (z.y0 + z.y1) / 2;
        const tx = (cx + ((z.x1 - z.x0) / 2) * Math.sin(2 * Math.PI * R.fx * t + R.px)) * w;
        const ty = (cy + ((z.y1 - z.y0) / 2) * Math.sin(2 * Math.PI * R.fy * t + R.py)) * h;
        if (R.x < 0) {
          R.x = tx;
          R.y = ty;
        } else {
          R.x += (tx - R.x) * ease;
          R.y += (ty - R.y) * ease;
        }
        // larger but fainter than the cursor, "just a little bit"
        stamp(R.x, R.y, 0.6, roamRadius);
      }
    };

    /** heat + per-glyph noise threshold -> per-cell alpha */
    const drawCells = () => {
      if (!img) return;
      const d = img.data;
      const a255 = strength * 255;
      for (let i = 0; i < heat.length; i++) {
        // higher-noise glyphs need more heat to show -> ragged glyph-by-glyph edge,
        // and the trail dissolves char by char instead of dimming uniformly
        let a = (heat[i] - noise[i] * NOISE) / (1 - NOISE);
        if (a <= 0) {
          d[i * 4 + 3] = 0;
          continue;
        }
        if (a > 1) a = 1;
        a = a * a * (3 - 2 * a);
        d[i * 4 + 3] = a * a255;
      }
      cctx.putImageData(img, 0, 0);
    };

    const drawCutouts = () => {
      kctx.setTransform(1, 0, 0, 1, 0, 0);
      kctx.globalCompositeOperation = "source-over";
      kctx.clearRect(0, 0, mw, mh);
      kctx.fillStyle = "#000";
      kctx.fillRect(0, 0, mw, mh);
      kctx.globalCompositeOperation = "destination-out";
      const s = MASK_SCALE;
      const steps = FEATHER_STEPS + 1;
      // per-step alpha so the innermost region ends up ~fully clear
      const stepA = 1 - Math.pow(0.02, 1 / steps);

      // page content: each marked element's viewport rect grown by `margin`, then
      // feathered outward w/ concentric rounded rects
      const els = document.querySelectorAll<HTMLElement>(clearSelector);
      kctx.fillStyle = `rgba(0,0,0,${stepA})`;
      for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        for (let k = 0; k < steps; k++) {
          const grow = margin + feather * (1 - k / FEATHER_STEPS);
          roundRectPath(
            kctx,
            (r.left - grow) * s,
            (r.top - grow) * s,
            (r.width + 2 * grow) * s,
            (r.height + 2 * grow) * s,
            (18 + grow) * s,
          );
          kctx.fill();
        }
      }

      // flow-line fans: fill each tagged svg path then feather outward w/ concentric
      // round-joined strokes. path coords are viewport px so scale the ctx not the data
      const areas = document.querySelectorAll<SVGPathElement>(areaSelector);
      if (areas.length) {
        kctx.lineCap = "round";
        kctx.lineJoin = "round";
        kctx.setTransform(s, 0, 0, s, 0, 0);
        const svgOpacity = new Map<SVGSVGElement, number>();
        for (const p of areas) {
          const svg = p.ownerSVGElement;
          if (!svg) continue;
          let op = svgOpacity.get(svg);
          if (op === undefined) {
            op = parseFloat(getComputedStyle(svg).opacity) || 0;
            svgOpacity.set(svg, op);
          }
          if (op <= 0.01) continue;
          const d = p.getAttribute("d");
          if (!d) continue;
          let path2d = pathCache.get(d);
          if (!path2d) {
            if (pathCache.size > 200) pathCache.clear();
            path2d = new Path2D(d);
            pathCache.set(d, path2d);
          }
          kctx.fillStyle = `rgba(0,0,0,${op})`;
          kctx.fill(path2d);
          kctx.strokeStyle = `rgba(0,0,0,${stepA * op})`;
          for (let k = 0; k < steps; k++) {
            kctx.lineWidth = 2 * (lineMargin + lineFeather * (1 - k / FEATHER_STEPS));
            kctx.stroke(path2d);
          }
        }
        kctx.setTransform(1, 0, 0, 1, 0, 0);
      }
    };

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      if (now - last < FRAME_MS) return;
      if (canvas.width === 0 || canvas.height === 0) return; // not layed out yet
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;

      updateHeat(dt);
      updateRoamers(now / 1000, dt);

      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalCompositeOperation = "source-over";
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (maxHeat <= 0) return; // nothing revealed, keep canvas empty

      drawCells();
      drawCutouts();

      // glyphs x per-glyph reveal (nearest-neighbour, whole chars) x cutouts (smooth)
      ctx.drawImage(text, 0, 0);
      ctx.globalCompositeOperation = "destination-in";
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(
        cells,
        0, 0, gcols, grows,
        gx0 * dpr, 0, gcols * charW * dpr, grows * LINE_H * dpr,
      );
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(cut, 0, 0, mw, mh, 0, 0, canvas.width, canvas.height);
      ctx.globalCompositeOperation = "source-over";
    };
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerleave", onLeave);
      window.removeEventListener("blur", onLeave);
    };
  }, [strength, hoverRadius, trail, roamRadius, clearSelector, margin, feather, areaSelector, lineMargin, lineFeather]);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden
      className={`pointer-events-none fixed inset-0 -z-20 h-full w-full ${className}`}
    />
  );
}
