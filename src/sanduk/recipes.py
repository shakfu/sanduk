"""Recipes: JSON descriptions of an agent image, rendered to one Containerfile.

A recipe names a base image, an agent, the agent's account, install sections
and kits. It may inherit from other recipes by name, left to right, and drop
what it inherits with `remove`. Inheritance comes from start-vm, with one
change: parent sections come first, because a child step may need a parent's
packages.

Parents are not pinned; kits are. A kit whose `kit.json` no longer matches the
recipe's `sha256` stops the build, so a catalogue update cannot change an image
without a recipe edit.

The image tag is the recipe's name and a hash of everything the build reads,
so a changed recipe, parent or kit builds a new image rather than reusing a
stale one. See docs/dev/kits.md.
"""

from __future__ import annotations

import hashlib
import json
import platform
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sanduk import kits
from sanduk.catalog import NAME_RE, SHA256_RE, is_path, locate, read_json
from sanduk.errors import AgentboxError
from sanduk.kits import Kit
from sanduk.runtime import AGENT_UID_LABEL, CONTAINER_PREFIX, Runtime
from sanduk.sections import (
    TYPES,
    USER_RE,
    Context,
    Section,
    env_map,
    exec_form,
    fail,
    matching,
    path_value,
    render,
    section,
    strings,
    text,
)
from sanduk.util import note

RECIPE_KEYS = {
    "name",
    "description",
    "inherits",
    "agent",
    "from",
    "user",
    "home",
    "sections",
    "kits",
    "remove",
    "env",
    "entrypoint",
    "instructions",
}
REMOVE_KEYS = {"kits", "sections", "env", "section_types"}
IMAGE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/:@-")


@dataclass
class KitUse:
    kit: Kit
    pin: str | None  # None: named on the command line, unpinned
    pinned_by: str = ""


@dataclass(frozen=True)
class Instructions:
    """Standing instructions for the agent, and the recipe that set them."""

    owner: str
    text: str


@dataclass
class Recipe:
    """A recipe with its parents merged in."""

    name: str
    path: Path | None
    description: str = ""
    agent: str = ""
    base: str = ""  # `from`
    user: str = ""
    home: str = ""
    entrypoint: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    kits: list[KitUse] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # Parents first: a child appends to what it inherits.
    instructions: list[Instructions] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        """The resolved recipe, as `build --dry-run` prints it."""
        return {
            "name": self.name,
            "agent": self.agent,
            "from": self.base,
            "user": self.user,
            "home": self.home,
            "sections": [s.raw for s in self.sections],
            "kits": [
                {"name": u.kit.name, "sha256": u.kit.sha256, "pinned": u.pin is not None}
                for u in self.kits
            ],
            "env": self.env,
            "entrypoint": self.entrypoint,
            "instructions": [
                {"recipe": i.owner, "text": i.text} for i in self.instructions
            ],
        }


# --- reading ----------------------------------------------------------------


@dataclass
class _File:
    """One recipe file as written, before inheritance."""

    name: str
    path: Path
    inherits: list[str]
    remove: dict[str, list[str]]
    recipe: Recipe


