#!/usr/bin/env python3
"""Run the hosted NL→JSON→BQL compiler against recurring constraint combinations.

Usage:
    .venv/bin/python scripts/eval_compiler.py
    .venv/bin/python scripts/eval_compiler.py --cached

This is intentionally an integration eval, not a unit test: it calls the configured
NVIDIA model, validates/typechecks every result, and checks a few semantic invariants.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from query_language import compiler, schema
from query_language.bridge import registry_to_schema
from query_language.typechecker import typecheck


@dataclass(frozen=True)
class Case:
    question: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...] = ()


CASES = (
    Case(
        "6th Circuit securities-fraud opinions since 2022 that reversed a motion to dismiss.",
        ('jurisdiction = "ca6"', 'date_issued >= "2022-01-01"',
         "securities fraud", "reversed a motion to dismiss"),
        ("2022-12-31", "proceeding_type"),
    ),
    Case(
        "Find 6th Circuit cases that went up to the Supreme Court in 2019.",
        ('jurisdiction = "ca6"', 'between "2019-01-01" and "2019-12-31"',
         "supreme court"),
    ),
    Case(
        "Sixth Circuit abortion opinions from 2019 through 2021 reviewed by the Supreme Court.",
        ('jurisdiction = "ca6"', 'between "2019-01-01" and "2021-12-31"',
         "abortion", "supreme court"),
        ("proceeding_type",),
    ),
    Case(
        "Second Circuit securities-fraud opinions since 2010 that reversed a motion to dismiss.",
        ('jurisdiction = "ca2"', 'date_issued >= "2010-01-01"',
         "securities fraud", "reversed a motion to dismiss"),
        ("2010-12-31", "proceeding_type"),
    ),
    Case(
        "2019 Ninth Circuit qualified immunity opinions.",
        ('jurisdiction = "ca9"', 'between "2019-01-01" and "2019-12-31"',
         "qualified immunity"),
    ),
    Case(
        "Supreme Court review of Fifth Circuit voting-rights cases after 2020.",
        ('jurisdiction = "ca5"', 'date_issued > "2020-12-31"',
         "voting rights", "supreme court"),
    ),
    Case(
        "Ninth Circuit opinions before 2020 about excessive force.",
        ('jurisdiction = "ca9"', 'date_issued < "2020-01-01"', "excessive force"),
        ('between "2020-01-01"',),
    ),
    Case(
        "Third Circuit cases from 2018 onwards but not after 2022 involving voting access.",
        ('jurisdiction = "ca3"', 'between "2018-01-01" and "2022-12-31"',
         "voting access"),
    ),
    Case(
        "Eleventh Circuit opinions from 2017 onwards discussing antitrust standing.",
        ('jurisdiction = "ca11"', 'date_issued >= "2017-01-01"',
         "antitrust standing"),
        ("2017-12-31",),
    ),
    Case(
        "First Circuit opinions reviewed by SCOTUS before 2020 involving habeas relief.",
        ('jurisdiction = "ca1"', 'date_issued < "2020-01-01"',
         "habeas relief", "supreme court"),
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cached", action="store_true", help="allow compiler cache hits")
    args = parser.parse_args()

    registry = schema.load("dataform")
    structural = registry_to_schema(registry)
    failures = 0
    for index, case in enumerate(CASES, 1):
        result = compiler.compile_question(
            case.question, registry=registry, use_cache=args.cached,
        )
        problems: list[str] = []
        if not result.ok or result.ast is None:
            problems.append(result.message())
        else:
            try:
                typecheck(result.ast, structural)
            except Exception as error:
                problems.append(f"typecheck: {error}")
            rendered = result.printed.lower()
            problems.extend(f"missing {text!r}" for text in case.required
                            if text.lower() not in rendered)
            problems.extend(f"unexpected {text!r}" for text in case.forbidden
                            if text.lower() in rendered)

        status = "PASS" if not problems else "FAIL"
        failures += bool(problems)
        print(f"{status} {index:02d}  attempts={len(result.attempts)}  "
              f"ms={result.latency_ms:.0f}  {case.question}")
        for problem in problems:
            print(f"         {problem}")
        if result.printed:
            print("         " + result.printed.replace("\n", " | "))

    print(f"\n{len(CASES) - failures}/{len(CASES)} compiler evals passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
