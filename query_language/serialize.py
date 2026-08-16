"""Compatibility helpers for the canonical BQL AST v2 serializer.

The GitHub checkpoint introduced this module against a removed ``grammar.py``
and an older snake_case wire shape. Keep its public helper names for callers,
but route every node through :mod:`query_language.serde`, the sole JSON contract
used by the compiler, frontend, and optimizer.
"""
from __future__ import annotations

import json
from typing import Any

from .serde import encode


def expression_to_dict(expression: Any) -> Any:
    return encode(expression)


def condition_to_dict(condition: Any) -> dict[str, Any]:
    return encode(condition)


def source_to_dict(source: Any) -> dict[str, Any]:
    return encode(source)


def query_to_dict(query: Any) -> dict[str, Any]:
    return encode(query)


def to_json(query: Any, **kwargs: Any) -> str:
    return json.dumps(query_to_dict(query), **kwargs)
