# Token compressors: rtk, caveman, snip

Status: investigated 2026-09-15 from each project's docs and source on its default branch. None installed or measured in a sanduk image. Decision: ship none by default; trial rtk and snip with claude. Claims cite primary sources; inference is marked.

Scope: whether sanduk's agent images should carry a tool that cuts the tokens an agent reads or writes.

- rtk: <https://github.com/rtk-ai/rtk>

- caveman: <https://github.com/JuliusBrussee/caveman>

- snip: <https://github.com/edouard-claude/snip>

## Baseline

One `--agent codex --provider openai --mode sealed` review run on this repository:

- 129,549 input tokens, 102,806 of them cached (79%).

- 4,021 output tokens (3% of the total).

- 7 relayed calls, 60.3s wall.

Two consequences follow for the tools below:

- Output is 3% of tokens, so a tool that shortens replies has little to cut.

- Cached input is billed at a discount. A cut in shell output lowers cost by less than it lowers tokens. rtk states this itself ([How savings work](https://github.com/rtk-ai/rtk#how-savings-work)).

## What each tool is

- **rtk** (Apache-2.0, Rust): rewrites shell commands to `rtk <cmd>` and filters their output. 100+ commands. Installs as an agent hook or plugin.

- **snip** (MIT, Go): the same design as rtk. Filters are YAML files, not compiled code. 132 filters.

- **caveman**: two separate parts.

  - Skill (MIT): a rule file that makes the model reply tersely.

  - Proxy (BSL-1.1 runtime, MIT CLI): a local process between agent and provider. It compresses request content and keeps originals in local SQLite for recall.

## File reads are not compressed

A "review every file" task is mostly file reads. Neither shell filter shrinks them by default.

- rtk rewrites `cat`, `head` and `tail` to `rtk read` ([`src/discover/rules.rs:127-128`](https://github.com/rtk-ai/rtk/blob/master/src/discover/rules.rs)). `rtk read` defaults to `--level none`, full content ([`src/main.rs:111-112`](https://github.com/rtk-ai/rtk/blob/master/src/main.rs)). The README table's "signatures and structure over full bodies" applies only with `-l aggressive`.

- snip has no `cat` filter. Its Files/Search filters are `ls`, `find`, `grep`, `rg`, `diff`, `wc`, `tree` ([README](https://github.com/edouard-claude/snip#132-built-in-filters)).

Both tools gain on test, build, git and log output.

## rtk against snip

| | rtk | snip |
|-|-|-|
| Release build | x86_64 musl; aarch64 glibc | static Go |
| claude | PreToolUse hook | PreToolUse hook |
| codex | AGENTS.md + RTK.md instructions; no interception | PreToolUse hook, Codex >=0.131.0 (sanduk pins 0.153.4) |
| opencode | own plugin | third-party `opencode-snip@latest`, unpinned |
| pi | own extension | via community `@hsingjui/pi-hooks` |
| hermes | own plugin | none |
| hax, prime | none | none |
| Telemetry | opt-in, off by default; `RTK_TELEMETRY_DISABLED=1` ([TELEMETRY.md](https://github.com/rtk-ai/rtk/blob/master/docs/TELEMETRY.md)) | none found: no `net/http` import in 55 non-test Go files |
| Repo-supplied filters | `.rtk/filters.toml` skipped until trusted by SHA-256 ([`src/hooks/trust.rs`](https://github.com/rtk-ai/rtk/blob/master/src/hooks/trust.rs)); `RTK_TRUST_PROJECT_FILTERS=1` bypasses | `.snip/` skipped until `snip trust` pins its SHA-256 |
| Full output on failure | SQLite store, `rtk recall <id>` | tee files, `mode = "failures"` |

Sources: the [rtk README](https://github.com/rtk-ai/rtk#supported-ai-tools) and [snip README](https://github.com/edouard-claude/snip#supported-ai-tools), unless cited.

rtk's `trust.rs` names the risk the gate exists for: a committed filter file can hide malicious code, suppress scanner output, or rewrite command output. In sanduk `/work` is the user's repository, so this applies.

## caveman

### Skill

Against sanduk:

- It adds about 1-1.5k input tokens per turn ([HONEST-NUMBERS](https://github.com/JuliusBrussee/caveman/blob/main/docs/HONEST-NUMBERS.md)). At 7 calls that is 7-10.5k input tokens, against at most 4,021 output tokens to cut. Inference: net-negative for this workload.

- It shortens prose. The prose in `REPORT.md` is the run's result.

- Its docs disagree. The README reports a 65% average output cut over ten prompts. HONEST-NUMBERS says no reviewed output-reduction result is published.

### Proxy

- **Position:** it sits between the agent and the provider, where sanduk's relay sits. The chain would be agent, caveman, relay, upstream. `--log-bodies` would record compressed requests.

- **License:** the engine, proxy and `shrink` are BSL-1.1. Hosting for third parties needs a commercial license. Each version converts to Apache-2.0 on the earlier of 2030-06-21 or four years after release ([README](https://github.com/JuliusBrussee/caveman#license)).

- **Telemetry:** on by default, sent to `https://api.caveman.so/telemetry/cli`. `DO_NOT_TRACK=1` disables it ([SECURITY.md](https://github.com/JuliusBrussee/caveman/blob/main/SECURITY.md)). In `key-safe` mode the container has a route off the host, so it would send.

- **Runtime:** Node.js 22.13+. sanduk's node images are `node:22-slim`.

- **Codex:** the command-output shrink hook is skipped for Codex ([README](https://github.com/JuliusBrussee/caveman#wrap-any-agent)).

- **Evidence:** a 54-run Claude Code benchmark reports -33.2% input tokens, 18 of 18 answer checks passed ([WRAP-BENCHMARK](https://github.com/JuliusBrussee/caveman/blob/main/docs/WRAP-BENCHMARK.md)). This is the most specific number of the three projects. It was run in their harness, on Claude Code only.

## Constraints for any image integration

- **Install at build time.** `sealed` has no route off the host, so nothing can be fetched at run time. Pin a release binary by checksum, as the `hax` and `minima` recipes' `binary` sections do. Do not use `curl | sh`.

- **Use the global install only.** `rtk init -g` and `snip init --agent codex` write under `$HOME` in the image. Project-scoped modes write `AGENTS.md`, `.clinerules` or `.windsurfrules` into `/work`.

- **Never set `RTK_TRUST_PROJECT_FILTERS`.**

- **Architecture:** Apple's `container` on Apple silicon runs arm64 Linux. rtk's aarch64 build links glibc, so it runs on the Debian-based images and not on Alpine.

- **Information loss:** filters drop diff context, collapse passing tests and truncate grep lines. Full output is recoverable only if the agent asks for it.

## Options considered

1. **rtk in every image.** Covers claude, opencode, pi and hermes with real interception. Codex, the default agent, gets prompt text only.

2. **snip in the codex image.** The only real interception for codex. Depends on the unresolved hook question below.

3. **caveman proxy.** Largest measured input cut, but it duplicates the relay's position and brings BSL-1.1 and default-on telemetry.

4. **caveman skill.** Rejected: likely net-negative here, and it degrades `REPORT.md`.

5. **None by default.** Measure first.

Chosen: 5, then a three-arm trial with claude, the one agent both tools hook natively.

## Plan

Tracked in [TODO.md](../../TODO.md): one task through `--agent claude --provider anthropic`, in a stock image, an rtk image and a snip image, 3 runs each. Cost comes from Claude Code's `total_cost_usd` and from the relay log's per-call counts priced at list rates. The stats line folds cache writes into `in`, so it cannot price a run alone. Codex stays out until its hook rewriting is confirmed (below).

Alternative: the larger costs in the baseline are file reads and context re-sent on every call. Model choice and narrower `--mount` scopes act on those directly, as does `--max-turns` for claude, hax, hermes and minima; codex ignores it.

## Unresolved

- **Codex hook rewriting.** snip says Codex >=0.131.0 accepts `updatedInput`. [openai/codex#18491](https://github.com/openai/codex/issues/18491) is open and quotes "PreToolUse hook returned unsupported updatedInput". The issue may cover only non-shell tools. Not determined without a run on 0.153.4.

- **`--allowed-tools` bypass.** snip's codex handler returns `permissionDecision: "allow"` with each rewrite ([`internal/hook/codex.go`](https://github.com/edouard-claude/snip/blob/master/internal/hook/codex.go)). Irrelevant for codex, whose sandbox sanduk disables. Inference: a claude hook that allows its rewrite may pass a command `--allowed-tools` would refuse. Not tested.

- **caveman telemetry in `sealed`.** Whether a failed telemetry POST delays the proxy when no route exists.
