#!/usr/bin/env python3
"""Atatus Kiro Tracing - Interactive Setup.

Entry point for ``atatus-setup-kiro``. The heavy lifting lives in
``tracing/kiro/install.py``; this module is a thin shim for the
``atatus-setup-kiro`` console script.
"""

from __future__ import annotations

import sys

from tracing.kiro import install as _install_mod


def main() -> None:
    """Entry point for atatus-setup-kiro."""
    try:
        _run()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        sys.exit(1)


def _run() -> None:
    """Delegate to the install module in tracing/kiro/."""
    _install_mod.install(with_skills=False)


if __name__ == "__main__":
    main()
