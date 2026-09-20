"""Detached OTLP sender for platforms without fork().

``send_span_async`` spawns ``python -m core.sender <envelope.json>`` when it cannot
double-fork. The envelope holds the span payload and, optionally, a delivery-gated
follow-up as an importable reference::

    {"payload": {...}, "on_success": {"callable": "pkg.mod:func", "kwargs": {...}}}

The file is removed as soon as it is read so a crash mid-send cannot leave it to
be replayed. Only callables under our own packages are honoured: the envelope is
written by the hook that spawned this process, but a stray file in the temp dir
must not turn into arbitrary code execution.
"""

from __future__ import annotations

import importlib
import json
import os
import sys

_ALLOWED_CALLABLE_PREFIXES = ("core.", "tracing.")


def _load_envelope(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            envelope = json.load(fh)
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return envelope if isinstance(envelope, dict) else None


def _resolve_callable(ref: object):
    if not isinstance(ref, dict):
        return None, {}
    target = ref.get("callable")
    kwargs = ref.get("kwargs") or {}
    if not isinstance(target, str) or ":" not in target or not isinstance(kwargs, dict):
        return None, {}
    module_name, _, func_name = target.partition(":")
    if not module_name.startswith(_ALLOWED_CALLABLE_PREFIXES):
        return None, {}
    try:
        func = getattr(importlib.import_module(module_name), func_name)
    except (ImportError, AttributeError):
        return None, {}
    return (func if callable(func) else None), kwargs


def deliver(envelope: dict) -> bool:
    from core.common import error, send_span

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return False
    ok = send_span(payload)
    if ok is False:
        return False
    func, kwargs = _resolve_callable(envelope.get("on_success"))
    if func is not None:
        try:
            func(**kwargs)
        except Exception as e:
            error(f"on_success follow-up failed: {e}")
    return True


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return 2
    envelope = _load_envelope(args[0])
    if envelope is None:
        return 1
    try:
        return 0 if deliver(envelope) else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
