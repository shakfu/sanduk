# Kits and recipes

Status: sketch, 2026-09-15. Nothing implemented or measured. Tool and agent facts come from each project's docs and source on its default branch. start-vm facts come from [shakfu/start-vm](https://github.com/shakfu/start-vm) at `d8de8c7`. Inference is marked; unverified claims say UNCONFIRMED.

Scope: JSON files that describe an agent image (recipes) and reusable bundles of tools and skills that recipes include by name (kits).

## Terms

| Term | Meaning |
|-|-|
| kit | a named, versioned bundle of tools and skills. JSON. Included by recipes |
| recipe | a JSON description of one agent image: base image, agent, install sections, kits. Renders to one Containerfile |
| runtime | unchanged: the container engine, `--runtime apple` or `docker` |
| agent handler | unchanged: `argv`, `wire`, `reader`. Gains `skills_dir` |

A recipe builds an image, not a runtime. `apple` and `docker` both build from the same Containerfile, so a recipe names no engine. An engine that cannot build an arbitrary Containerfile would need its own renderer: Docker Sandboxes templates must start `FROM docker/sandbox-templates:<variant>` ([sbx.md](sbx.md)).

## What kits and recipes add

Today each agent has one hand-written Containerfile, and `--containerfile` swaps in another.

- **Composition.** `--recipe claude-docs`, or `--agent claude --kit docs`, without writing a Containerfile per combination.
- **Per-agent integration.** A kit's skills land where the chosen agent reads skills.
- **Checks before a run.** Refuse a kit that needs egress under `sealed`, or a hook kit under `--allowed-tools`.
- **One copy of the invariants.** All 7 Containerfiles repeat the `AGENT_UID` block. A renderer emits it once.
- **A catalogue.** `sanduk list kits` and `sanduk list recipes`.

The ongoing cost is the catalogue. Each tool carries a version, one checksum per architecture, and skill text, all bumped by hand.

## What to take from start-vm