def read(path: Path) -> _File:
    _, data = read_json(path, RECIPE_KEYS)
    where = str(path)
    name = matching(data.get("name"), NAME_RE, where, "name")
    if name != path.stem:
        raise fail(where, f"name {name!r} must equal the file name, {path.stem!r}")

    inherits = data.get("inherits", [])
    inherits = [inherits] if isinstance(inherits, str) else inherits
    if not isinstance(inherits, list):
        raise fail(where, "inherits must be a name or a list of names")
    inherits = [text(p, where, "inherits") for p in inherits]

    recipe = Recipe(name=name, path=path)
    if "description" in data:
        recipe.description = text(data["description"], where, "description")
    if "agent" in data:
        recipe.agent = matching(data["agent"], NAME_RE, where, "agent")
    if "from" in data:
        recipe.base = text(data["from"], where, "from")
        if not set(recipe.base) <= IMAGE_CHARS:
            raise fail(where, f"from {recipe.base!r} is not an image reference")
    if "user" in data:
        recipe.user = matching(data["user"], USER_RE, where, "user")
    if "home" in data:
        recipe.home = path_value(data["home"], where, "home", absolute=True)
    if "entrypoint" in data:
        recipe.entrypoint = strings(data["entrypoint"], where, "entrypoint")
    sections = data.get("sections", [])
    if not isinstance(sections, list):
        raise fail(where, "sections must be a list")
    recipe.sections = [section(s, where, path.parent) for s in sections]
    kits.unique([s.name for s in recipe.sections], where, "section")
    recipe.env = env_map(data.get("env"), where)
    if "instructions" in data:
        text_ = read_instructions(data["instructions"], where, path.parent)
        recipe.instructions = [Instructions(owner=name, text=text_)]
    recipe.kits = [kit_entry(e, where, path, name) for e in data.get("kits", [])]
    kits.unique([u.kit.name for u in recipe.kits], where, "kit")

    remove = data.get("remove", {})
    if not isinstance(remove, dict):
        raise fail(where, "remove must be an object")
    unknown = set(remove) - REMOVE_KEYS
    if unknown:
        raise fail(where, f"remove takes {', '.join(sorted(REMOVE_KEYS))}")
    removals = {k: strings(v, where, f"remove.{k}") for k, v in remove.items()}
    for t in removals.get("section_types", []):
        if t not in TYPES:
            raise fail(where, f"remove.section_types: {t!r} is not a section type")
    for key, added in (
        ("sections", [s.name for s in recipe.sections]),
        ("kits", [u.kit.name for u in recipe.kits]),
        ("env", list(recipe.env)),
    ):
        both = sorted(set(removals.get(key, [])) & set(added))
        if both:
            raise fail(
                where,
                f"remove.{key} names {', '.join(both)}, which this recipe also "
                "adds; defining it again already replaces the inherited one",
            )
    return _File(name=name, path=path, inherits=inherits, remove=removals, recipe=recipe)


def read_instructions(value: object, where: str, base: Path) -> str:
    """A path relative to the recipe, or `{"text": ...}`. Never guessed between."""
    if isinstance(value, str):
        rel = path_value(value, where, "instructions", absolute=False)
        target = base / rel
        if target.is_symlink() or not target.resolve().is_relative_to(base.resolve()):
            raise fail(where, f"instructions {rel!r} leaves {base}")
        if not target.is_file():
            raise fail(where, f"instructions {rel!r} is not a file in {base}")
        body = target.read_text(encoding="utf-8")
    elif (
        isinstance(value, dict)
        and set(value) == {"text"}
        and isinstance(value["text"], str)
    ):
        body = value["text"]
    else:
        raise fail(where, 'instructions must be a path, or {"text": "..."}')
    if not body.strip():
        raise fail(where, "instructions must be non-empty")
    return body


def kit_entry(raw: object, where: str, path: Path, owner: str) -> KitUse:
    if not isinstance(raw, dict) or len(raw) != 2 or "sha256" not in raw:
        raise fail(
            where,
            'each kit is {"name": ..., "sha256": ...} or {"path": ..., "sha256": ...}',
        )
    pin = matching(raw["sha256"], SHA256_RE, where, "kit sha256")
    if "name" in raw:
        kit = kits.load(matching(raw["name"], NAME_RE, where, "kit name"))
    elif "path" in raw:
        spec = text(raw["path"], where, "kit path")
        kit = kits.load(spec if is_path(spec) else f"./{spec}", relative_to=path.parent)
    else:
        raise fail(where, "a kit entry needs name or path")
    return KitUse(kit=kit, pin=pin, pinned_by=owner)


# --- inheritance ------------------------------------------------------------


