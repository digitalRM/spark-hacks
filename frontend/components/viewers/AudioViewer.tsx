"use client";

import { useEffect, useRef, useState } from "react";
import { formatTime, type ResultFile } from "@/lib/results";

/**
 * Compact audio player: play/pause, scrubber with the match position marked,
 * elapsed / total time, and a "jump to match" affordance when a timestamp is
 * known. Streams straight from the file URL via a hidden <audio>.
 */
export default function AudioViewer({ file }: { file: ResultFile }) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const a = audioRef.current;
    if (!a) return;
    const onTime = () => setTime(a.currentTime);
    const onMeta = () => {
      setDuration(a.duration);
      if (file.timestamp != null) a.currentTime = Math.max(0, file.timestamp - 1);
    };
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onErr = () => setError("Couldn't load this audio.");
    a.addEventListener("timeupdate", onTime);
    a.addEventListener("loadedmetadata", onMeta);
    a.addEventListener("play", onPlay);
    a.addEventListener("pause", onPause);
    a.addEventListener("ended", onPause);
    a.addEventListener("error", onErr);
    return () => {
      a.removeEventListener("timeupdate", onTime);
      a.removeEventListener("loadedmetadata", onMeta);
      a.removeEventListener("play", onPlay);
      a.removeEventListener("pause", onPause);
      a.removeEventListener("ended", onPause);
      a.removeEventListener("error", onErr);
    };
  }, [file.timestamp]);

  const toggle = () => {
    const a = audioRef.current;
    if (!a) return;
    if (a.paused) void a.play().catch(() => setError("Playback was blocked."));
    else a.pause();
  };

  const seekTo = (sec: number) => {
    const a = audioRef.current;
    if (!a) return;
    a.currentTime = Math.min(Math.max(0, sec), duration || sec);
    setTime(a.currentTime);
  };

  const pct = duration ? (time / duration) * 100 : 0;
  const markPct =
    file.timestamp != null && duration ? (file.timestamp / duration) * 100 : null;

  return (
    <div className="flex flex-col gap-3">
      <audio ref={audioRef} src={file.url} preload="metadata" />

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={toggle}
          aria-label={playing ? "Pause" : "Play"}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-neutral-900 text-white transition-transform hover:scale-105 active:scale-95"
        >
          {playing ? (
            <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
              <rect x="2" y="1" width="3.5" height="12" rx="1" />
              <rect x="8.5" y="1" width="3.5" height="12" rx="1" />
            </svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
              <path d="M3 1.5v11l9-5.5z" />
            </svg>
          )}
        </button>

        {/* scrubber */}
        <div className="relative flex-1">
          <input
            type="range"
            min={0}
            max={duration || 0}
            step={0.05}
            value={Math.min(time, duration || 0)}
            onChange={(e) => seekTo(Number(e.target.value))}
            aria-label="Seek"
            className="audio-range w-full"
            style={{ ["--pct" as string]: `${pct}%` }}
          />
          {markPct != null && (
            <span
              title="Match"
              className="pointer-events-none absolute top-1/2 h-3 w-0.5 -translate-y-1/2 rounded bg-violet-500"
              style={{ left: `calc(${markPct}% - 1px)` }}
            />
          )}
        </div>

        <span className="w-[86px] shrink-0 text-right font-mono text-xs tabular-nums text-neutral-500">
          {formatTime(time)} / {formatTime(duration)}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-neutral-500">
        {file.timestamp != null && (
          <button
            type="button"
            onClick={() => seekTo(Math.max(0, file.timestamp! - 1))}
            className="rounded-md bg-violet-50 px-2 py-0.5 font-medium text-violet-700 hover:bg-violet-100"
          >
            Jump to match · {formatTime(file.timestamp)}
          </button>
        )}
        {file.snippet && (
          <span className="italic text-neutral-500">“{file.snippet}”</span>
        )}
        {error && <span className="text-rose-600">{error}</span>}
      </div>
    </div>
  );
}
