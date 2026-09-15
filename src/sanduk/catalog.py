"""Finding recipes and kits by name, and reading their JSON.

A name is looked up in three places: the package, the user's config directory,
and the `sanduk.recipes` / `sanduk.kits` entry-point groups. A name found in
more than one is refused, as is a user or plugin file claiming a shipped name:
either would change the image without changing the command line.

Lookup by name never reads the working directory. A cloned repository could
otherwise supply a kit that runs as root, with network access, at build time.
A path given explicitly is read as given. See docs/dev/kits.md.
"""

from __future__ import annotations

import json
import os
import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from sanduk.agent import RESOURCES
from sanduk.errors import AgentboxError
from sanduk.util import note

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def entry_path(kind: str, root: Path, name: str) -> Path:
    """Where a named recipe or kit lives under one catalogue root."""
    if kind == "recipes":
        return root / "recipes" / f"{name}.json"
    return root / "kits" / name / "kit.json"


def config_dir() -> Path:
    """`XDG_CONFIG_HOME/sanduk`, or `~/.config/sanduk`."""
    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root) if root else Path.home() / ".config") / "sanduk"


def is_path(spec: str) -> bool:
    """A spec with a slash or a `.json` suffix names a file, not a catalogue entry."""
    return "/" in spec or spec.endswith(".json")


def _plugins(kind: str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for ep in entry_points(group=f"sanduk.{kind}"):
        try:
            loaded = ep.load()
        except Exception as e:  # a broken plugin must not take the run down
            note(f"{kind} plugin {ep.name!r} failed to load: {e}")
            continue
        if not isinstance(loaded, (str, Path)):
            note(f"{kind} plugin {ep.name!r} is not a path; ignored")
            continue
        path = Path(loaded)
        # A kit is advertised by its directory, a recipe by its file.
        found[ep.name] = path / "kit.json" if kind == "kits" and path.is_dir() else path
    return found


def _sources(kind: str, name: str) -> list[tuple[str, Path]]:
    hits = []
    for origin, root in (("shipped", RESOURCES), ("user", config_dir())):
        path = entry_path(kind, root, name)
        if path.is_file():
            hits.append((origin, path))
    plugin = _plugins(kind).get(name)
    if plugin is not None:
        hits.append(("plugin", plugin))
    return hits


def names(kind: str) -> list[str]:
    """Every name of this kind the catalogue can find."""
    found: set[str] = set()
    for root in (RESOURCES, config_dir()):
        base = root / kind
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            name = entry.stem if kind == "recipes" else entry.name
            if NAME_RE.match(name) and entry_path(kind, root, name).is_file():
                found.add(name)
    found.update(_plugins(kind))
    return sorted(found)


def locate(kind: str, spec: str, relative_to: Path | None = None) -> Path:
    """The file a recipe or kit spec names: a catalogue name, or a path."""
    singular = kind[:-1]
    if is_path(spec):
        path = Path(spec).expanduser()
        if not path.is_absolute():
            path = (relative_to or Path.cwd()) / path
        if kind == "kits" and path.is_dir():
            path = path / "kit.json"
        if not path.is_file():
            raise AgentboxError(f"no {singular} at {path}")
        return path.resolve()
    if not NAME_RE.match(spec):
        raise AgentboxError(
            f"{spec!r} is not a {singular} name ([a-z0-9-]) or a path "
            "(containing / or ending .json)"
        )
    hits = _sources(kind, spec)
    if not hits:
        known = ", ".join(names(kind)) or "none"
        raise AgentboxError(f"no {singular} named {spec!r}; known: {known}")
    if len(hits) > 1:
        where = "; ".join(f"{origin}: {path}" for origin, path in hits)
        if hits[0][0] == "shipped":
            raise AgentboxError(
                f"{singular} {spec!r} is shipped with sanduk and cannot be replaced "
                f"({where}). Rename yours"
            )
        raise AgentboxError(f"{singular} {spec!r} is defined twice ({where})")
    return hits[0][1].resolve()


def read_json(path: Path, allowed: set[str]) -> tuple[bytes, dict[str, Any]]:
    """The file's bytes and its top-level object, refusing keys not in `allowed`."""
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as e:
        raise AgentboxError(f"{path}: {e}") from None
    if not isinstance(data, dict):
        raise AgentboxError(f"{path}: expected a JSON object")
    unknown = set(data) - allowed - {"$schema"}
    if unknown:
        raise AgentboxError(f"{path}: unknown keys {', '.join(sorted(unknown))}")
    return raw, data
