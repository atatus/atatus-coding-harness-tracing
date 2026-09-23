from __future__ import annotations

import json
import subprocess
from pathlib import Path

PLUGIN = Path(__file__).parents[3] / "tracing" / "omp" / "plugin" / "atatus-tracing.ts"


def _run_node(script: str) -> dict:
    result = subprocess.run(
        ["node", "--experimental-transform-types", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _instrumented_plugin(tmp_path: Path) -> Path:
    """Copy the real shim with only its process boundary replaced by a sink."""
    source = PLUGIN.read_text(encoding="utf-8")
    marker = "function forward(payload: unknown): void {\n"
    assert source.count(marker) == 1
    source = source.replace(marker, marker + "  globalThis.__atatusPayloads.push(payload)\n  return\n")
    target = tmp_path / "atatus-tracing-instrumented.ts"
    target.write_text(source, encoding="utf-8")
    return target


def _turn_end(plugin: Path, pi_extra: str) -> dict:
    return _run_node(
        f"""
        globalThis.__atatusPayloads = [];
        const mod = await import({json.dumps(plugin.as_uri())});
        const handlers = {{}};
        const pi = {{ on: (name, fn) => {{ handlers[name] = fn; }}, {pi_extra} }};
        mod.default(pi);
        await handlers.turn_end(
          {{ turnIndex: 0, message: {{ role: 'assistant' }}, toolResults: [] }},
          {{ sessionManager: {{ getSessionId: () => 's1' }} }},
        );
        console.log(JSON.stringify({{ payloads: globalThis.__atatusPayloads }}));
        """
    )


def test_thinking_level_is_forwarded_when_the_api_exposes_it(tmp_path: Path) -> None:
    result = _turn_end(_instrumented_plugin(tmp_path), "getThinkingLevel: () => 'xhigh'")
    assert result["payloads"][0]["thinkingLevel"] == "xhigh"


def test_an_api_without_the_method_still_forwards_the_turn(tmp_path: Path) -> None:
    """Older omp builds have no getThinkingLevel; the shim must load on them anyway."""
    result = _turn_end(_instrumented_plugin(tmp_path), "")
    payload = result["payloads"][0]
    assert payload["type"] == "turn_end"
    assert "thinkingLevel" not in payload


def test_a_throwing_probe_does_not_lose_the_turn(tmp_path: Path) -> None:
    result = _turn_end(
        _instrumented_plugin(tmp_path),
        "getThinkingLevel: () => { throw new Error('nope'); }",
    )
    payload = result["payloads"][0]
    assert payload["type"] == "turn_end"
    assert "thinkingLevel" not in payload
