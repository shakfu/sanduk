# TODO

Ordered by how much they would change a decision, not by effort.

## High

## Medium

### Compressors

- [ ] **Trial shell-output compressors with claude: none, rtk, snip.** Run one task through `--agent claude --provider anthropic --mode sealed` in three images: stock, with [rtk](https://github.com/rtk-ai/rtk), and with [snip](https://github.com/edouard-claude/snip). Both install a Claude Code PreToolUse hook. Build the two variants as kits: a pinned `binary` tool, `hook: true`, and a claude `setup` running the global `init`. Pick a test- and git-heavy task; neither tool shrinks file reads, so a review task shows little. Run each arm 3 times with a fixed `--model`, `--stats-file` and `--log-bodies`.

  Compare cost per arm. The relay prices nothing for Anthropic, and `--budget` is refused there. Two figures, which should agree:
  - Claude Code's `total_cost_usd`, the `$` in the stats line. Its own estimate.

  - The relay log's per-call `in=`, `cache_write=`, `cache_read=` and `out=`, summed and priced at Anthropic's list rates. The stats line alone is not enough: it folds cache writes, billed above base input, into `in`.

  Also compare turns, wall time, and whether `REPORT.md` still finds what the stock arm found. Filters hide diff context and passing tests. Run with the default permission mode; a hook that allows its rewrite may bypass `--allowed-tools` (untested). See [docs/dev/compressors.md](docs/dev/compressors.md).

### Performance

- [ ] **prime-agent's built-in Python skills are not in the image.** Before the `UV_OFFLINE=1` default, installing them cost ~6 minutes per `sealed` run while uv waited out its network retries. Measured on 0.9.5, same image, model and task:

  | Run | Wall time |
  |-|-|
  | `sealed` | 399s |
  | `key-safe` | 27.2s |
  | `sealed` with `UV_OFFLINE=1` | 8.6s |

  Cause, from 0.9.5's `dist/core/kernel/bootstrap.js`: kernel readiness compares `.bootstrap-version` against the Python skills the session passes in. `--prime-agent-bootstrap` calls `ensureKernelPython()` with no skills, so the image records `"pythonSkills": []`. The archive ships 11 skills with a `pyproject.toml` (`edit`, `compact`, `goal`, `refine`, `websearch` and others). Every fresh container therefore runs `syncPythonSkills` when the kernel starts: one `uv pip install` per skill. That it happens once per run rather than per call is inferred, not timed. `PI_OFFLINE=1` does not reach uv. The destination is uv's default index, which is inferred from the code and not captured. Offline, each install fails after the wait, so those skills were already unavailable in `sealed` runs (inferred from the warning path; the run's stderr showed nothing).

  Mitigated: `sanduk-prime` defaults `UV_OFFLINE=1`, and the prime case in `make test-agents` went from 417-440s to 8.4s. Open: `key-safe` and `open` runs no longer install the skills unless run with `-e UV_OFFLINE=0`. The fix is installing them at build time, which no CLI does today.

  For upstream: `--prime-agent-bootstrap` could install the built-in Python skills, and offline mode could imply `UV_OFFLINE`.


### Untested

- [ ] **Kata Containers under `--oci-runtime`.** A VM per container on Linux, where Docker otherwise shares the host kernel. Measure `sealed` mode (the relay on the host gateway) and `/work` under `runtime-rs` with QEMU, then Cloud Hypervisor and Dragonball; the Go runtime and Firecracker are deprecated and a separate archive. This host meets every hardware prerequisite and lacks only Docker, so the measurement is one install away. Needs no sanduk code change, but one decision: Kata drops `--pids-limit` silently ([kata#13824](https://github.com/kata-containers/kata-containers/issues/13824)), so one of the four flags in `Docker.hardening` is inert and nothing says so. See [docs/dev/microvms.md](docs/dev/microvms.md).

- [ ] **Measure QEMU's `restrict=on` plus one `guestfwd` against the relay.** The load-bearing claim of a direct-QEMU backend, and cheap: a guest, a `curl` at the relay and a `curl` at anything else. If it holds, `sealed` mode needs no network, no bridge gateway, no holder and no `wait_for_gateway`, and the relay is reachable only through that VM. Then 9p `/work` ownership, then the agent's JSONL over a serial console through `launch()`. See [docs/dev/qemu.md](docs/dev/qemu.md).

- [ ] **Decide on a Gondolin backend.** A microVM per session with no container engine, no daemon and no root, which is the one boundary sanduk cannot reach today. It replaces `Runtime` rather than subclassing it: no networks, no bindable gateway, JSON image configs instead of Containerfiles, and no `--cpus` or `--memory` on its CLI. Keeping the relay is possible through `--tcp-map` to loopback. Worth building only for a host where Docker cannot be installed; otherwise Kata is cheaper. See [docs/dev/gondolin.md](docs/dev/gondolin.md).

- [ ] **Recipes under Docker and on amd64.** Every recipe was built and run on Apple's `container` 1.2.0, arm64. Under Docker on amd64, only hax's recipe is built, by the `docker` and `gvisor` jobs on every push; that covers the uid build args and one amd64 artifact. The scheduled `images` job covers the other agents but has not run since recipes landed. `docker image ls` in `destroy` is unexercised, and nothing builds `claude-docs`.

- [ ] **An agent task that uses a kit.** d2 and officecli ran inside `claude-docs`, but no agent has been given a task that needs them, in `sealed` or otherwise. Also measure what installed skills cost: one task with and without `--kit docs`, comparing the relay log's per-call `in=` counts.

- [ ] **docker-agent as a kit and recipe target.** Its `toolsets` list is declarative and closed, so a recipe can pin the whole tool surface: a kit installing an stdio MCP server, a recipe pinning it, a config naming it. No other agent allows that; the rest fix their tools in the binary. `--exec --json` and `base_url: ${OPENAI_BASE_URL}` both hold against a stub, and the call is `POST /v1/chat/completions`, already on the relay's route table. Three measurements gate another agent slot: whether `selfupdate` or `toolinstall` fires before the first completion, which decides `sealed`; the layer cost of a 126 MiB binary; and the Anthropic endpoint end to end. See [docs/dev/docker-agent.md](docs/dev/docker-agent.md).

- [ ] **Where prime reads skills.** Its handler has no `skills_dir`, so kits with skills are refused for it. pi reads `~/.agents/skills`; whether PrimeIntellect's build does is not measured.

- [ ] **A long run.** Most runs measured so far finish in ~35s. No agent has exhausted `--max-turns` or run long enough to trigger context compaction.

### Correctness

- [ ] **Each provider's route table is the paths one workload was seen to use.** A different task (web search, subagents, MCP) may call something else and get a 403. The failure is legible in the proxy log, but the fix is manual: `--proxy-allow-path`.

- [ ] **`--max-tokens-cap` on OpenAI's `/v1/responses` is unchecked.** Usage has been read from real Responses streams (codex and minima through the relay), but clamping `max_output_tokens` has not been tried against a real request.

- [ ] **OpenAI Chat Completions has no recorded live run.** Responses has run live through codex (`make test-agents`) and minima. `make test-live` runs a Chat Completions call only with `OPENAI_MODEL` set, and no result is recorded.

- [ ] **`stream_usage_option` is a guess for any openai-compatible server but llama.cpp.** Measured there, both directions: no field means no streamed usage, the field means full counts. A stricter server could reject it outright, and nothing has tried one.

- [ ] **The relay buffers request bodies fully in memory** before forwarding. Fine at 60KB; unexamined for file uploads.

- [ ] **The firewall preflight only detects an explicit Block entry.** An interpreter that would merely prompt is not caught, and the symptom is identical: a hang.

- [ ] **A timed-out run records no token count.** The reader's partial tally is discarded with the kill.

### Design

- [ ] **`sanduk-logs` grows without bound.** No rotation, no cap.

- [ ] **Recipe builds accumulate.** Each edit to a recipe or kit builds a new `sanduk-<recipe>:<hash>` image, and nothing deletes the old ones until `sanduk destroy`, which deletes them all. Deleting superseded tags after a build is the obvious fix; an older tag may belong to a run still in flight.

- [ ] **Kits waiting to ship.** `rtk` and `snip` wait for the compressor trial above. `quarto` needs a decision: offline Typst PDF only, or a larger kit with TinyTeX preinstalled, since LaTeX PDF fetches packages at run time. See [docs/dev/kits.md](docs/dev/kits.md).

- [ ] **Whether `key-safe` still has a use case.** It exists for `npm install`, `pip install` and `git clone` during a run; recipes now put dependencies in the image, where a `sealed` run fetches nothing. Census what is left: the kits declaring `egress`, which `check_kits` already refuses under `sealed`, and whether a real task needs the network during the run rather than at build. If little remains, `key-safe` is a compatibility mode to document rather than harden, and OpenShell's TLS-terminating egress policy never earns its cost ([docs/dev/openshell.md](docs/dev/openshell.md)).

## Low

### Design

- [ ] **`--effort`, `--bare` and `--permission-mode` are Claude Code's flags on the shared parser.** claude reads all three; hax reads `--effort` and `--bare` and refuses `--permission-mode`; minima refuses `--effort` and `--permission-mode` and ignores `--bare`; the other five ignore them. `--bare` is also refused beside a recipe's `instructions`. A `--` passthrough is the cheaper shape.

- [ ] **The placeholder container costs a VM boot and 256MB** for the duration of every relayed run (`key-safe`, `sealed`) on Apple's `container`, purely so the bridge exists before the relay binds. Worth checking whether a shorter-lived container or a retrying bind would do.

- [ ] **No container reuse.** Every run pays a fresh VM boot. Fine for the experiment; wrong if this ever runs in a loop.

- [ ] **Someday: re-implement in Go.** A static binary removes the Python 3.11 install step, which macOS does not provide. Go's `net/http` would also stream relay bodies. Go over Rust: the stdlib covers the relay and subprocess work, where Rust needs `tokio`, `hyper` and `rustls`. Costs: the entry-point plugin model, the importable package, and zero runtime dependencies (SQLite, TOML). Wait until the `Runtime` and `Agent` seams settle. Until then, ship through `uv tool` or PyApp.

### Nice to have

- [ ] `--report` copies `REPORT.md` out, but nothing collects other artifacts the agent writes outside the mount.

- [ ] The stream trace prints tool names only. Tool inputs would make a failed run easier to read, at the cost of terminal noise.

- [ ] No way to resume or re-attach to a `--keep` container from the runner. See [docs/dev/adoption.md](docs/dev/adoption.md).