def merge(into: Recipe, layer: Recipe) -> None:
    """Apply `layer` over `into`: scalars replace, named things replace in place."""
    for attr in ("agent", "base", "user", "home", "entrypoint"):
        if getattr(layer, attr):
            setattr(into, attr, getattr(layer, attr))
    into.sections = replace_by_name(into.sections, layer.sections, lambda s: s.name)
    into.kits = replace_by_name(into.kits, layer.kits, lambda u: u.kit.name)
    into.env = {**into.env, **layer.env}
    # Keyed by owner, so a recipe reached twice through inheritance counts once.
    into.instructions = replace_by_name(
        into.instructions, layer.instructions, lambda i: i.owner
    )


def replace_by_name(old: list[Any], new: list[Any], key: Any) -> list[Any]:
    out = list(old)
    index = {key(item): i for i, item in enumerate(out)}
    for item in new:
        if key(item) in index:
            out[index[key(item)]] = item
        else:
            index[key(item)] = len(out)
            out.append(item)
    return out


def apply_remove(merged: Recipe, remove: dict[str, list[str]], where: str) -> None:
    def gone(key: str, present: list[str]) -> set[str]:
        asked = set(remove.get(key, []))
        missing = sorted(asked - set(present))
        if missing:
            raise fail(
                where, f"remove.{key}: nothing inherited is named {', '.join(missing)}"
            )
        return asked

    drop = gone("sections", [s.name for s in merged.sections])
    types = set(remove.get("section_types", []))
    absent = sorted(types - {s.type for s in merged.sections})
    if absent:
        raise fail(
            where, f"remove.section_types: no inherited section is {', '.join(absent)}"
        )
    merged.sections = [
        s for s in merged.sections if s.name not in drop and s.type not in types
    ]
    kit_drop = gone("kits", [u.kit.name for u in merged.kits])
    merged.kits = [u for u in merged.kits if u.kit.name not in kit_drop]
    env_drop = gone("env", list(merged.env))
    merged.env = {k: v for k, v in merged.env.items() if k not in env_drop}


def resolve(spec: str, extra_kits: list[str] | None = None) -> Recipe:
    """A recipe by name or path, with its parents merged and its kit pins checked.

    `extra_kits` are added unpinned, as `--kit` on the command line.
    """
    recipe = _resolve(locate("recipes", spec), [])
    for kit_spec in extra_kits or []:
        kit = kits.load(kit_spec)
        note(f"kit {kit.name} is unpinned here; its sha256 is {kit.sha256}")
        recipe.kits = replace_by_name(
            recipe.kits, [KitUse(kit, None)], lambda u: u.kit.name
        )

    where = f"recipe {recipe.name}"
    for attr, key in (
        ("agent", "agent"),
        ("base", "from"),
        ("user", "user"),
        ("home", "home"),
    ):
        if not getattr(recipe, attr):
            raise fail(where, f"no {key}, in the recipe or anything it inherits")
    if not recipe.entrypoint:
        raise fail(where, "no entrypoint, in the recipe or anything it inherits")
    for use in recipe.kits:
        if use.pin is not None and use.pin != use.kit.sha256:
            raise AgentboxError(
                f"recipe {use.pinned_by} pins kit {use.kit.name} at {use.pin}, but "
                f"{use.kit.path} is {use.kit.sha256}. Check what changed, then "
                "update the pin"
            )
    providers: dict[str, str] = {}
    skills: dict[str, str] = {}
    for use in recipe.kits:
        for cap in use.kit.provides:
            if cap in providers:
                raise fail(
                    where, f"kits {providers[cap]} and {use.kit.name} both provide {cap}"
                )
            providers[cap] = use.kit.name
        for s in use.kit.skills:
            if s.name in skills:
                raise fail(
                    where,
                    f"kits {skills[s.name]} and {use.kit.name} both carry skill {s.name}",
                )
            skills[s.name] = use.kit.name
    return recipe


