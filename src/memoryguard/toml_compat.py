"""Unified TOML loading for Python 3.10+.

``tomllib`` entered the standard library in Python 3.11.  On Python 3.10 we
fall back to the ``tomli`` backport, which is declared as a conditional
dependency in ``pyproject.toml``.  Production and test code should import
this module instead of ``tomllib`` directly so the 3.10 contract holds.
"""
from __future__ import annotations

try:
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as _tomllib  # type: ignore[no-redef]

load = _tomllib.load
loads = _tomllib.loads
TOMLDecodeError = _tomllib.TOMLDecodeError

__all__ = ["load", "loads", "TOMLDecodeError"]