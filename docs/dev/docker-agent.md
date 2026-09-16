# Docker Agent (`docker agent`)

Status: investigated, 2026-09-16. Binary v1.140.0 run locally on darwin-arm64; source read at commit `5d959fa`. Nothing built into a sanduk image. Decision: do not add it as an agent yet; use it to exercise kits and recipes first. Claims cite primary sources; inference is marked.

Scope: whether docker-agent should join the `sanduk.agents` registry.

It is not `sbx`. sbx is a sandbox and competes with `Runtime` ([sbx.md](sbx.md)); docker-agent is an agent CLI and competes for a slot in `Agent`. The two ship from the same vendor and sbx lists Docker Agent among its supported agents.

## What it is

- A Go CLI, also installable as a `docker` CLI plugin. Apache-2.0, 3,328 stars, repository created 2025-09-01 ([repo](https://github.com/docker/docker-agent), [docs](https://docker.github.io/docker-agent/)).

- One agent is a YAML document: model, instruction, and an explicit `toolsets` list. Multi-agent teams, MCP servers, RAG, hooks and budgets are further keys. The schema has 15 top-level keys and 35 definitions (`agent-schema.json`).

- Tools come from three places: built-ins needing no daemon, MCP servers over stdio or HTTP, and MCP servers run as containers through the Docker MCP Gateway ([tools](https://docker.github.io/docker-agent/concepts/tools)).

- Providers include OpenAI, Anthropic, Gemini, Bedrock, Mistral and any OpenAI-compatible endpoint ([providers](https://docker.github.io/docker-agent/providers/overview)).

- Releases carry six platform binaries and no checksum file. linux-arm64 is 126 MiB (132,065,088 bytes), linux-amd64 133 MiB ([v1.140.0](https://github.com/docker/docker-agent/releases/tag/v1.140.0)).

- Telemetry is on by default, posted to `https://api.docker.com/events/v1/track` (`pkg/telemetry/client.go:107`). `TELEMETRY_ENABLED=false` disables it (`pkg/telemetry/utils.go:32`).

## Fit with `Agent`

`sanduk.agent` asks four questions. All four have answers.

| Question | Answer | Verified |
|-|-|-|
| Which image carries it | one static binary, as hax | no; only the darwin build was run |
| Headless flags | `run --exec --json <file> "<prompt>"` | yes |
| Endpoint and credential variables | `base_url: ${OPENAI_BASE_URL}` in YAML, key from `OPENAI_API_KEY` | yes |
| How to read the stream | NDJSON, `type` discriminator, terminal `stream_stopped` | yes |

A recipe needs a `binary` section and a `copy` section for the YAML. Both are already in `sections.TYPES`; no new machinery.

### What was run

Cassette replay, for the record shapes:

```text
./docker-agent run --exec --json \
  --fake e2e/testdata/cassettes/TestExec_OpenAI_ToolCall.yaml \
  e2e/testdata/fs_tools.yaml "How many files in testdata/working_dir?"
```

A stub HTTP server on `127.0.0.1:8899` returning an SSE completion, for the wire shape, with this config:

```yaml
models:
  relayed:
    provider: openai
    model: gpt-4o
    base_url: ${OPENAI_BASE_URL}
agents:
  root:
    model: relayed
    instruction: Be brief.
```

The stub saw one request: `POST /v1/chat/completions`, `Authorization: Bearer $OPENAI_API_KEY`, `stream: true`. That is sanduk's `OPENAI_CHAT` route, so the relay allowlist needs no new path. Anthropic `base_url` reaches the SDK the same way (`pkg/model/provider/anthropic/client.go:86`), but was not run.

### Reading the stream

- Terminal record is `{"type":"stream_stopped","reason":"normal"}`. A replay whose cassette did not match the prompt ended after `agent_info` with no terminal record and exit status 0, so the exit code cannot stand in for one. `Reader.finish()` returning None is the only detection.

- `token_usage` is a third accounting shape, matching neither Claude Code nor hax. `usage.cost` accumulates over the run; `usage.input_tokens` is the last request's prompt tokens, not a run total; cache counts appear only under `usage.last_message`. Summing overcounts input, taking the last value undercounts it.

- `partial_tool_call` arrives once per streamed argument fragment: ten records for one 31-character argument string. A trace must drop them.

- `--json` rejects every confirmation unconditionally (`pkg/cli/runner.go:121`), so a run needs `--safety autonomous` or `--safety restricted` to use a tool. The container is the boundary, as with hax.

## What it adds

Its tool surface is declarative and closed. A config declaring `type: filesystem` alone reported `available_tools: 9`, with no shell. Every other sanduk agent fixes its tool set in its own binary, so the tool list can only be narrowed by prompt or by an agent-specific flag. This is the only candidate whose whole tool surface a recipe can pin.

## What it costs

1. 126 MiB of binary in the image, several times any current agent's addition. The resulting image size is unmeasured.

2. No published checksum to pin against. The recipe would carry a hash sanduk computed itself, with no independent digest to check it. hax is pinned the same way, so this is not a regression.

3. `pkg/selfupdate/selfupdate.go:57` and `pkg/toolinstall/registry.go:319` both reach `api.github.com` at run time. Whether either sits on the startup path is not determined; if one does, `sealed` breaks.

4. Overlapping layers to suppress: its own sandbox flags (`--sandbox`, `--sbx`, `--template`), its own skills directory (`pkg/skills`, `pkg/promptfiles/lookup.go:62`), safety modes, budgets, hooks and OCI agent packaging. A handler must refuse or map the sanduk flags that collide, as `Hax.check` does.

5. `ref: docker:` toolsets need the MCP Gateway and a docker socket in the container, which defeats the boundary. Built-in toolsets and build-time stdio MCP servers only. The daemon is otherwise unused: the sole socket dial is Docker Model Runner's (`pkg/model/provider/dmr/dmrmodels/resolve.go:216`).

## Options considered

1. **An agent now.** A hax-shaped handler, a recipe, one YAML file. Roughly a day. It widens a matrix that is already unmeasured: no agent task has used a kit, recipes are unexercised under Docker and on amd64, and no run has lasted longer than ~35s.

2. **Kit and recipe proving ground first.** Build a kit installing an stdio MCP server, a recipe pinning it, and a config whose `toolsets` name it. This closes the open "agent task that uses a kit" item with the one agent whose tool surface a recipe can pin end to end. The agent slot follows if the image cost and the sealed run hold up.

3. **Not integrated.** The shipped agents already cover all four providers.

Chosen: 2.

## Unresolved

- **Startup network.** Whether `selfupdate` or `toolinstall` fires before the first completion, which decides whether `sealed` works at all.

- **The Anthropic endpoint.** `base_url` reaches the SDK in source; no run has gone through sanduk's Anthropic relay.

- **Image size.** The layer cost of a 126 MiB binary on `debian:trixie-slim`, against the current per-agent images.

- **Writable state.** `--session-db` defaults under a data directory. Which paths it needs writable inside the container is unexamined.

- **Long runs.** Whether a `stream_stopped` record arrives on timeout or context compaction, and what `reason` carries.
