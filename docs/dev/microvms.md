# MicroVMs

Status: investigated, 2026-09-10; Kata section rewritten 2026-09-20 against `runtime-rs`, which replaced the Go runtime the earlier plan targeted. `--oci-runtime` is implemented; gVisor is measured, Kata is not. Claims cite primary sources; inference is marked.

Scope: whether sanduk should run agents in microVMs, and how.

## Where sanduk already has a VM boundary

- **macOS, Apple's `container`:** every container is its own lightweight VM. No change needed.

- **Linux, Docker:** the agent shares the host kernel. A kernel escape reaches the host, and in `sealed` mode the host is where the key lives: in the sanduk process, not the container.

So the gap is Linux only, and the threat it closes is kernel escape. Boot time does not matter: a measured run takes ~35s, so a microVM's faster start changes nothing.

## Driving a VMM directly

Firecracker, Cloud Hypervisor, QEMU and crosvm are virtual machine monitors, not container engines. A sanduk engine on one would have to rebuild what Docker does now:

- turn an OCI image into a root filesystem, and supply a kernel;

- rebuild the internal network the relay depends on;

- share `/work`. Firecracker has no filesystem sharing: its devices are virtio-net, virtio-block, a serial console and legacy controllers, and block devices are backed by pre-formatted files ([design.md](https://github.com/firecracker-microvm/firecracker/blob/main/docs/design.md)). `/work` would be a disk image copied in and out;

- stream the agent's stdout, and get its exit status back, over a serial console or vsock;

- need KVM, which exists only on Linux, so macOS gains nothing.

For Firecracker and crosvm that is a container runtime written again. QEMU is the exception, and the two bullets that sound most expensive are the two it answers outright.

### QEMU is the exception

Two of the bullets above are Firecracker's, not QEMU's. `-netdev user,restrict=on` with one `guestfwd` rule rebuilds the sealed network with no privilege, no bridge and no tap, and `-virtfs` shares `/work` with no separate daemon. What is left is a kernel, a root filesystem and a guest init: three artifacts sanduk would own, and no container engine at run time. See [qemu.md](qemu.md).

## An OCI runtime under Docker: yes

Docker picks the program that starts each container with `--runtime`. Two candidates:

- **Kata Containers:** a VM per container. Docker v26 or later launches one directly with the Kata shim, tested with QEMU as the VMM ([installation.md](https://github.com/kata-containers/kata-containers/blob/main/docs/installation.md)).

  - Since 4.0.0 the default shim is `runtime-rs`, the Rust rewrite; the Go runtime is still shipped but deprecated, in a separate `kata-go-static` archive ([4.0.0 release notes](https://github.com/kata-containers/kata-containers/releases/tag/4.0.0)). Latest release: 4.2.0, 2026-09-15.

  - `runtime-rs` supports QEMU, Cloud Hypervisor and Dragonball. Firecracker and StratoVirt are the Go runtime's alone ([hypervisors.md](https://github.com/kata-containers/kata-containers/blob/main/docs/hypervisors.md), 4.0.0 release notes).

  - Outside TEE setups, bind mounts use filesystem sharing, so `/work` should work.

  - **`--pids-limit` is silently dropped.** `runtime-rs` clears `resources.pids` in the container create path, and the grpc-to-OCI conversion inverts the check that copies it, so the guest cgroup keeps `pids.max = max`; the Go runtime clears the same field in `kata_agent.go` ([#13824](https://github.com/kata-containers/kata-containers/issues/13824), [#13826](https://github.com/kata-containers/kata-containers/pull/13826), both open 2026-09-13). One of the four flags in `Docker.hardening` is therefore inert under Kata, and Docker reports no error. What bounds a fork bomb is then the VM's memory, `default_memory = 2048`, not a pid ceiling. `--cap-drop`, `no-new-privileges` and `--init` are expected to reach the guest; that is inference, since #13824's reproduction asserted only the pids limit.

  - Host networking and Podman are unsupported. sanduk uses neither.

- **gVisor (`runsc`):** not a VM. It serves the container's system calls from a user-space kernel. Installed with `sudo runsc install`, run with `docker run --runtime=runsc` ([quick start](https://gvisor.dev/docs/user_guide/quick_start/docker/)). Its default platform needs no KVM (inference), so a standard CI runner can test it.

`--oci-runtime NAME` passes the name through as `docker run --runtime NAME`. Apple's engine refuses it. Docker's `default-runtime` in `daemon.json` does the same without sanduk, but for every container on the host.

## What is measured

| | `open` | `sealed` | `/work` |
| --- | --- | --- | --- |
| gVisor | CI job `gvisor`; by hand | CI job `gvisor`; by hand | CI job `gvisor`; by hand |
| Kata `runtime-rs`, QEMU | no | no | no |
| Kata `runtime-rs`, Cloud Hypervisor or Dragonball | no | no | no |
| Kata Go runtime, Firecracker | no | no | probably unsupported (inference, from Firecracker's device list) |

The relay works under gVisor. On 2026-09-11 the container suite passed under runsc 20260831 on docker-ce 29.8.0, for hax and claude, including two concurrent `sealed` runs and a round trip through `/work`. gVisor uses the container's network namespace, which sits on the Docker bridge, so the host gateway stays bindable and reachable.

Kata attaches its VM to the same namespace, so the relay should work there too. That is inference; Kata is in `TODO.md`.

## Measuring Kata

Needs a native Docker daemon v26 or later, `/dev/kvm`, `/dev/vhost-vsock`, and a user that can reach the Docker socket. This host qualifies: bare metal (`systemd-detect-virt` says `none`), AMD `svm`, `kvm_amd` loaded, both vhost devices present, 32 cores, 29GB. Docker is not installed on it, so every step below is untried.

1. Install `runtime-rs`. `kata-static-4.2.0-amd64.tar.zst` is 950MB and unpacks under `/opt/kata/`.

   ```sh
   v=4.2.0
   curl -fsSLO "https://github.com/kata-containers/kata-containers/releases/download/$v/kata-static-$v-amd64.tar.zst"
   sudo tar --zstd -xf "kata-static-$v-amd64.tar.zst" -C /
   ls /opt/kata/share/defaults/kata-containers/runtime-rs/
   sudo modprobe vhost_vsock vhost_net
   ```

   `ls` shows which `configuration-*-runtime-rs.toml` files this build ships. The release page publishes no checksum file; take the digest from the asset and record it.

2. Register the shim with Docker. `ConfigPath` is what selects the VMM, so each pass is a one-line edit here rather than a symlink. Merge into any existing `/etc/docker/daemon.json`:

   ```json
   {
     "runtimes": {
       "kata": {
         "runtimeType": "/opt/kata/runtime-rs/bin/containerd-shim-kata-v2",
         "options": {
           "ConfigPath": "/opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-runtime-rs.toml"
         }
       }
     }
   }
   ```

   ```sh
   sudo systemctl restart docker
   docker info --format '{{json .Runtimes}}' | grep -q kata
   ```

   Omitting `ConfigPath` lets `/etc/kata-containers/configuration.toml` take precedence over the packaged defaults, which is how a stale file from an earlier pass silently decides the VMM.

3. Confirm a VM. The guest kernel differs from the host's:

   ```sh
   uname -r
   docker run --rm --runtime kata debian:trixie-slim uname -r
   ```

4. Run the container suite, as the `gvisor` CI job does, for hax and then claude:

   ```sh
   make image AGENT=hax RUNTIME=docker
   SANDUK_RUNTIME=docker SANDUK_AGENT=hax SANDUK_NETWORK=sanduk-kata \
     SANDUK_OCI_RUNTIME=kata uv run pytest -q -m container
   ```

   This covers the `/work` round trip under sanduk's hardening flags, egress blocked on the internal network, the relay on the gateway, the key kept out, a wakeup, and two concurrent `sealed` runs.

5. Check the four things the suite does not assert, each of which is a flag sanduk sets and Kata may ignore:

   ```sh
   docker run --rm --runtime kata --pids-limit 8 debian:trixie-slim \
     sh -c 'cat /sys/fs/cgroup/pids.max'                      # expect "max": dropped, #13824
   docker run --rm --runtime kata --cap-drop ALL debian:trixie-slim \
     sh -c 'grep CapEff /proc/self/status'                    # expect 0000000000000000
   docker run --rm --runtime kata --init debian:trixie-slim \
     sh -c 'cat /proc/1/comm'                                 # expect docker-init
   docker run --rm --runtime kata --cpus 4 --memory 4G debian:trixie-slim \
     sh -c 'nproc; free -m | head -2'                         # does VM sizing follow the flags
   ```

   `--init` is the one with a mechanism that could fail outright: Docker bind-mounts `docker-init` from the host into the container, so it depends on file sharing rather than on a spec field.

6. Repeat steps 2 to 5 with `configuration-clh-runtime-rs.toml`, then `configuration-dragonball.toml`. Only QEMU is upstream-tested with Docker, so the other two are measurements of what happens. Firecracker needs the deprecated Go runtime from `kata-go-static`; it is expected to fail on `/work`, having no filesystem sharing, and its Kata configuration needs a block-device snapshotter Docker's default does not supply (inference).

7. Record in the table above: Kata version and shim, VMM, docker-ce version, which tests passed, and the first error for any that failed. Then tear down:

   ```sh
   docker network rm sanduk-kata
   ```

   Remove the `kata` entry from `daemon.json` and restart Docker when done. `/opt/kata` is left for a later pass.

Nothing here needs a sanduk code change. `--oci-runtime` already passes an arbitrary name through to `docker run --runtime`, and `kata` is a name `daemon.json` assigns. The one decision the measurement forces is what to do about `--pids-limit`: document that Kata ignores it, or have `run` say so when `--oci-runtime` is set, which means sanduk knowing which runtimes are VMs.

Not planned: a CI job. Whether GitHub-hosted runners expose `/dev/kvm` is unverified, and a 950MB download per run is the cost if they do.

## Alternative

Rootless Docker or Podman limits what a kernel escape reaches, with no VM. It narrows the impact rather than adding a boundary, and Podman's rootless networking rules out the relayed modes ([podman.md](podman.md)).

Docker Sandboxes (`sbx`) give a microVM per sandbox but replace the relay's policy and require a Docker account. Not integrated; see [sbx.md](sbx.md).

## Recommendation, 2026-09-20

After four investigations -- this note, [sbx.md](sbx.md), [gondolin.md](gondolin.md), [qemu.md](qemu.md) -- ranked by what each would change.

1. **Build no new backend yet.** Every candidate here closes kernel escape, a boundary with no known exploit in sanduk's path. The one unfixed hole that does not need an exploit is the unpinned kit `copy` file (`TODO.md`, Critical): editing one changes the image tag and no build refuses it, which is the pin's whole purpose. That outranks a second boundary.

   `TODO.md` is stale above it. `--log-dir` inside the mount, `sweep()` reading an unreachable engine as an empty one, the relay and holder outside the cleanup block, the unverified internal network and the shared `wakeup.json` were all fixed in 0.3.1; the entries remain. Read the CHANGELOG before ranking against that list.

2. **Say that `--pids-limit` is dropped under a VM runtime.** `--oci-runtime` shipped before Kata was measured, and the flag is inert under it, silently, in both Kata runtimes. The comment at `runtime.py:452` is false in a configuration sanduk already accepts. Details in the Kata bullet above; the fix is a sentence in `--help` or a note from `run`, not code.

3. **Kata when a VM boundary is wanted, and not before.** One `daemon.json` entry, no sanduk code, `SANDUK_OCI_RUNTIME` already wired into the container suite. Until then gVisor is the shipped boundary and Kata is a recipe, not pending work.

4. **sbx is closed. gondolin is conditional.** An account, telemetry on by default, no path rules and no arbitrary image are not a trade worth revisiting. gondolin stays open on one condition -- sanduk running where no container engine can be installed -- and if that becomes a goal, [qemu.md](qemu.md) is the better answer to it: no Node, no second policy engine, and `--cpus` and `--memory` survive.

5. **Concurrent sealed runs are isolated by the run token, not by the network.** Two runs share one bridge, so each agent can reach the other's relay port; the relay refuses it with `secrets.compare_digest` and a 401 (`proxy.py:299`), which is correct. It is worth stating, because "the network has no route off the host" reads as "each run is alone on it". QEMU's `guestfwd` would make that structural, since a forward belongs to one VM.

## Corrections

The first version of this note said a direct-VMM engine would have to "create tap devices and a bridge (root) to rebuild the internal network the relay depends on", and listed `/work` sharing among the costs. Both are Firecracker's problems, not QEMU's: `-netdev user,restrict=on` with one `guestfwd` rule needs no privilege, and `-virtfs` is built into QEMU. Corrected above, and investigated in [qemu.md](qemu.md).

A widely copied summary says Firecracker cannot pause. It can: `PATCH /vm` pauses and resumes it, and snapshots are supported ([snapshot-support.md](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md)).