def _resolve(path: Path, chain: list[tuple[Path, str]]) -> Recipe:
    file = read(path)
    chain = [*chain, (path, file.name)]
    merged = Recipe(name=file.name, path=path)
    for parent_spec in file.inherits:
        parent_path = locate("recipes", parent_spec, relative_to=path.parent)
        if parent_path in [p for p, _ in chain]:
            names = [n for _, n in chain] + [read(parent_path).name]
            raise AgentboxError(f"recipe inheritance cycle: {' -> '.join(names)}")
        merge(merged, _resolve(parent_path, chain))
    apply_remove(merged, file.remove, str(path))
    merge(merged, file.recipe)
    merged.description = file.recipe.description
    return merged


# --- rendering --------------------------------------------------------------


@dataclass(frozen=True)
class Rendered:
    containerfile: str
    files: dict[str, bytes]


def host_arch(machine: str | None = None) -> str:
    """The architecture both engines build for on this host."""
    machine = (machine or platform.machine()).lower()
    return {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, machine)


def check(
    recipe: Recipe,
    agent: str,
    skills_dir: str | None,
    arch: str,
    instructions_file: str | None = None,
) -> None:
    """Refuse what would fail late: an agent a kit cannot serve, a missing build."""
    if recipe.agent != agent:
        raise AgentboxError(f"recipe {recipe.name} builds {recipe.agent}, not {agent}")
    if recipe.instructions and not instructions_file:
        raise AgentboxError(
            f"recipe {recipe.name} has instructions, and sanduk does not know where "
            f"{agent} reads them. They would be installed with nothing reading them"
        )
    for use in recipe.kits:
        kits.check(use.kit, agent, skills_dir)
    owned = [(recipe.name, s) for s in recipe.sections] + [
        (f"kit {u.kit.name}", t) for u in recipe.kits for t in u.kit.tools
    ]
    for owner, s in owned:
        if s.type in ("binary", "archive") and arch not in s.raw["artifacts"]:
            raise AgentboxError(f"{owner}: section {s.name} has no {arch} artifact")


def render_recipe(
    recipe: Recipe, skills_dir: str | None, instructions_file: str | None = None
) -> Rendered:
    """One Containerfile and the build-context files it copies."""
    ctx = Context()
    env = merged_env(recipe)
    lines = [
        f"# Rendered by sanduk from recipe {recipe.name}. Edit the recipe instead.",
        f"FROM {recipe.base}",
    ]

    def block(new: list[str]) -> None:
        lines.append("")
        lines.extend(new)

    for s in recipe.sections:
        if not s.as_agent:
            block(render(s, recipe.name, ctx))
    for use in recipe.kits:
        for t in use.kit.tools:
            if not t.as_agent:
                block(render(t, f"kit-{use.kit.name}", ctx))

    user, home = recipe.user, recipe.home
    block(
        [
            "# The agent's uid and gid. Docker passes the caller's, so the agent can",
            "# write a bind mount the host user owns.",
            "ARG AGENT_UID=1000",
            "ARG AGENT_GID=1000",
            f"LABEL {AGENT_UID_LABEL}=$AGENT_UID",
            f"RUN if id -u {user} >/dev/null 2>&1; then \\",
            f'      groupmod -o -g "$AGENT_GID" "$(id -gn {user})" \\',
            f'      && usermod -o -u "$AGENT_UID" -g "$AGENT_GID" {user}; \\',
            "    else \\",
            f'      groupadd -o -g "$AGENT_GID" {user} \\',
            f"      && useradd -o --create-home --home-dir {home} \\",
            f'         --uid "$AGENT_UID" --gid "$AGENT_GID" {user}; \\',
            "    fi \\",
            ' && mkdir -p /work && chown "$AGENT_UID:$AGENT_GID" /work',
        ]
    )

    if skills_dir and any(u.kit.skills for u in recipe.kits):
        parts = skills_dir.split("/")
        owned = [f"{home}/{'/'.join(parts[: i + 1])}" for i in range(len(parts))]
        block([f'RUN install -d -o "$AGENT_UID" -g "$AGENT_GID" {" ".join(owned)}'])
        for use in recipe.kits:
            if use.kit.skills:
                block(kits.render_skills(use.kit, f"{home}/{skills_dir}", ctx))

    if instructions_file and recipe.instructions:
        block(render_instructions(recipe, f"{home}/{instructions_file}", ctx))

    block([f"USER {user}", env_instruction({"HOME": home, **env})])
    for use in recipe.kits:
        agent_steps = [
            render(t, f"kit-{use.kit.name}", ctx) for t in use.kit.tools if t.as_agent
        ]
        setup = kits.render_setup(use.kit, recipe.agent)
        for step in agent_steps:
            block(step)
        if setup:
            block([f"# kit-{use.kit.name}: setup for {recipe.agent}", *setup])
    for s in recipe.sections:
        if s.as_agent:
            block(render(s, recipe.name, ctx))

    kit_label = ",".join(f"{u.kit.name}@{u.kit.sha256}" for u in recipe.kits)
    block(
        [
            "WORKDIR /work",
            f'LABEL sanduk.recipe="{recipe.name}" sanduk.kits="{kit_label}"',
            f"ENTRYPOINT {exec_form(recipe.entrypoint)}",
        ]
    )
    return Rendered(containerfile="\n".join(lines) + "\n", files=ctx.files)


