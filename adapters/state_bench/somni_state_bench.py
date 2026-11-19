"""Compatibility entry point for the STATE-Bench adapter in evals.state_bench."""

from pathlib import Path as _Path
import sys as _sys

_root = str(_Path(__file__).resolve().parents[2])
if _root not in _sys.path:
    _sys.path.insert(0, _root)

from evals.state_bench import somni_state_bench as _adapter  # noqa: E402

__all__ = [name for name in vars(_adapter) if not name.startswith("_")]


def __getattr__(name):
    return getattr(_adapter, name)


def __dir__():
    return sorted(set(globals()) | set(vars(_adapter)))


if __name__ == "__main__":
    raise SystemExit(_adapter.main())
