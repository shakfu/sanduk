# Gondolin

Status: investigated from documentation and the CLI source, 2026-09-20. Not installed or measured: this host has no QEMU. Proposed decision: not integrated. Kata under `--oci-runtime` gives the same VM boundary without a second image format ([microvms.md](microvms.md)). Revisit for one case only: a host that cannot install a container engine. Claims cite primary sources; inference is marked.

Scope: whether sanduk should run agents through Gondolin ([repo](https://github.com/earendil-works/gondolin), [docs](https://earendil-works.github.io/gondolin/)). Where this sits against the other boundaries is ranked in [microvms.md](microvms.md#recommendation-2026-09-20).

## What it is

- **Isolation:** one Linux microVM per session, QEMU by default, `krun` experimental ([backends.md](https://github.com/earendil-works/gondolin/blob/main/docs/backends.md)). Apache-2.0. npm `@earendil-works/gondolin` 0.12.0, published 2026-05-19; the repository was last pushed 2026-07-06.

- **Host requirements:** QEMU and Node.js 23.6.0 or later ([cli.md](https://github.com/earendil-works/gondolin/blob/main/docs/cli.md)). Guest assets are ~200MB, cached in `~/.cache/gondolin/images/`. `builtin-image-registry.json` ships prebuilt `alpine-base` for `aarch64` and `x86_64`. No account, no telemetry documented. macOS and Linux; no Windows.

- **Network:** no NAT. The host runs a userspace network stack, classifies each outbound TCP flow as HTTP, TLS, SSH or explicitly mapped, and drops everything else ([network.md](https://github.com/earendil-works/gondolin/blob/main/docs/network.md)). The gateway `192.168.127.1` is inside that stack, not an address on the host. UDP is limited to port 53; no QUIC, no HTTP/2, no HTTP/3 ([limitations.md](https://github.com/earendil-works/gondolin/blob/main/docs/limitations.md)).

- **Egress policy:** `--allow-host HOST_PATTERN` with `*` wildcards. Host and port only; path rules exist only as JavaScript `onRequest` hooks in the SDK.

- **Credentials:** `--host-secret NAME@HOST[,HOST]` puts a placeholder in the guest environment and substitutes the real value into outbound request headers, for the listed hosts only. Request bodies, URL paths and responses are not substituted ([secrets.md](https://github.com/earendil-works/gondolin/blob/main/docs/secrets.md)). This is sanduk's `key-safe` property by another mechanism.

- **Filesystem:** `--mount-hostfs HOST:GUEST[:ro]`, `--mount-memfs GUEST`, served by a host-side VFS provider. The rootfs is ephemeral; `/root`, `/tmp`, `/var/log` and friends are tmpfs ([workloads.md](https://github.com/earendil-works/gondolin/blob/main/docs/workloads.md)).

- **Lifecycle:** `gondolin exec [options] -- CMD` creates a VM, runs the command, prints stdout and stderr, and exits with the command's status. Disposal is the default; there is no delete verb to call.

- **Images:** built from a JSON config, Alpine only, via `gondolin build --config`. Packages come from `alpine.rootfsPackages`, extra steps from `postBuild.commands`. An `oci.image` field instead exports a container image's filesystem as the rootfs, and that export runs `docker` or `podman` ([custom-images.md](https://github.com/earendil-works/gondolin/blob/main/docs/custom-images.md)).

## Against sanduk

| | sanduk, docker | sanduk, apple | gondolin |
|-|-|-|-|
| Kernel boundary on Linux | shared (gVisor optional) | n/a | VM |
| Key off the container | `key-safe`, `sealed` | same | yes, header substitution |
| Egress rule granularity | exact path | exact path | host:port; paths need JS hooks |
| `--allow-model`, `--max-tokens-cap` | yes | yes | no |
| Body log | `--log-bodies` | same | no (hooks could) |
| Deleted after the run | yes | yes | yes |
| Arbitrary image | yes | yes | Alpine build, or an OCI export via docker/podman |
| `--cpus`, `--memory` | yes | yes | SDK only, no CLI flag |
| Account | no | no | no |
| Host dependencies | container engine | container engine | QEMU, Node 23.6+, and docker/podman for OCI rootfs |

## Fit with `Runtime`

Poor, and poor differently from sbx. sbx has no equivalent of the pieces `Runtime` renders; gondolin has equivalents with different names and a different boundary shape.

- **Networks.** `ensure_network`, `network_info`, `network_internal`, `hold_network_up`, `delete_network` and `wait_for_gateway` all assume a bridge whose gateway is a host address. Gondolin has no network object and no bindable gateway. The relay would bind `127.0.0.1` and the guest would reach it through `--tcp-map sanduk-relay:80=127.0.0.1:PORT`, which is the documented shape for a local Postgres. Mapped flows are raw TCP, so gondolin's own HTTP mediation and secret substitution do not apply to them; sanduk's relay keeps the whole policy. The cost is the one sbx option 1 carries: a relay every process on the host can reach, guarded only by the run token.

- **Images.** `build_image` takes a Containerfile. Gondolin takes JSON. Either every recipe gains a second form, or `oci.image` exports the image sanduk already builds, which keeps the docker dependency and adds QEMU to it.

- **Containers.** `list_containers`, `stop` and `destroy` back `sanduk ps`, `sanduk rm` and `runs.sweep`'s ownership record. Gondolin registers sessions as `~/.cache/gondolin/sessions/<uuid>.json` and lists them with `gondolin list`, keyed by owning pid. That is a second source of truth for who holds a credential.

- **Hardening.** `--cap-drop ALL`, `--init`, `no-new-privileges` and `--pids-limit` have no counterpart; the VM replaces them. `AGENT_UID_LABEL`, `image_uid` and `keeps_mount_owner` also fall away: the guest runs as root and the host-side VFS provider writes as the sanduk user (inference, from the VFS provider design).

- **Resources.** `host/bin/gondolin.ts` on `main` has no `--cpus` and no `--memory`, and no `GONDOLIN_` variable for either; `sandbox.cpus` and `sandbox.memory` exist only in `VM.create`. sanduk's two flags would be silently dropped.

- **Environment.** Only `--env KEY=VALUE`, so the run token would appear in the host's process list. `run_argv`'s bare `-e NAME` exists to prevent exactly that.

So a `Gondolin(Runtime)` would override every method except `require`, and `cli.run`'s relay block would need a second path for a loopback relay with no holder and no gateway wait.

## Options considered

1. **Gondolin CLI as a backend, relay kept.** Bind the relay to loopback, reach it with `--tcp-map`, allow no hosts. Keeps path, model and token policy and the body log. Costs: Node 23.6+ on the host; a JSON image config per agent beside every Containerfile; the token in argv; `--cpus` and `--memory` dropped; a relay any host process can reach; a second run path in `cli.run`.

2. **Gondolin's policy, relay dropped.** `--allow-host` plus `--host-secret` per provider. Loses the path allowlist, `--allow-model`, `--max-tokens-cap`, `--log-bodies` and arbitrary `openai-compat` upstreams unless they are rewritten as JavaScript hooks.

3. **A bundled Node shim on the SDK.** Regains `cpus`, `memory`, streaming control and hooks. It puts a JavaScript component in a package whose selling point is no runtime dependencies.

4. **Not integrated.** Kata under `--oci-runtime` gives a VM per container on Linux and keeps the images, the internal network, the relay and the hardening flags.

Proposed: 4, with one reservation. Gondolin is the only candidate that needs no container engine, no daemon and no root: option 1 with an Alpine-built image would run sanduk where Docker cannot be installed. If that host matters, option 1 is the shape; if it does not, Kata is strictly cheaper.

## Unresolved

- **Streaming.** `gondolin exec` prints stdout and stderr, but whether it forwards the agent's JSON stream unbuffered is unverified. The SDK's `stdout: "pipe"` does; the CLI's mode is not documented.

- **`/work` ownership.** Whether an agent in the guest can write a host directory mounted with `--mount-hostfs`, and as which uid the host provider creates files.

- **x86_64 under QEMU without KVM acceleration.** Prebuilt `alpine-base` exists for `x86_64`, and `sandbox.accel` is a QEMU knob, but boot time on this class of host is unmeasured.

- **Per-agent images.** Every shipped agent would need an Alpine package set: `node` and `npm` for claude, codex, opencode and pi; `python3` and `uv` for hermes and prime-agent; static binaries for hax and minima. Whether each agent runs on musl is unverified.

- **Maintenance.** npm 0.12.0 is four months old as of this note, against a repository pushed two months ago. Whether releases track `main` is unknown.

## Not this

Gondolin also ships a pi extension that runs pi's `read`, `write`, `edit` and `bash` tools inside a VM ([pi-gondolin.ts](https://github.com/earendil-works/gondolin/blob/main/host/examples/pi-gondolin.ts)). That sandboxes an agent's tools from inside the agent, which is the opposite direction from sanduk putting the whole agent in a box. It is not an integration point.