def render_instructions(recipe: Recipe, dest: str, ctx: Context) -> list[str]:
    """The instructions as one read-only file at `dest`, in an agent-owned directory.

    The directories are the agent's because the agent keeps its own state beside
    the file, e.g. Claude Code under ~/.claude.
    """
    home = recipe.home
    parents = dest[len(home) + 1 :].split("/")[:-1]
    owned = [f"{home}/{'/'.join(parents[: i + 1])}" for i in range(len(parents))]
    body = "\n\n".join(i.text.strip("\n") for i in recipe.instructions) + "\n"
    rel = ctx.add(f"instructions/{recipe.name}/{dest.rsplit('/', 1)[1]}", body.encode())
    lines = [f"# instructions from {', '.join(i.owner for i in recipe.instructions)}"]
    if owned:
        lines.append(f'RUN install -d -o "$AGENT_UID" -g "$AGENT_GID" {" ".join(owned)}')
    return [*lines, f"COPY {rel} {dest}", f"RUN chmod a=r {dest}"]


def merged_env(recipe: Recipe) -> dict[str, str]:
    """Kit env under recipe env. Two kits disagreeing on a key is refused; the
    recipe composes them, so its own value wins."""
    env: dict[str, str] = {}
    source: dict[str, str] = {}
    for use in recipe.kits:
        for key, value in use.kit.env.items():
            if key in env and env[key] != value and key not in recipe.env:
                raise AgentboxError(
                    f"recipe {recipe.name}: kits {source[key]} and {use.kit.name} "
                    f"set {key} differently; set it in the recipe to choose"
                )
            env[key], source[key] = value, use.kit.name
    return {**env, **recipe.env}


def env_instruction(env: dict[str, str]) -> str:
    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    pairs = [f"{k}={quoted(v)}" for k, v in env.items()]
    return "ENV " + " \\\n    ".join(pairs)


def image_tag(recipe: Recipe, rendered: Rendered, build_args: list[str]) -> str:
    """`sanduk-<recipe>:<12 hex>`, over everything the build reads."""
    h = hashlib.sha256()
    h.update(rendered.containerfile.encode())
    for rel in sorted(rendered.files):
        h.update(
            b"\0" + rel.encode() + b"\0" + hashlib.sha256(rendered.files[rel]).digest()
        )
    h.update(b"\0" + json.dumps(build_args).encode())
    return f"{CONTAINER_PREFIX}{recipe.name}:{h.hexdigest()[:12]}"


def repository(recipe_name: str) -> str:
    return f"{CONTAINER_PREFIX}{recipe_name}"


def build(runtime: Runtime, image: str, rendered: Rendered) -> None:
    """Write the context to a scratch directory and build it."""
    with tempfile.TemporaryDirectory(prefix="sanduk-build-") as scratch:
        root = Path(scratch)
        (root / "Containerfile").write_text(rendered.containerfile)
        for rel, data in rendered.files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        runtime.build_image(image, root / "Containerfile")
