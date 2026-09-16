"""`minima -p --json`: a static Rust binary that streams one record per line.

minima's `--provider` fixes the wire format, and `MINIMA_BASE_URL` moves the
endpoint without changing it, so the relay is reached by naming the minima
provider that speaks the sanduk provider's protocol. `MINIMA_API_KEY` carries
the credential, which minima accepts only beside a named provider.
"""

from __future__ import annotations

import argparse
from typing import Any

from sanduk.agent import Agent, Outcome, Reader, Wiring
from sanduk.errors import AgentboxError
from sanduk.providers import (
    ANTHROPIC_MESSAGES,
    OPENAI_CHAT,
    OPENAI_RESPONSES,
    Provider,
)

# sanduk provider -> minima's registry entry of the same wire format.
# `llamacpp` rather than `ollama`: both are Chat Completions, and the base URL
# is overridden either way.
PROVIDERS = {
    "anthropic": "anthropic",
    "openai": "openai",
    "openrouter": "openrouter",
    "openai-compat": "llamacpp",
}


class MinimaReader(Reader):
    def __init__(self) -> None:
        self.result: dict[str, Any] | None = None

    def event(self, record: dict[str, Any], quiet: bool) -> None:
        kind = record.get("type")
        if kind == "result":
            self.result = record
            return
        if quiet:
            return
        if kind == "turn" and str(record.get("text", "")).strip():
            print(f"  . {str(record['text']).strip()[:160]}")
        elif kind == "tool_call":
            print(f"  > {record.get('name')}")
        elif kind == "tool_result" and not record.get("ok"):
            print("  ! tool error")
        elif kind == "retry":
            print(f"  ! retry {record.get('attempt')}")

    def finish(self) -> Outcome | None:
        if self.result is None:
            return None
        outcome = str(self.result.get("outcome", ""))
        ok = outcome == "complete"
        return Outcome(
            ok=ok,
            text=str(self.result.get("text", "")),
            # A cancelled run carries no error string; the outcome names it.
            error="" if ok else str(self.result.get("error") or outcome),
            # minima reports no cache counts and no cost.
            stats=(
                f"{self.result.get('turns', '?')} turns, "
                f"{self.result.get('input_tokens', 0):,} in / "
                f"{self.result.get('output_tokens', 0):,} out"
            ),
        )


class Minima(Agent):
    name = "minima"
    recipe = "minima"
    # $XDG_CONFIG_HOME/minima, which is ~/.config/minima in the image. Both from 0.3.0.
    skills_dir = ".config/minima/skills"
    instructions_file = ".config/minima/AGENTS.md"
    protocols = frozenset({ANTHROPIC_MESSAGES, OPENAI_CHAT, OPENAI_RESPONSES})

    def check(self, args: argparse.Namespace, provider: Provider) -> None:
        super().check(args, provider)
        if provider.name not in PROVIDERS:
            raise AgentboxError(f"minima has no provider matching {provider.name}")
        for flag, value in (
            ("--allowed-tools", args.allowed_tools),
            ("--permission-mode", args.permission_mode),
            ("--effort", args.effort),
        ):
            # Dropped silently, the run would differ from the one asked for.
            if value:
                raise AgentboxError(f"{flag} has no minima equivalent")
        if not args.model:
            # A fresh container has no remembered model, and minima picks one
            # itself only from an endpoint that lists exactly one.
            raise AgentboxError("--model is required with --agent minima")

    def wire(
        self, args: argparse.Namespace, provider: Provider, root: str | None
    ) -> Wiring:
        base = root or f"{provider.scheme}://{provider.host}"
        return Wiring(
            key_env=args.agent_key_env or "MINIMA_API_KEY",
            base_url_env=args.agent_base_url_env or "MINIMA_BASE_URL",
            # minima posts <base>/messages, /chat/completions or /responses.
            base_url=base + provider.api_prefix,
        )

    def argv(
        self, args: argparse.Namespace, provider: Provider, task: str, wiring: Wiring
    ) -> list[str]:
        argv = [
            "--json",
            f"--provider={PROVIDERS[provider.name]}",
            f"--model={args.model}",
        ]
        if args.max_turns:
            argv.append(f"--max-turns={args.max_turns}")
        # `=` binds the task to the flag, whatever it starts with.
        argv.append(f"--prompt={task}")
        return argv

    def reader(self) -> Reader:
        return MinimaReader()
