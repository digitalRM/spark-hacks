"use client";

import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import JsonTree from "@/components/JsonTree";
import QueryBrief, { SearchedAcross } from "@/components/QueryBrief";
import NotchedPanel from "@/components/NotchedPanel";
import BackgroundLines from "@/components/BackgroundLines";
import BackgroundText from "@/components/BackgroundText";
import FlowLines from "@/components/FlowLines";
import TextShader from "@/components/TextShader";
import ThinkingLabel from "@/components/ThinkingLabel";
import AutoHeight from "@/components/AutoHeight";
import { EXAMPLE_QUERIES, type JsonValue } from "@/lib/dummyBql";
import type { BqlQuery } from "@/lib/bql";
import { compileQuery } from "@/lib/compile";
import { runQuery } from "@/lib/runQuery";
import type { RunResponse } from "@/lib/results";
import ResultsList from "@/components/ResultsList";
import { useTypewriter } from "@/lib/useTypewriter";

/** how long the circular reveal takes to clear the text shader */
const REVEAL_MS = 1100;

/** fake "run" time in ms; override w/ `?run=3000` while developing */
function runDurationMs() {
  if (typeof window === "undefined") return 10_000;
  const v = Number(new URLSearchParams(window.location.search).get("run"));
  return Number.isFinite(v) && v > 0 ? v : 10_000;
}

type View = "query" | "tree" | "json" | "bql";
const VIEWS: { id: View; label: string }[] = [
  { id: "query", label: "Summary" },
  { id: "tree", label: "Tree" },
  { id: "json", label: "JSON" },
  { id: "bql", label: "BQL" },
];

