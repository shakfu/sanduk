# OpenShell

Status: investigated from source and documentation, 2026-09-16, at commit `b3e4ad4`. Not installed or run. Decision: do not integrate; record the comparison. Claims cite primary sources; inference is marked.

Scope: whether sanduk should run agents through NVIDIA OpenShell ([repo](https://github.com/NVIDIA/OpenShell), [docs](https://docs.nvidia.com/openshell/latest/index.html)).

Like `sbx` ([sbx.md](sbx.md)) and unlike docker-agent ([docker-agent.md](docker-agent.md)), this is a sandbox. It competes with `Runtime` and with the relay at once. It is the closest thing to sanduk yet examined: the core mechanism is the same.

## What it is

- Rust, Apache-2.0, 8,625 stars, repository created 2026-02-24. The README badge reads `status: alpha`.

- Three components: a gateway control plane, a per-sandbox supervisor, and a policy engine. Compute drivers are Docker, Podman, MicroVM, Kubernetes, and MXC on Windows.

- Install is `curl -LsSf .../install.sh | sh` or a Helm chart. The PyPI `openshell` package is the SDK only, not the CLI.

- Policy is declarative YAML across four domains. Filesystem (Landlock) and process are locked at sandbox creation; network and provider attachment are hot-reloadable.

- Sandboxes can be ephemeral: the CLI cleans up after the sandbox command exits (`crates/openshell-cli/src/run.rs:397`).

- Agents in the base image are Claude Code, OpenCode, Codex and Copilot CLI. pi and Ollama come from the community catalog, hermes and OpenClaw through NemoClaw.

- Telemetry is on by default. `OPENSHELL_TELEMETRY_ENABLED=false` disables it, and it can be compiled out with `--no-default-features --features defaults-without-telemetry`.

## The mechanism is sanduk's

Sandboxes hold a placeholder, never a real token ([`architecture/google-vertex-ai-provider.md:82`](https://github.com/NVIDIA/OpenShell/blob/main/architecture/google-vertex-ai-provider.md)). The placeholder resolves only when the request host, port and path match an authorized endpoint (`architecture/sandbox.md:226`). A credential used against any other endpoint is denied with `credential_endpoint_mismatch` (`crates/openshell-supervisor-network/src/proxy.rs:117`).

That is sanduk's relay: a per-run token in the container, the real key injected host-side, bound to one endpoint. Two projects arrived at it independently. The design bet holds.

They also refuse the same thing sanduk refuses. A credentialed endpoint that the proxy cannot inspect fails policy validation unless `allow_uninspected_credentials` is set explicitly, and that flag defaults to false (`architecture/security-policy.md:139`).

## Where the designs diverge

OpenShell terminates TLS with a per-sandbox ephemeral CA and plants it in the sandbox as `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` and `GIT_SSL_CAINFO`, all under `/etc/openshell-tls/` (`crates/openshell-sandbox/src/child_env.rs:13`). That buys method and path rules on arbitrary HTTPS hosts.

sanduk's relay is the endpoint rather than a man in the middle. No CA is installed, and nothing in the container trusts a certificate it would not otherwise trust. The cost is that the rule surface covers the model API only; every other destination is answered by having no route at all.

This resolves, for OpenShell's design, the HTTPS question left open in [sbx.md](sbx.md): the proxy sees plaintext because the sandbox trusts its CA.

## Against sanduk

| | sanduk | OpenShell |
|-|-|-|
| Credential off the container | `key-safe`, `sealed` | yes, placeholder plus endpoint binding |
| TLS interception | none | per-sandbox ephemeral CA, trusted in the sandbox |
| Egress rule granularity | exact path, model API only | method, path, query, GraphQL field, MCP tool, any host |
| Egress by calling binary | no | yes, by absolute path |
| In-container filesystem policy | none; the container is the boundary | Landlock read-only and read-write path sets |
| In-container process policy | `--cap-drop` | unprivileged user, reduced capabilities, syscall limits |
| `--allow-model`, `--max-tokens-cap` | yes | not found |
| Body log | `--log-bodies` | OCSF event stream; body capture not determined |
| Deleted after the run | yes | yes, for ephemeral sandboxes |
| Control plane | none beyond the engine | a gateway process |
| Maturity | 0.2.x | alpha, 7 months old |

Rule granularity is finer than sanduk's in every dimension except the model itself: `L7AllowDef` carries `method`, `path`, `query`, `operation_name`, `fields`, `tool` and `params` (`crates/openshell-policy/src/lib.rs:397`). An MCP tool call and a GraphQL field are both addressable. sanduk's `--proxy-allow-path` is a path list.

Two defaults differ in posture. The shipped `claude-code` provider profile permits `statsig.anthropic.com` and `sentry.io` alongside the API (`providers/claude-code.yaml`); sanduk's route table carries neither.

## Fit with `Runtime`

Poor, for the same reason as sbx. `Runtime.run_argv` renders Docker CLI flags, and `ensure_network` needs `network create --internal` with a bindable gateway. OpenShell exposes a gateway API and its own policy model instead. An `OpenShell` subclass would override nearly every method.

The relay is the larger problem. Provider binding and the relay do the same job, so integrating means picking one. Keeping the relay under OpenShell's proxy stacks two credential boundaries for no gain. Dropping it costs `--allow-model`, `--max-tokens-cap`, `--budget` and `--log-bodies`, and makes sanduk a wrapper.

## Options considered

1. **OpenShell as a `Runtime`.** Rejected above: duplicated credential boundary, or sanduk reduced to a wrapper.

2. **Borrow the rule shape.** `L7AllowDef`'s query and body matchers are how a model allowlist would be expressed generically. sanduk already has `--allow-model` and gets the same result with less machinery. No change earns its cost.

3. **Not integrated; record the comparison.** The placeholder-plus-binding design is now attested by two independent implementations, which is worth more than any component here.

Chosen: 3.

The one capability with no sanduk equivalent is policy *inside* the container: Landlock path sets, process limits, and egress bound to a calling binary. sanduk's position is that the container is the boundary, so a second boundary inside it is redundant. That position is a choice, not a consequence, and this is the first evidence that someone took the other side seriously. Worth revisiting only as its own design question, not as an integration.

## Unresolved

- **Whether the sandbox env holds a real key on any path.** The architecture docs say placeholders throughout, but the README also says credentials are "injected as environment variables at runtime", and the CLI warns when a user passes a credential through `--env` (`crates/openshell-cli/src/commands/common.rs:785`). The provider path looks clean; the `--env` path is the user's own doing. Not run, so not settled.

- **Body capture.** Whether the OCSF event stream records request and response bodies, as `--log-bodies` does.

- **Offline.** Whether a gateway and sandbox start with no network, against a local model.

- **The CA's blast radius.** The CA is per-sandbox and ephemeral (inference, from the naming in `child_env.rs`); whether one sandbox's CA can reach another was not checked.
