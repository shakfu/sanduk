"""One real run per shipped agent: its image, a real model, the relay, a report.

The runs are marked `agent_live` and deselected by default. Every run spends
money, so they also skip unless AGENT_LIVE=1, which `make test-agents` sets;
`make test-all` alone spends nothing here. An agent whose provider key is unset
skips. The check that every agent has a case is free and runs in `make test`.

The task can only be answered by running code in the container: hash a random
nonce from the mounted workdir. A model cannot compute SHA-256, so the digest in
REPORT.md proves a tool call ran and wrote through the mount.

    make test-agents                              # every agent
    make test-agents ARGS='-k "minima or hax"'    # some of them
"""

import hashlib
import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass

import pytest

from sanduk.agents import BUILTIN

KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
# Cheap tool-calling models, overridable per provider. openai uses the
# provider's own default when OPENAI_MODEL is unset.
MODELS = {
    "anthropic": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
    "openai": os.environ.get("OPENAI_MODEL", ""),
    "openrouter": os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash-0731"),
}
# Only OpenRouter reports cost, so only its runs can be capped by the relay.
BUDGET_USD = "0.10"


@dataclass(frozen=True)
class Case:
    agent: str
    provider: str
    mode: str = "sealed"


CASES = [
    Case("claude", "anthropic"),
    Case("codex", "openai"),
    Case("hax", "openrouter"),
    # hermes ignores every endpoint override, so it cannot use the relay.
    Case("hermes", "openrouter", mode="open"),
    Case("minima", "openrouter"),
    Case("opencode", "openrouter"),
    Case("pi", "openrouter"),
    Case("prime", "openrouter"),
]


def test_every_shipped_agent_has_a_case():
    assert sorted(c.agent for c in CASES) == sorted(a.name for a in BUILTIN)


@pytest.mark.agent_live
@pytest.mark.skipif(
    os.environ.get("AGENT_LIVE") != "1",
    reason="spends money; set AGENT_LIVE=1 or run `make test-agents`",
)
@pytest.mark.parametrize("case", CASES, ids=[c.agent for c in CASES])
def test_the_agent_runs_code_and_reports_the_result(case, tmp_path):
    key = KEYS[case.provider]
    if not os.environ.get(key):
        pytest.skip(f"{key} is not set")

    work = tmp_path / "work"
    work.mkdir()
    nonce = secrets.token_hex(16)
    (work / "nonce.txt").write_text(nonce)
    (work / "probe.py").write_text(
        "import hashlib, pathlib\n"
        "print(hashlib.sha256(pathlib.Path('nonce.txt').read_bytes()).hexdigest()[:16])\n"
    )
    expected = hashlib.sha256(nonce.encode()).hexdigest()[:16]
    stats = tmp_path / "stats.json"

    argv = [
        sys.executable, "-m", "sanduk", "run",
        "Run `python3 probe.py` in the working directory and report the exact "
        "line it prints.",
        "-w", str(work),
        "--agent", case.agent,
        "--provider", case.provider,
        "--mode", case.mode,
        "--max-turns", "10",
        "--timeout", "600",
        "--stats-file", str(stats),
    ]  # fmt: skip
    if MODELS[case.provider]:
        argv += ["--model", MODELS[case.provider]]
    if case.provider == "openrouter" and case.mode != "open":
        argv += ["--budget", BUDGET_USD]
    if os.environ.get("SANDUK_RUNTIME"):
        argv += ["--runtime", os.environ["SANDUK_RUNTIME"]]

    out = subprocess.run(argv, capture_output=True, text=True)
    log = f"stdout:\n{out.stdout[-3000:]}\nstderr:\n{out.stderr[-3000:]}"

    assert out.returncode == 0, log
    result = json.loads(stats.read_text())
    assert result["ok"], f"{result}\n{log}"
    report = work / "REPORT.md"
    assert report.is_file(), log
    assert expected in report.read_text(), f"digest {expected} not in:\n" + (
        report.read_text()
    )