export default function Home() {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [compiling, setCompiling] = useState(false);
  const [ast, setAst] = useState<BqlQuery | null>(null);
  const [bql, setBql] = useState("");
  const [compilerMeta, setCompilerMeta] = useState<{
    schema: string;
    version: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("query");
  // result phase: once we have a plan it "runs" for a bit, then shows a result
  const [resultPhase, setResultPhase] = useState<
    "idle" | "running" | "revealing" | "done"
  >("idle");
  const [results, setResults] = useState<RunResponse | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  // bumped per search so the run re-fires even if the ast object is identical
  const [run, setRun] = useState(0);
  useEffect(() => {
    if (!ast) return;
    const ctrl = new AbortController();
    let t: number | undefined;
    // real runtime if NEXT_PUBLIC_RUNTIME_URL is set; dummy results after the
    // placeholder run time otherwise
    runQuery(ast, { signal: ctrl.signal, fallbackMs: runDurationMs() })
      .then((res) => {
        setResults(res);
        setResultPhase("revealing");
        // reveal wipe (REVEAL_MS) + a beat for "done!" to fade, then show results
        t = window.setTimeout(() => setResultPhase("done"), REVEAL_MS + 500);
      })
      .catch((e) => {
        if (e?.name === "AbortError") return;
        setRunError(e instanceof Error ? e.message : "The search failed.");
        setResultPhase("done");
      });
    return () => {
      ctrl.abort();
      window.clearTimeout(t);
    };
  }, [ast, run]);
  // once the title finishes shrinking into the header, pin a fixed copy
  // so it stays put while results scroll underneath
  const [pinned, setPinned] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // animated placeholder; pauses while theres text in the box, resumes when empty
  // again (incl. after a search was sent)
  const typewriter = useTypewriter(EXAMPLE_QUERIES, {
    active: query.length === 0,
  });
  const placeholder = typewriter.text + (typewriter.cursor ? "|" : "");

  async function compile() {
    const text = query.trim();
    if (!text || compiling) return;
    setSubmitted(true);
    setCompiling(true);
    setAst(null);
    setBql("");
    setCompilerMeta(null);
    setError(null);
    setResultPhase("idle");
    setResults(null);
    setRunError(null);
    try {
      const result = await compileQuery(text);
      setAst(result.query);
      setBql(result.bql);
      setCompilerMeta({ schema: result.schema, version: result.bql_version });
      setResultPhase("running");
      setRun((r) => r + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't compile that query.");
    } finally {
      setCompiling(false);
    }
  }

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    compile();
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      compile();
    }
  }

  return (
    <main className="flex flex-1 flex-col px-4 py-8">
      {/* lines flowing into the input from the left and out the right; gone on submit */}
      {/* faint legal-text texture behind everything */}
      <BackgroundText roam={submitted ? "sides" : "landing"} />
      <BackgroundLines active={!submitted} targetRef={inputRef} />

      {/* pinned title: sits exactly where the in-flow title landed after the shrink,
          on a white -> transparent gradient so scrolled results fade out under it */}
      {pinned && (
        <div
          aria-hidden
          className="pointer-events-none fixed inset-x-0 top-0 z-[200] bg-gradient-to-b from-white from-60% to-white/0 px-4 pb-8 pt-8 text-center"
        >
          <span
            className="block font-semibold tracking-tighter"
            style={{ fontSize: "1.5rem" }}
          >
            Amicus
          </span>
        </div>
      )}

      {/* top spacer: shrinks to 0 on submit so the query box slides up */}
      <div
        aria-hidden
        className="transition-[flex-grow] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)]"
        style={{ flexGrow: submitted ? 0 : 1 }}
      />

      <div className="mx-auto flex w-full flex-col gap-6">
        <section className="flex flex-col gap-4 max-w-3xl mx-auto w-full">
          <div className="text-center" data-bg-clear>
            <h1
              className="font-semibold tracking-tighter transition-[font-size] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)]"
              style={{
                fontSize: submitted ? "1.5rem" : "2.5rem",
                // keep taking up space so layout dosnt shift once the fixed copy takes over
                visibility: pinned ? "hidden" : "visible",
              }}
              onTransitionEnd={(e) => {
                if (e.propertyName === "font-size") setPinned(submitted);
              }}
            >
              Amicus
            </h1>
            <p
              className="overflow-hidden text-sm text-neutral-500 transition-all duration-500 mb-3"
              style={{
                maxHeight: submitted ? 0 : "2rem",
                opacity: submitted ? 0 : 1,
                marginTop: submitted ? 0 : "0.5rem",
              }}
            >
              Describe the cases you&apos;re looking for in plain English.
            </p>
          </div>

          <form
            data-bg-clear
            onSubmit={handleSubmit}
            className="flex flex-col gap-3 rounded-2xl focus-within:border-neutral-400 z-100 relative"
          >
            <textarea
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleKeyDown}
              rows={submitted ? 2 : 3}
              placeholder={placeholder}
              aria-label="Legal query"
              className="w-[100.25%] resize-none text-base leading-relaxed outline-none placeholder:text-neutral-400 border border-neutral-200 rounded-[18px] -mx-px -mt-px p-4 bg-white"
            />
            <div className="flex items-center justify-between">
              <span className="text-xs text-neutral-400">
                <kbd className="rounded-md tracking-tighter bg-neutral-100 px-1.5 py-0.5 text-xs font-medium text-neutral-500">Enter</kbd> to search · <kbd className="rounded-md tracking-tighter bg-neutral-100 px-1.5 py-0.5 text-xs font-medium text-neutral-500">Shift</kbd> + <kbd className="rounded-md tracking-tighter bg-neutral-100 px-1.5 py-0.5 text-xs font-medium text-neutral-500">Enter</kbd> for a new line
              </span>
              <button
                type="submit"
                disabled={!query.trim() || compiling}
                className="rounded-xl bg-neutral-900 px-3 py-1.5 tracking-tight text-sm font-medium text-white transition-opacity disabled:opacity-40"
              >
                {compiling ? "Searching..." : "Search"}
              </button>
            </div>
          </form>
        </section>

        {submitted && (
          <div data-bg-clear className="max-w-4xl mx-auto w-full">
          <NotchedPanel
            className="animate-rise-in"
            title="Compiled Search"
            actions={
              <div
                role="tablist"
                className="flex max-w-full overflow-x-auto rounded-xl bg-neutral-100 p-1 text-sm"
              >
                {VIEWS.map((v) => (
                  <button
                    key={v.id}
                    role="tab"
                    aria-selected={view === v.id}
                    onClick={() => setView(v.id)}
                    className={
                      "whitespace-nowrap rounded-lg px-3 py-1 transition-colors " +
                      (view === v.id
                        ? "bg-white font-medium"
                        : "text-neutral-500 hover:text-neutral-900")
                    }
                  >
                    {v.label}
                  </button>
                ))}
              </div>
            }
          >
            {/* panel mounts once; only the body cross-fades loading -> ready */}
            {error ? (
              <p className="rounded-xl border border-neutral-200 py-8 text-center text-sm text-rose-600 rounded-tr-lg">
                {error}
              </p>
            ) : !ast ? (
              <p className="rounded-xl border border-neutral-200 py-8 text-center text-sm text-neutral-400 -m-1.5 -mt-3">
                Compiling your query…
              </p>
            ) : (
              <div key="ready" className="animate-fade-in">
                {compilerMeta && (
                  <p className="mb-3 text-[11px] text-neutral-400">
                    natural language → JSON → BQL · schema {compilerMeta.schema} · AST v{compilerMeta.version}
                  </p>
                )}
                {view === "query" ? (
                  <QueryBrief query={ast} />
                ) : view === "json" ? (
                  <pre className="overflow-x-auto rounded-xl bg-neutral-50 p-4 font-mono text-[13px] leading-relaxed text-neutral-800">
                    {JSON.stringify(ast, null, 2)}
                  </pre>
                ) : view === "bql" ? (
                  <pre className="overflow-x-auto whitespace-pre-wrap rounded-xl bg-neutral-950 p-4 font-mono text-[13px] leading-relaxed text-neutral-100">
                    {bql}
                  </pre>
                ) : (
                  <JsonTree data={ast as unknown as JsonValue} />
                )}
              </div>
            )}
          </NotchedPanel>
          </div>
        )}

        {/* between the cards: sources on the left; right ~28% has lines carrying
            colored pulses from the plan down into the result while it runs */}
        {ast && (
          <div
            data-bg-clear
            className="animate-fade-in max-w-4xl mx-auto w-full flex items-stretch -my-6"
          >
            <div className="flex-1 px-5 py-6">
              {view === "query" && (
                <SearchedAcross query={ast} done={resultPhase === "done"} />
              )}
            </div>
            <div className="relative w-[28%] min-h-[120px] mr-6">
              <FlowLines
                active={resultPhase === "running"}
                className="absolute inset-0 h-full w-full"
              />
            </div>
          </div>
        )}

        {/* search result: shows up once a plan exists; "runs", then shows the result */}
        {ast && resultPhase !== "idle" && (
          <section
            data-bg-clear
            className="animate-rise-in max-w-4xl mx-auto w-full overflow-hidden rounded-[22px] border border-neutral-200 bg-white -mt-4"
          >
            {/* height eases between the tall thinking state and the result */}
            <AutoHeight>
              {resultPhase === "running" || resultPhase === "revealing" ? (
                // empty state: legal text as a shader field while the plan runs;
                // on "revealing" a white circle wipes it away from the center
                <div className="relative h-[350px]">
                  <TextShader
                    active
                    hole={{ rx: 150, ry: 34, feather: 56 }}
                    reveal={resultPhase === "revealing"}
                    revealMs={REVEAL_MS}
                    className="absolute -inset-1"
                  />
                  <p className="pointer-events-none absolute inset-0 flex items-center justify-center">
                    <ThinkingLabel done={resultPhase === "revealing"} />
                  </p>
                </div>
              ) : runError ? (
                <p className="animate-fade-in p-4 text-center text-sm text-rose-600">
                  {runError}
                </p>
              ) : results ? (
                <div className="animate-fade-in">
                  <ResultsList response={results} />
                </div>
              ) : null}
            </AutoHeight>
          </section>
        )}
      </div>

      {/* bottom spacer keeps the box centered before submit */}
      <div aria-hidden className="flex-1" />
    </main>
  );
}
