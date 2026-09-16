"""Kits: named bundles of tools and skills that recipes include.

A kit is a directory holding `kit.json`. Its identity is the SHA-256 of that
file's bytes, which a recipe pins. `kit.json` in turn pins downloads by
`sha256`, and every file of a vendored skill by its own hash, checked here when
the kit is read. A file a `copy` tool takes from the kit directory is not
pinned; see TODO.md.

One skill text serves every agent; the recipe's agent decides where it lands.
See docs/dev/kits.md.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from sanduk.catalog import NAME_RE, SHA256_RE, locate, read_json
from sanduk.errors import AgentboxError
from sanduk.sections import (
    URL_RE,
    Context,
    Section,
    env_map,
    exec_form,
    fail,
    matching,
    path_value,
    q,
    run_line,
    section,
    strings,
    text,
)

KIT_KEYS = {
    "name",
    "description",
    "tools",
    "skills",
    "env",
    "agents",
    "egress",
    "provides",
    "hook",
}
SKILL_FILE = "SKILL.md"


@dataclass(frozen=True)
class Skill:
    """A vendored skill directory (`path`, `files`) or one fetched `SKILL.md`."""

    name: str
    path: Path | None = None
    files: tuple[tuple[str, str], ...] = ()  # (relative path, sha256)
    url: str = ""
    sha256: str = ""


@dataclass(frozen=True)
class Kit:
    name: str
    path: Path  # kit.json
    sha256: str
    description: str
    tools: tuple[Section, ...]
    skills: tuple[Skill, ...]
    env: dict[str, str]
    # Agent name -> setup argvs run as the agent user. Empty: any agent.
    agents: dict[str, tuple[tuple[str, ...], ...]]
    egress: bool
    provides: tuple[str, ...]
    hook: bool


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(spec: str, relative_to: Path | None = None) -> Kit:
    """Read and check a kit by catalogue name or path."""
    path = locate("kits", spec, relative_to)
    raw, data = read_json(path, KIT_KEYS)
    where = str(path)
    name = matching(data.get("name"), NAME_RE, where, "name")
    if name != path.parent.name:
        raise fail(where, f"name {name!r} must equal its directory, {path.parent.name!r}")

    tools = [section(s, where, path.parent) for s in data.get("tools", [])]
    unique([t.name for t in tools], where, "tool")
    skills = [skill(s, where, path.parent) for s in data.get("skills", [])]
    unique([s.name for s in skills], where, "skill")

    agents_raw = data.get("agents", {})
    if not isinstance(agents_raw, dict):
        raise fail(where, "agents must be an object keyed by agent name")
    agents: dict[str, tuple[tuple[str, ...], ...]] = {}
    for agent, entry in agents_raw.items():
        matching(agent, NAME_RE, where, "agent name")
        if not isinstance(entry, dict) or set(entry) - {"setup"}:
            raise fail(where, f"agents.{agent} takes only setup")
        setup = entry.get("setup", [])
        if not isinstance(setup, list):
            raise fail(where, f"agents.{agent}.setup must be a list of argv lists")
        agents[agent] = tuple(tuple(strings(argv, where, "setup argv")) for argv in setup)

    provides = data.get("provides", [])
    if not isinstance(provides, list):
        raise fail(where, "provides must be a list")
    for flag in ("egress", "hook"):
        if not isinstance(data.get(flag, False), bool):
            raise fail(where, f"{flag} must be true or false")
    if "description" in data:
        text(data["description"], where, "description")

    return Kit(
        name=name,
        path=path,
        sha256=digest(raw),
        description=str(data.get("description", "")),
        tools=tuple(tools),
        skills=tuple(skills),
        env=env_map(data.get("env"), where),
        agents=agents,
        egress=bool(data.get("egress", False)),
        provides=tuple(matching(p, NAME_RE, where, "provides") for p in provides),
        hook=bool(data.get("hook", False)),
    )


def unique(names: list[str], where: str, what: str) -> None:
    seen = set()
    for name in names:
        if name in seen:
            raise fail(where, f"two {what}s named {name!r}")
        seen.add(name)


def skill(raw: object, where: str, base: Path) -> Skill:
    if not isinstance(raw, dict):
        raise fail(where, "each skill must be an object")
    if "path" in raw:
        if set(raw) != {"path", "files"}:
            raise fail(where, "a vendored skill takes exactly path and files")
        rel = path_value(raw["path"], where, "skill path", absolute=False)
        files = raw["files"]
        if not isinstance(files, dict):
            raise fail(where, f"skill {rel}: files must map each file to its sha256")
        root = base / rel
        verify_dir(root, base, files, f"{where}: skill {rel}")
        return Skill(name=root.name, path=root, files=tuple(sorted(files.items())))
    if set(raw) != {"name", "url", "sha256"}:
        raise fail(where, "a skill takes path and files, or name, url and sha256")
    name = matching(raw["name"], NAME_RE, where, "skill name")
    return Skill(
        name=name,
        url=matching(raw["url"], URL_RE, where, f"skill {name} url"),
        sha256=matching(raw["sha256"], SHA256_RE, where, f"skill {name} sha256"),
    )


def verify_dir(root: Path, base: Path, files: dict[str, object], where: str) -> None:
    """Every file under `root` is listed with a matching hash, and nothing more.

    An unlisted file would ride along uncovered by the recipe's pin.
    """
    if root.is_symlink() or not root.resolve().is_relative_to(base.resolve()):
        raise fail(where, "must be a directory inside the kit")
    if not root.is_dir():
        raise fail(where, "is not a directory")
    found: dict[str, Path] = {}
    for current, dirs, names in os.walk(root):
        for entry in [*dirs, *names]:
            if os.path.islink(os.path.join(current, entry)):
                raise fail(where, f"{entry} is a symlink; a skill holds plain files")
        for entry in names:
            full = Path(current) / entry
            found[full.relative_to(root).as_posix()] = full
    unlisted = sorted(set(found) - set(files))
    missing = sorted(set(files) - set(found))
    if unlisted:
        raise fail(where, f"files not listed in kit.json: {', '.join(unlisted)}")
    if missing:
        raise fail(where, f"listed files that are not there: {', '.join(missing)}")
    for rel, expected in sorted(files.items()):
        matching(expected, SHA256_RE, where, f"{rel} sha256")
        actual = digest(found[rel].read_bytes())
        if actual != expected:
            raise fail(where, f"{rel} is {actual}, but kit.json lists {expected}")
    if SKILL_FILE not in found:
        raise fail(where, f"has no {SKILL_FILE}")
    meta = frontmatter(found[SKILL_FILE].read_text(errors="replace"))
    if meta.get("name") != root.name or not meta.get("description"):
        raise fail(
            where,
            f"{SKILL_FILE} needs frontmatter with name: {root.name} and a description",
        )


def frontmatter(body: str) -> dict[str, str]:
    """The top-level keys of a `---` block at the start, as plain strings."""
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    found: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return found
        key, sep, value = line.partition(":")
        if sep and not line[:1].isspace():
            found[key.strip()] = value.strip().strip("\"'")
    return {}


# --- rendering --------------------------------------------------------------


def render_skills(kit: Kit, skills_root: str, ctx: Context) -> list[str]:
    """Root-owned, read-only skill directories under `skills_root`.

    The agent cannot edit a skill during a run; a fresh container resets it
    anyway, but a run should not be able to rewrite its own instructions.
    """
    lines: list[str] = []
    for s in kit.skills:
        dest = f"{skills_root}/{s.name}"
        lines.append(f"# kit-{kit.name}: skill {s.name}")
        if s.path is not None:
            # One COPY per file. A directory COPY left the directory empty on
            # Apple's builder (container 1.2.0) unless a file was also named.
            for rel, _ in s.files:
                source = ctx.add(
                    f"skills/{kit.name}/{s.name}/{rel}", (s.path / rel).read_bytes()
                )
                lines.append(f"COPY {source} {dest}/{rel}")
            lines.append(f"RUN chmod -R a=rX {q(dest)}")
        else:
            pieces = [
                "set -eu;",
                'tmp="$(mktemp)";',
                f'curl -fsSL -o "$tmp" {q(s.url)};',
                f'echo "{s.sha256}  $tmp" | sha256sum -c -;',
                f'install -D -m 0444 "$tmp" {q(dest + "/" + SKILL_FILE)};',
                f"chmod 0555 {q(dest)};",
                'rm -f "$tmp"',
            ]
            lines += run_line(pieces).splitlines()
    return lines


def render_setup(kit: Kit, agent: str) -> list[str]:
    return [f"RUN {exec_form(list(argv))}" for argv in kit.agents.get(agent, ())]


def check(kit: Kit, agent: str, skills_dir: str | None) -> None:
    """Refuse an agent this kit cannot serve."""
    if kit.agents and agent not in kit.agents:
        raise AgentboxError(
            f"kit {kit.name} supports {', '.join(sorted(kit.agents))}, not {agent}"
        )
    if kit.skills and skills_dir is None and agent not in kit.agents:
        raise AgentboxError(
            f"kit {kit.name} carries skills, and sanduk does not know where {agent} "
            "reads them. The tools would be installed with nothing telling the "
            "agent they exist"
        )
