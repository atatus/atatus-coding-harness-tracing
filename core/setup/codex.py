#!/usr/bin/env python3
"""Atatus Codex Tracing - Interactive Setup.

Entry point for ``atatus-setup-codex``. The heavy lifting lives in
``tracing/codex/install.py``; this module is a thin shim for the
``atatus-setup-codex`` console script.

It used to carry a second, complete wizard of its own, with its own
existing-config branch and its own ``~/.codex/atatus-env.sh`` contents. That
meant codex had two install paths that asked different questions and wrote
different env files depending on whether you came via the shell router or the
console script. Delegating is what keeps there being one.
"""

from __future__ import annotations

import sys

from tracing.codex import install as _install_mod


def install(with_skills: bool = False) -> None:
    """Delegate to tracing/codex/install.py install()."""
    _install_mod.install(with_skills=with_skills)


def uninstall() -> None:
    """Delegate to tracing/codex/install.py uninstall()."""
    _install_mod.uninstall()


def main() -> None:
    """Entry point for atatus-setup-codex."""
    try:
        _run()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        sys.exit(1)


def _run() -> None:
    """Delegate to the install module in tracing/codex/."""
    _install_mod.install(with_skills=False)


if __name__ == "__main__":
    main()
