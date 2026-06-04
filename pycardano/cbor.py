"""Centralized cbor2 import module."""

import cbor2  # noqa: F401

try:
    from cbor2 import frozendict as FrozenDict  # noqa: F401
except ImportError:  # pragma: no cover - Python 3.15+ removed frozendict
    FrozenDict = dict  # type: ignore
