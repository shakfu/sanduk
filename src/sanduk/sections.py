"""Install sections: the vocabulary recipes and kits share.

A section is checked when its file is read and rendered to Containerfile lines
when an image is built. Every value that reaches a shell is matched against a
character set and quoted. `run` is the exception by design: its lines are
written to a script in the build context rather than spliced into a RUN line,
so no quoting rule stands between the author and the shell they wrote.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sanduk.catalog import NAME_RE, SHA256_RE
from sanduk.errors import AgentboxError

TYPES = ("apt", "npm", "pip", "binary", "archive", "copy", "run")
# What `dpkg --print-architecture` prints, and what `uname -m` prints where
# there is no dpkg.
ARCHES = {"amd64": ("amd64", "x86_64"), "arm64": ("arm64", "aarch64")}

# Package specs, after start-vm's PackageSpec: nothing a shell treats specially,
# and no leading dash, so a spec cannot become a flag.
SPEC_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._+:@/~=<>!,-]*$")
NPM_PINNED = re.compile(r"^(@[a-z0-9._-]+/)?[a-z0-9._-]+@\d[A-Za-z0-9._+-]*$")
PIP_PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!_-]+$")
FLAG_RE = re.compile(r"^--?[a-z][a-z0-9-]*(=[A-Za-z0-9._/:-]+)?$")
URL_RE = re.compile(r"^https://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+$")
SEGMENTS_RE = re.compile(r"^[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*$")
MODE_RE = re.compile(r"^0?[0-7]{3}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")

KEYS = {
    "apt": {"install"},
    "npm": {"install", "flags"},
    "pip": {"install", "flags"},
    "binary": {"artifacts", "path"},
    "archive": {"artifacts", "dest", "links"},
    "copy": {"from", "to", "mode"},
    "run": {"lines", "user"},
}

# Where `run` scripts land in the image. Kept, not deleted: a record of what
# built the image, and nothing in them is secret.
STEPS_DIR = "/usr/local/share/sanduk/steps"


def fail(where: str, msg: str) -> AgentboxError:
    return AgentboxError(f"{where}: {msg}")


def text(value: object, where: str, what: str) -> str:
    """A single-line string. A newline would end a Containerfile instruction."""
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise fail(where, f"{what} must be a non-empty single-line string")
    return value


def matching(value: object, pattern: re.Pattern[str], where: str, what: str) -> str:
    found = text(value, where, what)
    if not pattern.match(found):
        raise fail(where, f"{what} {found!r} is not allowed here")
    return found


def strings(value: object, where: str, what: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise fail(where, f"{what} must be a non-empty list")
    return [text(v, where, what) for v in value]


def check_keys(data: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise fail(where, f"unknown keys {', '.join(sorted(unknown))}")


def path_value(value: object, where: str, what: str, absolute: bool) -> str:
    """A path with no `.` or `..` segment, absolute or relative as asked."""
    found = text(value, where, what)
    body = found[1:] if absolute else found
    if (absolute and not found.startswith("/")) or not SEGMENTS_RE.match(body):
        kind = "an absolute" if absolute else "a relative"
        raise fail(where, f"{what} {found!r} must be {kind} path of plain segments")
    if any(part in (".", "..") for part in body.split("/")):
        raise fail(where, f"{what} {found!r} may not contain . or ..")
    return found


def env_map(value: object, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise fail(where, "env must be an object")
    out: dict[str, str] = {}
    for key, v in value.items():
        matching(key, ENV_KEY_RE, where, "env key")
        if not isinstance(v, str) or "\n" in v or "\r" in v:
            raise fail(where, f"env {key} must be a single-line string")
        out[key] = v
    return out


@dataclass(frozen=True)
class Section:
    """One validated install step, and the directory `copy` reads from."""

    name: str
    type: str
    raw: dict[str, Any] = field(compare=False)
    base: Path = field(compare=False)

    @property
    def as_agent(self) -> bool:
        return self.raw.get("user") == "agent"


def section(raw: object, where: str, base: Path) -> Section:
    if not isinstance(raw, dict):
        raise fail(where, "each section must be an object")
    name = matching(raw.get("name"), NAME_RE, where, "section name")
    where = f"{where}: section {name}"
    kind = raw.get("type")
    if kind not in TYPES:
        raise fail(where, f"type must be one of {', '.join(TYPES)}")
    check_keys(raw, {"name", "type", "description"} | KEYS[kind], where)
    if "description" in raw:
        text(raw["description"], where, "description")

    if kind in ("apt", "npm", "pip"):
        specs = [
            matching(s, SPEC_RE, where, "package")
            for s in strings(raw.get("install"), where, "install")
        ]
        pinned = {"npm": (NPM_PINNED, "pkg@1.2.3"), "pip": (PIP_PINNED, "pkg==1.2.3")}
        if kind in pinned:
            pattern, example = pinned[kind]
            for spec in specs:
                if not pattern.match(spec):
                    raise fail(
                        where,
                        f"{spec!r} is unpinned; give an exact version, as {example}",
                    )
            flags = raw.get("flags", [])
            if not isinstance(flags, list):
                raise fail(where, "flags must be a list")
            for flag in flags:
                matching(flag, FLAG_RE, where, "flag")
    elif kind in ("binary", "archive"):
        artifacts(raw.get("artifacts"), where, kind)
        if kind == "binary" and "path" in raw:
            path_value(raw["path"], where, "path", absolute=True)
        if kind == "archive":
            path_value(raw.get("dest"), where, "dest", absolute=True)
            links = raw.get("links", {})
            if not isinstance(links, dict):
                raise fail(where, "links must be an object")
            for link, target in links.items():
                path_value(link, where, "link", absolute=True)
                path_value(target, where, "link target", absolute=False)
    elif kind == "copy":
        source = path_value(raw.get("from"), where, "from", absolute=False)
        resolved = (base / source).resolve()
        if not resolved.is_relative_to(base.resolve()) or (base / source).is_symlink():
            raise fail(where, f"from {source!r} leaves {base}")
        if not resolved.is_file():
            raise fail(where, f"from {source!r} is not a file in {base}")
        path_value(raw.get("to"), where, "to", absolute=True)
        if "mode" in raw:
            matching(raw["mode"], MODE_RE, where, "mode")
    else:
        strings(raw.get("lines"), where, "lines")
        if raw.get("user", "root") not in ("root", "agent"):
            raise fail(where, 'user must be "root" or "agent"')
    return Section(name=name, type=kind, raw=raw, base=base)


def artifacts(value: object, where: str, kind: str) -> None:
    if not isinstance(value, dict) or not value:
        raise fail(where, "artifacts must be an object keyed by amd64 and/or arm64")
    extra = {"binary": "member", "archive": "strip"}[kind]
    members = set()
    for arch, artifact in value.items():
        if arch not in ARCHES:
            raise fail(where, f"artifact architecture {arch!r} is not amd64 or arm64")
        if not isinstance(artifact, dict):
            raise fail(where, f"artifact {arch} must be an object")
        check_keys(artifact, {"url", "sha256", extra}, f"{where}: {arch}")
        matching(artifact.get("url"), URL_RE, where, f"{arch} url")
        matching(artifact.get("sha256"), SHA256_RE, where, f"{arch} sha256")
        if kind == "binary":
            members.add("member" in artifact)
            if "member" in artifact:
                path_value(artifact["member"], where, f"{arch} member", absolute=False)
        elif "strip" in artifact:
            strip = artifact["strip"]
            if not isinstance(strip, int) or isinstance(strip, bool) or strip < 0:
                raise fail(where, f"{arch} strip must be a non-negative integer")
    if len(members) > 1:
        raise fail(where, "either every artifact names a member or none does")


# --- rendering --------------------------------------------------------------


class Context:
    """The files a rendered Containerfile copies, by path in the build context."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def add(self, rel: str, data: bytes) -> str:
        if self.files.get(rel, data) != data:
            raise AgentboxError(f"two different files would be copied as {rel}")
        self.files[rel] = data
        return rel