start-vm renders YAML recipes into shell, Python, PowerShell and Dockerfile output ([README](https://github.com/shakfu/start-vm#the-model)). A recipe has `name`, `platform`, `os`, `version`, `release`, `inherits` and a list of typed `sections` ([recipe_schema.md](https://github.com/shakfu/start-vm/blob/master/docs/recipe_schema.md)).

| start-vm | sanduk | Reason |
|-|-|-|
| YAML through PyYAML, Jinja2 templates | JSON through `json`, rendering in Python | sanduk has zero runtime dependencies |
| typed sections with `install` lists | kept; types in [Section types](#section-types) | one installer vocabulary for recipes and kits |
| package specs checked against a character set, then shell-quoted (`PackageSpec`, `start_vm.py:35-75`) | kept | a spec cannot inject shell |
| `inherits`: a name or a list, left to right, cycle reported with its chain | kept | |
| a child section replaces a parent section of the same `name` | kept, replaced in place | |
| child sections first, unmatched parent sections appended (`start_vm.py:731-737`) | parent sections first | a child section may need a parent's packages. start-vm's schema doc lists parents first; its code runs the child first |
| `name` inherited; the schema doc warns a child then overwrites its parent's output | `name` required in every file, not inherited, equal to the file stem | removes the collision |
| `platform`, `os`, `version`, `release` | `from` (image) and `agent` | the image is the target |
| `purge`, uninstall scripts | none | images are rebuilt, never uninstalled |
| unpinned packages; `shell` sections with `curl \| sh` | versions required for npm and pip; SHA-256 for downloads | `sealed` fetches nothing at run time; supply chain |
| lockfile | image labels carry kit hashes; a resolved lockfile is deferred | inference from the README example: start-vm's lockfile records the declared specs, which the recipe already holds |
| `default/` and `config/` copied wholesale into `$HOME` | only declared `copy` sections and skills | nothing lands in the image unlisted |

## Format: JSON

Decided: JSON for both kits and recipes. TOML only if it serves both; one format, not two.

What JSON costs here:

- **No comments.** The shipped Containerfiles keep their rationale in comments, e.g. why `Containerfile.prime` builds a Python kernel. In JSON, rationale moves to `description` fields, to copied script files, or to `docs/dev/`.
- **Shell in strings.** `run` takes an array of lines; anything longer goes in a copied script.

What it gives:

- `json` reads and writes. `build --dry-run` can print the resolved recipe.
- A `$schema` key gives editor completion and validation. sanduk validates by hand, without `jsonschema`.

## Section types

Recipes and kits share these types.

| Type | Fields | Renders |
|-|-|-|
| `apt` | `install: [spec]` | `apt-get install --no-install-recommends`, then clears the lists |
| `npm` | `install: ["pkg@version"]` | `npm install -g`; a version is required |
| `pip` | `install: ["pkg==version"]` | `pip install`; a version is required |
| `binary` | `artifacts.<arch>`: `url`, `sha256`, optional `member` | fetch, verify, install one file to `/usr/local/bin` |
| `archive` | `artifacts.<arch>`: `url`, `sha256`; `dest`; `links` | fetch, verify, extract a tree to `dest`, symlink `links` onto `PATH` |
| `copy` | `from` (relative to the JSON file), `to`, `mode` | `COPY` from the build context |
| `run` | `lines: [str]`, `user: "root" \| "agent"` | `RUN set -eux; ...`. The escape hatch; flagged in listings |

Every section has `name` and optional `description`. `<arch>` is `amd64` or `arm64`, matched against `dpkg --print-architecture` at build.

Architecture is the only platform axis. Every shipped base image is Debian with glibc, and both engines build Linux images. rtk's arm64 release is glibc-only, which would matter only for an Alpine `from`.

## Recipes

### A shipped recipe

`Containerfile.claude` ported:

```json
{
  "$schema": "../schema/recipe.schema.json",
  "name": "claude",
  "description": "Claude Code on node:22-slim",
  "agent": "claude",
  "from": "docker.io/library/node:22-slim",
  "user": "node",
  "home": "/home/node",
  "sections": [
    {
      "name": "base-tools",
      "type": "apt",
      "install": ["git", "ripgrep", "ca-certificates", "curl", "jq", "python3"]
    },
    {
      "name": "agent",
      "type": "npm",
      "install": ["@anthropic-ai/claude-code@<version>"]
    }
  ],
  "env": {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  },
  "entrypoint": ["claude"]
}
```

### A child recipe

```json
{
  "name": "claude-docs",
  "description": "Claude Code with diagram and Office tooling",
  "inherits": "claude",
  "kits": [
    {"name": "docs", "sha256": "9b1c...e04a"}
  ]
}
```

A child that drops what it inherits:

```json
{
  "name": "claude-lean",
  "inherits": "claude-docs",
  "remove": {
    "kits": ["docs"],
    "env": ["DISABLE_ERROR_REPORTING"]
  }
}
```

`remove.sections` drops inherited sections by name. `remove.section_types`, e.g. `["apt"]`, drops every inherited section of a type. It applies to the recipe's own sections only; a kit's tools go when the kit goes. The recipe may then add sections of that type again, e.g. to replace all inherited `apt` sections with one.

To change a single inherited section rather than drop it, redefine it under the same `name`; that needs no `remove`.

### Fields

| Field | Required | Inherited | Meaning |
|-|-|-|-|
| `name` | yes | no | equals the file stem |
| `description` | no | no | |
| `inherits` | no | no | a name or a list of names |
| `agent` | yes, after inheritance | yes | handler name; supplies `skills_dir` |
| `from` | yes, after inheritance | yes | base image |
| `user`, `home` | yes, after inheritance | yes | the agent's account; created if `from` lacks it |
| `sections` | no | merged | see below |
| `kits` | no | merged by name | `{"name", "sha256"}` or `{"path", "sha256"}`; `path` is relative to this file |
| `remove` | no | no | `kits`, `sections`, `env`: names to drop from what was inherited. `section_types`: section types to drop |
| `env` | no | merged by key | `ENV` lines |
| `entrypoint` | yes, after inheritance | yes | argv |

Merge rules:

1. Parents resolve left to right, each with its own `remove` already applied.
2. The child's `remove` applies to that merged result.
3. The child's own fields apply last.
4. Scalars: a later value replaces an earlier one.
5. `sections`: parent order is kept. A later section with the same `name` replaces the earlier one in place. New names append.
6. `kits`: merged by name in first-seen order. A later entry replaces an earlier one, so a child can re-pin a kit.
7. A cycle is refused with its chain, e.g. `a -> b -> a`.

A `remove` reaches only the recipe's own ancestry. With `inherits: ["a", "b"]`, `b` cannot remove what `a` contributes; the child inheriting both can.

`remove` cannot reach the renderer's fixed steps: the `AGENT_UID` block, `USER`, `WORKDIR` and labels are not sections. Removing a section a later one needs fails at build, not at load; sanduk cannot see dependencies between shell steps.

### Kit pins

A recipe names each kit with the SHA-256 of its `kit.json`, as `sha256sum kit.json` prints it. A mismatch stops the build:

```text
recipe claude-docs pins kit docs at 9b1c...e04a, but
~/.config/sanduk/kits/docs/kit.json is 51f7...a9c2
```

The pin covers the whole kit only if `kit.json` pins everything else the kit uses. Downloads already carry `sha256`. Vendored skill files must too; see [Kit fields](#fields-1). A byte-level hash also fails on a whitespace-only edit. That is the price of a hash `sha256sum` can reproduce.

Consequences:

- **A shipped kit updated in a sanduk release breaks every recipe that pins it,** until the pin is updated. This is intended: an image does not change without a recipe edit.
- **`sanduk list kits` prints each kit's hash,** so re-pinning is a copy, not a computation.
- **Parents are not pinned.** `inherits` takes names only, so an edit to `claude.json` changes `claude-docs` without an error. Pinning parents would break every child recipe on each sanduk release that touches a shipped recipe. The changed parent still changes the image tag, so the image is rebuilt rather than reused.
- **`--kit docs` on the command line needs no pin.** sanduk prints the hash it used. Recipe files and `assistant.toml` go through recipes, so unattended runs are always pinned.

### Rendering

One Containerfile per resolved recipe, in this order:

1. `FROM`.
2. Recipe sections as root.
3. Each kit's tool sections as root, in kit order.
4. Skills copied into `home/skills_dir`, root-owned, mode 0444.
5. The `AGENT_UID` block: `usermod` if `user` exists, `useradd` otherwise. Last among root steps, so a uid change reuses the install layers.
6. `USER`, then each kit's agent `setup` and any `run` section with `user: "agent"`.
7. `ENV`, `WORKDIR /work`, `ENTRYPOINT`.
8. `LABEL sanduk.recipe=<name> sanduk.kits=<name>@<sha256>,...`.

The tag is `sanduk-<recipe>:<sha12>`. The hash covers the rendered Containerfile, every file copied into the context, and the build args. Everything is in one file, so a changed kit or parent changes the tag. No base-image staleness check is needed. Layer caching keeps unchanged steps.

`destroy` deletes images labelled `sanduk.recipe`. Label filtering on Apple's CLI is UNCONFIRMED; the fallback is recording built tags under `$XDG_STATE_HOME/sanduk`.

### Porting the shipped Containerfiles

- **claude** installs `@anthropic-ai/claude-code` unpinned (`Containerfile.claude:7`). The other five package installs pin a version. The `npm` rule would refuse it, so the port pins it.
- **hax** downloads its release tarball without a checksum (`Containerfile.hax`). The port uses `binary` with `sha256`.
- **pi, prime** write their entrypoint with `printf`. The port copies a real script file, which can hold comments.
- **prime** installs a checksummed tarball with `npm` and build-time environment. That stays a `run` section.
- **hax, hermes** create their user. The renderer handles both cases.

A third-party `Agent` with `containerfile` keeps working, without kits. The shipped handlers replace `image` and `containerfile` with `recipe = "claude"`.

## Kits

### A bundle

```json
{
  "$schema": "../../schema/kit.schema.json",
  "name": "docs",
  "description": "Diagrams from D2 text; Office documents",
  "tools": [
    {
      "name": "d2",
      "type": "binary",
      "version": "0.9.0",
      "license": "MPL-2.0",
      "homepage": "https://github.com/d2lang/d2",
      "artifacts": {
        "amd64": {"url": "https://github.com/d2lang/d2/releases/download/v0.9.0/d2-v0.9.0-linux-amd64.tar.gz", "sha256": "...", "member": "d2-v0.9.0/bin/d2"},
        "arm64": {"url": "https://github.com/d2lang/d2/releases/download/v0.9.0/d2-v0.9.0-linux-arm64.tar.gz", "sha256": "...", "member": "d2-v0.9.0/bin/d2"}
      }
    },
    {
      "name": "officecli",
      "type": "binary",
      "version": "1.0.150",
      "license": "Apache-2.0",
      "homepage": "https://github.com/iOfficeAI/OfficeCLI",
      "artifacts": {
        "amd64": {"url": "...", "sha256": "..."},
        "arm64": {"url": "...", "sha256": "..."}
      }
    }
  ],
  "skills": [
    {"path": "skills/d2", "files": {"SKILL.md": "..."}},
    {"name": "officecli", "url": "https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/<commit>/SKILL.md", "sha256": "..."}
  ],
  "env": {"OFFICECLI_SKIP_UPDATE": "1"},
  "egress": false
}
```

The d2 archive layout in `member` is UNCONFIRMED. officecli's releases are raw binaries, so they have no `member`.

### A hook kit

```json
{
  "name": "rtk",
  "description": "Filter shell output before the agent reads it",
  "tools": [{"name": "rtk", "type": "binary", "version": "0.49.0", "artifacts": {"...": "..."}}],
  "provides": ["shell-filter"],
  "hook": true,
  "agents": {
    "claude": {"setup": [["rtk", "init", "-g"]]}
  }
}
```

`rtk init -g` may prompt; a non-interactive flag is UNCONFIRMED.

### Fields

| Field | Meaning |
|-|-|
| `name`, `description` | `name` equals the directory name |
| `tools` | sections, each with optional `version`, `license`, `homepage` |
| `skills` | `path` (a `SKILL.md` directory in the kit) with `files`, a map of every file under it to its SHA-256; or `url` + `sha256` + `name` |
| `env` | `ENV` lines |
| `agents` | per-agent `setup` argv lists, run as the agent user. If present, only these agents are supported |
| `egress` | `true` if a tool needs the network at run time |
| `provides` | capability names; two kits with a shared name are refused |
| `hook` | `true` if the kit changes how the agent runs commands |

A kit does not include other kits; recipes compose kits. A kit's identity is the SHA-256 of `kit.json`, so it has no version field; its tools carry their own. A file under a skill `path` that is missing from `files`, or whose hash differs, is refused. That keeps the recipe's pin transitive.

## Skill directories

Every shipped agent discovers `SKILL.md` directories ([Agent Skills spec](https://agentskills.io/specification)). The handler's `skills_dir` records where.

| Agent | `skills_dir` (under `home`) | Source |
|-|-|-|
| claude | `.claude/skills` | [docs](https://code.claude.com/docs/en/skills) |
| codex | `.agents/skills` (`.codex/skills` deprecated) | [host_roots.rs](https://github.com/openai/codex/blob/main/codex-rs/ext/skills/src/host_roots.rs) |
| opencode | `.agents/skills`; also reads `.claude/skills` | [skill/index.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/skill/index.ts) |
| pi | `.agents/skills` | [skills.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md) |
| prime | UNCONFIRMED; inference: as pi | -- |
| hax | `.agents/skills` | [usage.md](https://github.com/OleksandrChekhovskyi/hax/blob/main/docs/usage.md) |
| hermes | `.hermes/skills` | [skills.md](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md) |

One skill text serves every agent: the same files go into each agent's `skills_dir`. Skill text therefore names shell commands, never an agent's tool names such as claude's `Bash`.

Install each skill into one directory. opencode reads two, so a copy in both may load twice (UNCONFIRMED).

hermes writes its own skills into `.hermes/skills`. Root-owned kit skills there may break that (inference).

## Lookup by name

In order; a name found in two places is refused:

1. `$XDG_CONFIG_HOME/sanduk/recipes/<name>.json` and `$XDG_CONFIG_HOME/sanduk/kits/<name>/kit.json`.
2. Entry point groups `sanduk.recipes` and `sanduk.kits`, each loading a `Path`.
3. Shipped: `src/sanduk/resources/recipes/` and `src/sanduk/resources/kits/`.

A user or third-party file named like a shipped one is refused, as `agent.py` refuses for handlers.

A path given explicitly (`--recipe ./x.json`, or a kit `path` inside such a recipe) is read as given, and a kit found by path is pinned like any other. Lookup by name never reads the working directory or `/work`. A cloned repository could otherwise supply a kit that runs as root, with network access, when an image is built.

## Commands

```text
sanduk list recipes | list kits                   list kits prints each kit's SHA-256
sanduk build --recipe claude-docs [--dry-run]     --dry-run prints the resolved recipe and Containerfile
sanduk run TASK --recipe claude-docs
sanduk run TASK --agent claude --kit docs         an unnamed recipe: inherits claude, adds docs unpinned
sanduk shell --recipe claude-docs
```

```toml
# assistant.toml
recipe = "claude-docs"
```

`--agent claude` alone means `--recipe claude`. A recipe holds build settings only. Mode, provider and mounts stay in flags and `assistant.toml`.

## Refusals

Checked before the build, alongside `Agent.check()`:

| Condition | Why refused |
|-|-|
| a kit with `egress: true` under `--mode sealed` | the tool fails at its first call, after tokens are spent |
| no artifact for the build architecture | the build fails late otherwise |
| `hook: true` with `--allowed-tools` | a hook that allows its rewrite may pass a command the flag refuses ([compressors.md](compressors.md#unresolved)); untested |
| `hook: true` with claude `--bare` | `--bare` drops hooks, so the kit does nothing |
| kit skills for an agent with no `skills_dir`, and no `agents` entry for it | the tool is present, but nothing tells the agent it exists |
| an `agents` map that omits the chosen agent | the kit declared which agents it supports |
| two kits share a `provides` name | rtk and snip edit the same hook config |
| a download without `sha256`; npm or pip without a version | unpinned install |
| a recipe kit entry without `sha256`, or whose `sha256` differs from `kit.json` | the image would change without a recipe edit |
| a file under a skill `path` not listed in `files`, or with a different hash | the kit pin would not cover it |
| a `remove` name or section type that nothing inherited provides | a typo would otherwise remove nothing, silently |
| a name in `remove` that the same recipe also adds | the remove does nothing; replacement by name already exists |
| a `remove.section_types` entry that is not a section type | |
| a package spec outside the allowed characters | shell injection through a spec |
| an inheritance cycle; `name` not equal to the file stem | |
| `--agent X --recipe Y` where Y's agent is not X | |
| `--kit` with `--image` or `--containerfile` | `user` and `home` are unknown for an arbitrary image |
| a name found in two lookup locations, or shadowing a shipped one | changes the image without changing the command line |

## Trust

- **The build is outside every mode.** Recipes and kits run as root, with network access, at build time. `sealed` constrains the run only. A third-party recipe or kit has the power of a Containerfile.
- **A skill is instruction.** The agent follows it; review it as a prompt.
- **A hook rewrites commands.** `list kits` shows `hook`.
- **The repository can shadow a skill.** hax searches project `.agents/skills` first ([usage.md](https://github.com/OleksandrChekhovskyi/hax/blob/main/docs/usage.md)), so `/work` can override a kit skill of the same name.
- **Upstream installers are not used.** officecli's installer writes skills into every agent directory it finds. `rtk init` without `-g` writes project files.
- **Telemetry and update checks** are disabled through `env` where a switch exists. `sealed` blocks them anyway; `open` and `key-safe` do not.

## First kits

| Kit | Tools | Skills | Run-time network | Notes |
|-|-|-|-|-|
| `docs` | d2 0.9.0: static tarball, `SHA256SUMS` ([release](https://github.com/d2lang/d2/releases/tag/v0.9.0)); officecli 1.0.150: single-file .NET binary, glibc x64/arm64 ([repo](https://github.com/iOfficeAI/OfficeCLI)) | d2: vendored. officecli: upstream `SKILL.md` | d2: remote icons only. officecli: update check, off via `OFFICECLI_SKIP_UPDATE=1` | officecli PNG screenshots need a headless browser |
| `quarto` | quarto 1.10.18: 147 MB `archive`, glibc; bundles Deno, Pandoc, Typst ([configuration](https://github.com/quarto-dev/quarto-cli/blob/main/configuration)) | vendored | LaTeX PDF: `tlmgr` fetches missing packages ([docs](https://quarto.org/docs/output-formats/pdf-engine.html)) | Typst PDF works offline. TinyTeX must be installed at build |
| `rtk` | rtk 0.49.0: x86_64 musl, aarch64 glibc | none; `rtk init -g` hook | none; telemetry opt-in | claude only, after the TODO trial |
| `snip` | snip 0.25.2: static Go | its `SKILL.md` has no frontmatter; not usable | none found | shares `provides` with rtk |

Start with `docs`: two single-binary tools, one vendored skill, one upstream skill. Compressors wait for the trial in [TODO.md](../../TODO.md). Their benefit is unmeasured, and the hook raises the `--allowed-tools` question.

`quarto` needs a decision: offline Typst PDF only, or a larger kit with TinyTeX preinstalled.

## Alternatives considered

1. **Kits without recipes.** Render a layer `FROM` the existing agent image. Ships kits one stage sooner. It needs a base-image staleness check that recipes make unnecessary, and becomes dead code once recipes land.
2. **Containerfile fragments.** `--layer FILE` appends a snippet. About 50 lines. No skill placement, refusals or catalogue.
3. **Run-time read-only mounts.** The host verifies Linux binaries into a cache and mounts tools and skills read-only. No rebuild per combination, and the agent cannot edit them. Cannot handle `apt`, `npm`, `pip` or hooks that edit agent config. Nested mounts under `$HOME` on Apple's engine are UNCONFIRMED.
4. **Claude Code plugins as the unit.** They bundle skills, hooks, MCP servers and `bin/` ([plugins-reference](https://code.claude.com/docs/en/plugins-reference)). They serve one agent of seven.

## Stages

1. Recipe loader, inheritance with `remove`, validation, renderer, tag hash; `build --recipe --dry-run`. Port `claude`.
2. Port the other 6 recipes; handlers take `recipe`; delete the Containerfiles.
3. Kits: `binary`, `skills` with `files`, `env`, pins, lookup, refusals, `list kits`; ship `docs`.
4. `agents.setup`, `provides`, `hook`; `rtk` after the trial.
5. `archive` and `pip`; `quarto`.

Recipes come first to avoid alternative 1's throwaway layer. The cost is one stage before the first kit ships.

## Tests

- Unit, no engine:
  - Inheritance: parent-first order, replacement in place, scalar precedence across multiple parents, the cycle message, `name` against file stem.
  - `remove`: drops kits, sections, env keys and section types; stays within its own ancestry; refuses unknown names, unknown types and remove-plus-add by name; allows re-adding a removed type.
  - Pins: a one-byte change to `kit.json` or a vendored skill file refuses the build; a child re-pin wins.
  - Package spec rejection.
  - Rendered Containerfile per shipped recipe against a golden file.
  - Tag stable under JSON whitespace and key order.
  - Each refusal; lookup shadowing.
- `container` marker:
  - Build every shipped recipe and run the agent's `--version`.
  - Build `claude-docs`; in `sanduk shell`, check `d2 --version` and `~/.claude/skills/d2/SKILL.md`.
  - Run one `sealed` task that renders a diagram.

## Size

Inference: 500-700 lines across loading, inheritance, rendering, kits and CLI. About as many again in tests. Plus 7 recipe files.

## Open questions

- **Skill token cost.** Inference: each installed skill's name and description enter every session's context. The cost depends on the number of skills, their description lengths and the agent. Measure it when `docs` ships: one task, with and without the kit, comparing the relay log's per-call `in=` counts.
