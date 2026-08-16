"use client";

import { FormEvent, KeyboardEvent, useRef, useState } from "react";
import JsonTree from "@/components/JsonTree";
import QueryPlan from "@/components/QueryPlan";
import BackgroundLines from "@/components/BackgroundLines";
import { DUMMY_BQL_AST, EXAMPLE_QUERIES, type BqlQuery } from "@/lib/dummyBql";
import { useTypewriter } from "@/lib/useTypewriter";

type View = "query" | "tree" | "json";
const VIEWS: { id: View; label: string }[] = [
  { id: "query", label: "Query" },
  { id: "tree", label: "Tree" },
  { id: "json", label: "JSON" },
];

export default function Home() {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [compiling, setCompiling] = useState(false);
  const [ast, setAst] = useState<BqlQuery | null>(null);
  const [view, setView] = useState<View>("query");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Animated placeholder; freezes as soon as the user starts typing.
  const typewriter = useTypewriter(EXAMPLE_QUERIES, {
    active: query.length === 0 && !submitted,
  });
  const placeholder = typewriter.text + (typewriter.cursor ? "|" : "");

  function compile() {
    const text = query.trim();
    if (!text || compiling) return;
    setSubmitted(true);
    setCompiling(true);
    setAst(null);
    // TODO: replace with a call to the BQL compiler. Dummy AST for now.
    window.setTimeout(() => {
      setAst(DUMMY_BQL_AST);
      setCompiling(false);
    }, 600);
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
      {/* Lines flowing into the input from the left and out to the right; vanish on submit. */}
      <BackgroundLines active={!submitted} targetRef={inputRef} />

      {/* Top spacer: shrinks to 0 on submit, sliding the query box upward. */}
      <div
        aria-hidden
        className="transition-[flex-grow] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)]"
        style={{ flexGrow: submitted ? 0 : 1 }}
      />

      <div className="mx-auto flex w-full flex-col gap-6">
        <section className="flex flex-col gap-4 max-w-3xl mx-auto w-full">
          <div className="text-center">
            <h1
              className="font-semibold tracking-tighter transition-[font-size] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)]"
              style={{ fontSize: submitted ? "1.5rem" : "2.5rem" }}
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
          <section
            key={ast ? "ready" : "loading"}
            className="animate-rise-in rounded-[22px] border border-neutral-200 bg-white p-4 max-w-4xl mx-auto w-full"
          >
            <header className="mb-4 flex items-center justify-between gap-4">
              <h2 className="text-sm font-semibold">Compiled BQL</h2>
              <div
                role="tablist"
                className="flex rounded-xl bg-neutral-100 p-1 text-sm"
              >
                {VIEWS.map((v) => (
                  <button
                    key={v.id}
                    role="tab"
                    aria-selected={view === v.id}
                    onClick={() => setView(v.id)}
                    className={
                      "rounded-lg px-3 py-1 transition-colors " +
                      (view === v.id
                        ? "bg-white font-medium"
                        : "text-neutral-500 hover:text-neutral-900")
                    }
                  >
                    {v.label}
                  </button>
                ))}
              </div>
            </header>

            {!ast ? (
              <p className="py-8 text-center text-sm text-neutral-400">
                Compiling your query…
              </p>
            ) : view === "query" ? (
              <QueryPlan query={ast} />
            ) : view === "json" ? (
              <pre className="overflow-x-auto rounded-xl bg-neutral-50 p-4 font-mono text-[13px] leading-relaxed text-neutral-800">
                {JSON.stringify(ast, null, 2)}
              </pre>
            ) : (
              <JsonTree data={ast} />
            )}
          </section>
        )}
      </div>

      {/* Bottom spacer keeps the box centered before submit. */}
      <div aria-hidden className="flex-1" />
    </main>
  );
}