def run_line(pieces: list[str]) -> str:
    """One RUN instruction. Each piece carries its own trailing separator."""
    return "RUN " + " \\\n    ".join(pieces)


def q(value: str) -> str:
    return shlex.quote(value)


def fetch(
    label: str, arts: dict[str, dict[str, Any]], names: dict[str, str]
) -> list[str]:
    """Shell pieces that download this architecture's artifact to "$tmp/download"
    and verify it. `names` maps an artifact key to the variable it sets."""
    pieces = [
        "set -eu;",
        'arch="$(dpkg --print-architecture 2>/dev/null || uname -m)";',
        'case "$arch" in',
    ]
    for arch in sorted(arts):
        artifact = arts[arch]
        assigns = [f"url={q(artifact['url'])}", f"sum={q(artifact['sha256'])}"]
        assigns += [
            f"{var}={q(str(artifact[key]))}"
            for key, var in names.items()
            if key in artifact
        ]
        pieces.append(f"  {'|'.join(ARCHES[arch])}) {'; '.join(assigns)} ;;")
    pieces += [
        f'  *) echo "{label}: no build for $arch" >&2; exit 1 ;;',
        "esac;",
        'tmp="$(mktemp -d)";',
        'curl -fsSL -o "$tmp/download" "$url";',
        'echo "$sum  $tmp/download" | sha256sum -c -;',
    ]
    return pieces


