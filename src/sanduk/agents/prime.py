"""PrimeIntellect's `prime-agent`, a Rust port of pi's CLI.

Through 0.9.x it was pi's TypeScript in another build. 0.10 is Rust with a
Python kernel, and keeps pi's flags, JSON stream and `models.json` provider
block, read from the 0.10.2 source. What differs is the command, the
configuration directory, and the build.

A subclass rather than a copy: the day the two forks diverge in the stream,
one reader has to change and this file says which handler owns the difference.
"""

from __future__ import annotations

from sanduk.agents.pi import Pi


class Prime(Pi):
    name = "prime"
    recipe = "prime"
    # 0.10.2 reads this ahead of ~/.agents/skills, and beside AGENTS.md.
    skills_dir = ".prime/agent/skills"
    # In PRIME_AGENT_CODING_AGENT_DIR, which the recipe sets to ~/.prime/agent.
    instructions_file = ".prime/agent/AGENTS.md"
    key_env = "PRIME_RELAY_KEY"
    base_url_env = "PRIME_RELAY_BASE_URL"
    # 0.10.2 has no --no-approve, so a `.prime/agent/settings.json` in the
    # mounted directory is read. That steers the run; it does not widen the
    # box, which is the container and the relay either way.
    trust_flags = ()

    def key_reference(self) -> str:
        """A bare variable name, where pi takes `$NAME`.

        Measured against 0.9.4 with a stub upstream: `$PRIME_RELAY_KEY` arrived
        as the literal string in the Authorization header, `PRIME_RELAY_KEY`
        arrived as the variable's value, and omitting the field sent the
        request to api.openai.com under `OPENAI_API_KEY` instead of to the
        relay. 0.10.2's resolver reads the environment first, then the literal.
        """
        return self.key_env
