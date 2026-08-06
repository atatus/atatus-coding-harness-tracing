#!/usr/bin/env python3
"""Atatus Codex Tracing Plugin - Interactive Setup.

Writes config.json, ~/.codex/atatus-env.sh, and ~/.codex/config.toml.

The ``atatus-setup-codex`` entry point calls ``main()`` here, which runs the
legacy interactive wizard.  The new ``tracing/codex/install.py`` module
provides the decomposed ``install()`` / ``uninstall()`` API used by the
shell router.  ``install()`` and ``uninstall()`` below delegate to it.
"""

import os
import sys
from pathlib import Path

from core.common import DEFAULT_OTLP_ENDPOINT
from core.config import get_value, load_config, save_config, set_value
from core.setup import err, info, print_color, prompt_backend, prompt_project_name, prompt_user_id, write_config
from tracing.codex import install as _install_mod


def install(with_skills: bool = False) -> None:
    """Delegate to tracing/codex/install.py install()."""
    _install_mod.install(with_skills=with_skills)


def uninstall() -> None:
    """Delegate to tracing/codex/install.py uninstall()."""
    _install_mod.uninstall()


def _write_env_file(env_path: Path, target: str, credentials: dict, project_name: str = "codex") -> None:
    """Write ~/.codex/atatus-env.sh with export statements."""
    env_path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["# Atatus Codex tracing environment (auto-generated)"]
    lines.append("export ATATUS_TRACE_ENABLED=true")

    lines.append(f'export ATATUS_OTLP_ENDPOINT="{credentials.get("endpoint", DEFAULT_OTLP_ENDPOINT)}"')
    api_key = credentials.get("api_key", "")
    if api_key:
        lines.append(f'export ATATUS_API_KEY="{api_key}"')

    lines.append(f'export ATATUS_PROJECT_NAME="{project_name}"')

    env_path.write_text("\n".join(lines) + "\n")

    # chmod 600
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass  # Windows doesn't support chmod the same way


def main() -> None:
    """Entry point for atatus-setup-codex."""
    try:
        _run()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        sys.exit(1)


def _run() -> None:
    codex_config_dir = Path.home() / ".codex"
    env_file = codex_config_dir / "atatus-env.sh"

    print("")
    print_color("▸ ATATUS Codex Tracing Setup", "green")
    print("")

    # Check for existing config
    config = load_config()
    existing_entry = get_value(config, "harnesses.codex")

    # Project name
    project_name = prompt_project_name("codex")

    if existing_entry:
        target = existing_entry.get("target", "")
        print_color(
            f"Existing config found: target={target} in ~/.atatus/harness/config.json",
            "yellow",
        )
        print("Skipping credential prompts — updating codex harness entry.")
        print("")

        # Update codex harness entry
        set_value(config, "harnesses.codex.project_name", project_name)
        save_config(config)
        info("Updated codex harness in existing config")

        # Write env file from existing config
        endpoint = get_value(config, "harnesses.codex.endpoint") or ""
        api_key = get_value(config, "harnesses.codex.api_key") or ""
        if target != "atatus":
            err(f"Unknown target in config: {target}")
            sys.exit(1)
        creds = {"endpoint": endpoint or DEFAULT_OTLP_ENDPOINT, "api_key": api_key}

        _write_env_file(env_file, target, creds, project_name)
        info(f"Wrote credentials to {env_file}")
    else:
        # No existing config — prompt for backend
        existing_harnesses = config.get("harnesses", {}) if config else {}
        target, credentials = prompt_backend(existing_harnesses=existing_harnesses)
        info(f"Target: Atatus at {credentials['endpoint']}")

        # Write config.json
        write_config(target, credentials, "codex", project_name)
        info("Wrote config to ~/.atatus/harness/config.json")

        # Write env file
        _write_env_file(env_file, target, credentials, project_name)
        info(f"Wrote credentials to {env_file}")

    # Optional: User ID
    user_id = prompt_user_id()
    if user_id:
        config = load_config()
        set_value(config, "user_id", user_id)
        save_config(config)
        info(f"User ID set: {user_id}")

    # Summary
    print("")
    info("Setup complete!")
    print("")
    print("  Configuration:")
    print("    Config file:  ~/.atatus/harness/config.json")
    print(f"    Env file:     {env_file}")
    print("")
    print("  Next steps:")
    print("    Run codex — traces are sent straight to Atatus from the hooks.")
    print("")
    print("  To verify setup: ATATUS_DRY_RUN=true codex")
    print("")


if __name__ == "__main__":
    main()