def render(s: Section, owner: str, ctx: Context) -> list[str]:
    """Containerfile lines for one section. `owner` names the recipe or kit."""
    raw = s.raw
    head = f"# {owner}: {s.name}" + (
        f" -- {raw['description']}" if "description" in raw else ""
    )
    flags = " ".join(q(f) for f in raw.get("flags", []))
    specs = " ".join(q(p) for p in raw.get("install", []))

    if s.type == "apt":
        body = [
            "RUN apt-get update \\",
            " && apt-get install -y --no-install-recommends \\",
            f"      {specs} \\",
            " && rm -rf /var/lib/apt/lists/*",
        ]
    elif s.type == "npm":
        body = [
            f"RUN npm install -g {flags + ' ' if flags else ''}{specs} \\",
            " && npm cache clean --force",
        ]
    elif s.type == "pip":
        body = [f"RUN pip install --no-cache-dir {flags + ' ' if flags else ''}{specs}"]
    elif s.type == "binary":
        target = raw.get("path", f"/usr/local/bin/{s.name}")
        pieces = fetch(s.name, raw["artifacts"], {"member": "member"})
        if any("member" in a for a in raw["artifacts"].values()):
            pieces += [
                'tar -xzf "$tmp/download" -C "$tmp" "$member";',
                f'install -D -m 0755 "$tmp/$member" {q(target)};',
            ]
        else:
            pieces.append(f'install -D -m 0755 "$tmp/download" {q(target)};')
        pieces.append('rm -rf "$tmp"')
        body = run_line(pieces).splitlines()
    elif s.type == "archive":
        dest = raw["dest"]
        pieces = fetch(s.name, raw["artifacts"], {"strip": "strip"})
        pieces += [
            f"mkdir -p {q(dest)};",
            f'tar -xzf "$tmp/download" -C {q(dest)} --strip-components="${{strip:-0}}";',
        ]
        for link, target in sorted(raw.get("links", {}).items()):
            pieces.append(f"ln -sf {q(dest + '/' + target)} {q(link)};")
        pieces.append('rm -rf "$tmp"')
        body = run_line(pieces).splitlines()
    elif s.type == "copy":
        source = Path(raw["from"])
        rel = ctx.add(
            f"files/{owner}/{s.name}/{source.name}", (s.base / source).read_bytes()
        )
        body = [f"COPY {rel} {raw['to']}"]
        if "mode" in raw:
            body.append(f"RUN chmod {raw['mode']} {raw['to']}")
    else:
        script = f"{owner}-{s.name}.sh"
        rel = ctx.add(f"steps/{script}", ("\n".join(raw["lines"]) + "\n").encode())
        body = [f"COPY {rel} {STEPS_DIR}/{script}", f"RUN sh -eu {STEPS_DIR}/{script}"]
    return [head, *body]


def exec_form(argv: list[str]) -> str:
    """JSON exec form, for ENTRYPOINT and agent setup: no shell parses it."""
    return json.dumps(argv)
