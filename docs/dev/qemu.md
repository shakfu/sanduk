# QEMU directly

Status: investigated from QEMU's documentation, 2026-09-20. Nothing built or measured; this host has no QEMU installed. Claims quote the documentation where they can; inference is marked. Decision: not implemented. It is the strongest design of the VM options and the only one with new artifacts to own; Kata under `--oci-runtime` gives the same boundary for a `daemon.json` entry ([microvms.md](microvms.md)).

Scope: what a `Runtime` on QEMU would have to supply, and which parts QEMU answers with a flag. Where this sits against the other boundaries is ranked in [microvms.md](microvms.md#recommendation-2026-09-20).

## Why this is not a rewrite of Docker

The earlier note said a direct VMM engine would rebuild what Docker does. That is right for Firecracker and crosvm and wrong for QEMU: the two costs that sound largest are a flag each, and the parts sanduk cannot borrow -- relay, policy, key handling, report, agent handlers -- already exist on the host and are engine-neutral.

What QEMU has to be given: a kernel, a root filesystem, an init, a channel for the agent's output, and a channel for its exit status. What it already has: an unprivileged isolated network with explicit forwarding, filesystem sharing, and KVM.

## The sealed network is one flag

`-netdev user` is "a user mode host network backend which requires no administrator privilege to run". `restrict=on` gives the containment: "If this option is enabled, the guest will be isolated, i.e. it will not be able to contact the host and no guest IP packets will be routed over the host to the outside. This option does not affect any explicitly set forwarding rules." `guestfwd` is the one exception sanduk needs: "Forward guest TCP connections to the IP address `server` on port `port` to the character device `dev` or to a program executed by `cmd:command`", with `guestfwd=tcp:10.0.2.100:1234-tcp:10.10.1.1:4321` as the documented form ([invocation](https://www.qemu.org/docs/master/system/invocation.html)).

```text
-netdev user,id=n0,restrict=on,guestfwd=tcp:10.0.2.100:80-tcp:127.0.0.1:PORT
-device virtio-net-pci,netdev=n0
```

The guest reaches one address and one port, which is the relay. Against the Docker path this drops four things: the `--internal` network, the bridge gateway bind, the network holder, and `wait_for_gateway`. It also makes the relay strictly less exposed than either alternative -- the Docker relay listens on the bridge, where every container on that bridge can reach it, and a gondolin relay listens on loopback, where every process on the host can. A `guestfwd` target is reached only through this VM's virtual network.

`key-safe` mode, which keeps the key on the host but leaves egress open, is `restrict=off` with the same forward.

## `/work` is one flag

`-virtfs local,path=HOST,mount_tag=work,security_model=none` shares a host directory over virtio-9p, which is built into QEMU; `-virtfs` is documented as "just a convenience shortcut for its generalized form `-fsdev` and `-device virtio-9p-pci`". The guest mounts it with `mount -t 9p -o trans=virtio work /work`.

The security model matters for two reasons. "In `passthrough` security model, files are stored using the same credentials as they are created on the guest. This requires QEMU to run as root." `none` "is same as passthrough except the server won't report failures if it fails to set file attributes like ownership", so files land with the QEMU process's own credentials. That is the answer to a problem the Docker path has: whatever uid the agent runs as inside the guest, the report and every file it writes are owned by the user who started sanduk. `image_uid`, the `AGENT_UID` build args and `Docker.keeps_mount_owner` exist to catch that mismatch, and here it cannot arise.

virtiofs is the faster share and costs a `virtiofsd` process plus a shared-memory backend (`-object memory-backend-file,share=on` or memfd). 9p costs nothing and an agent editing a source tree is not throughput-bound (inference).

Read-only mounts are `readonly=on` on the `-fsdev`, so `Mount(ro=True)` maps across.

## Use `q35`, not `-M microvm`

The microvm machine type is "a minimalist machine type without PCI nor ACPI support, designed for short-lived guests", supports "up to eight virtio-mmio devices" and no PCI device at all, and exists to cut boot time ([microvm](https://www.qemu.org/docs/master/system/i386/microvm.html)). A measured sanduk run takes ~35s, so boot time is noise, and q35 keeps virtio-pci, where `virtio-9p-pci` and every example in the documentation live.

## The three artifacts sanduk would own

### 1. A kernel and an initramfs

An OCI image carries neither. Options, cheapest first: borrow the host's `/boot/vmlinuz-$(uname -r)`, which is readable on most distributions but pins the guest kernel to the host's and is absent in a container-built CI job; take a distribution kernel package, as gondolin takes Alpine's `linux-virt`; build one. An initramfs is avoidable if the kernel has virtio-9p and the root filesystem driver built in rather than as modules -- which a distribution kernel usually does not, so in practice an initramfs is needed to load the modules and pivot (inference).

### 2. A root filesystem

Either an ext4 image (`mke2fs -d DIR image.ext4`, which needs no root) or the directory itself over 9p as the root, with `root=/dev/root rootfstype=9p rootflags=trans=virtio` (inference: documented for virtiofs, plausible for 9p, untested).

Nothing about QEMU requires Docker to produce that directory:

- `docker export` or `podman export` of the image sanduk already builds.

- `skopeo copy` plus `umoci unpack --rootless`, or `crane export`, for an image in a registry or local store. No daemon, no root: `--rootless` "implies `--uid-map=0:$(id -u):1`", and "it is almost always not possible to perfectly extract an OCI image with `--rootless`", `security.capabilities` xattrs among the casualties ([umoci-unpack(1)](https://github.com/opencontainers/umoci/blob/master/doc/man/umoci-unpack.1.md)).

- `buildah`, which builds a Containerfile daemonless and rootless.

- The distribution's own tooling: `mmdebstrap`, usable "without superuser privileges" ([README](https://gitlab.mister-muffin.de/josch/mmdebstrap)), or `apk --root`.

The coupling is to the recipe, not to Docker. A sanduk recipe is a Containerfile and a `RUN` line is executed by a container build; whichever tool runs that build is the dependency, and `docker` is only the one `Runtime.build_image` shells out to today. The last option is the only one that drops a build-time engine, and the price is describing each agent's image as a package list plus post-install commands instead -- a second recipe format beside the nine that exist, and the loss of the layer cache.

Unprivileged extraction loses ownership: files come out owned by the caller, not root. `unshare -r` maps the caller to uid 0 so a tar extraction and `mke2fs -d` in that namespace see root-owned files (inference, untested).

### 3. A guest init

Something inside has to mount `/work`, export the environment, run the agent's argv, report the exit status and power off. This is the piece gondolin writes in Zig as `sandboxd`, and sanduk's version is far smaller because the host keeps the relay, the policy, the key and the report: no network mediation, no programmable VFS, no snapshots, no SSH. A shell script as `/init` covers the happy path. The two parts that need care are below.

## Output and exit status

`launch()` reads the child's stdout line by line, hands anything that does not start with `{` to `reader.line`, and takes the agent's exit code from `proc.returncode`.

- **Output works as is.** A serial console on stdout (`-serial stdio` with `-nographic`) mixes kernel messages with the agent's JSONL, and the existing loop already tolerates non-JSON lines. `-device virtio-serial` plus a second chardev separates them if the mixing proves noisy.

- **Exit status does not.** `proc.returncode` would be QEMU's, and QEMU exits 0 whatever the guest ran. Three options: the init writes the code to a file in the shared directory, where the host reads it after the process ends; the init prints a sentinel line the engine parses, which puts a control channel in the same stream the agent writes; or `-device isa-debug-exit`, where a guest write to an I/O port exits QEMU with `(value << 1) | 1` -- x86-only and it cannot carry an arbitrary code (inference, from its use in test harnesses). The file is the least clever and the only one that cannot be forged by the agent printing a line.

## The run token must not go on the kernel command line

`-append` lands in `/proc/cmdline`, which every process in the guest can read, and in the host's `ps` output. `run_argv`'s bare `-e NAME` exists to keep the credential out of both. `-fw_cfg name=opt/sanduk/token,file=PATH` passes a host file to the guest instead, read from sysfs under `/sys/firmware/qemu_fw_cfg/` (inference: the option is documented, the guest path comes from the kernel driver and is unverified). A file in the shared directory also works, since the agent owns that directory anyway.

## Fit with `Runtime`

`run_argv` does not fit: QEMU takes none of `--name`, `--cpus`, `--memory`, `-v`, `-w`, `-e`, `--network` or `--entrypoint`, so the method would be overridden wholesale rather than shared. What else changes:

- `ensure_network`, `network_info`, `network_internal`, `hold_network_up`, `delete_network`, `wait_for_gateway`: gone. There is no network object; `restrict=on` plus a forward is the whole of it.

- `list_containers`, `stop`, `destroy`: a QEMU process is the container. Tracking by pid rather than by engine-assigned name, which `runs.claim`/`sweep` would have to learn.

- `hardening`: `--cap-drop`, `--init`, `no-new-privileges`, `--pids-limit` have no counterpart and no need for one inside a VM, except that a fork bomb then has the guest's memory as its only ceiling -- the same hole Kata has, for the same reason ([microvms.md](microvms.md)).

- `--cpus`, `--memory`: `-smp` and `-m`, so both flags keep working.

- `image_exists`, `image_tags`, `build_image`, `image_uid`, `AGENT_UID_LABEL`: replaced by whatever produces the kernel and rootfs.

So this is the same question gondolin raises: a backend that owns the whole run does not subclass `Runtime`, it sits beside it ([gondolin.md](gondolin.md)).

## An illustrative argv

Untested, and written out so the shape can be argued with rather than imagined:

```text
qemu-system-x86_64 -machine q35,accel=kvm -cpu host -smp 4 -m 4G -nographic -no-reboot \
  -kernel vmlinuz -initrd initrd.img \
  -append "console=ttyS0 quiet root=/dev/vda ro" \
  -drive if=virtio,file=rootfs.ext4,format=raw,readonly=on \
  -virtfs local,path=/home/me/work,mount_tag=work,security_model=none \
  -netdev user,id=n0,restrict=on,guestfwd=tcp:10.0.2.100:80-tcp:127.0.0.1:8931 \
  -device virtio-net-pci,netdev=n0 \
  -fw_cfg name=opt/sanduk/env,file=/run/user/1000/sanduk-env \
  -serial stdio
```

The agent's base URL inside the guest is then `http://10.0.2.100:80`, which is what `relay_root` would return for this engine.

## macOS

QEMU runs there with `-accel hvf`, so this is not Linux-only in the way the earlier note implied. It still buys nothing on macOS: Apple's `container` already runs every container as a lightweight VM.

## What to measure first

In order, because each one can end the exercise:

1. `restrict=on` with one `guestfwd` against a relay: does the agent reach the API and nothing else. This is the load-bearing claim and the cheapest to test with `curl` in any guest.

2. A 9p `/work` round trip, and who owns the files afterwards.

3. The agent's JSONL over a serial console through the existing `launch()` loop, unmangled and unbuffered.

4. An exit status out of the guest.

5. Only then: turning a sanduk image into a bootable rootfs, which is the part with no interesting unknowns and the most work.

## Verdict

Cheaper than the earlier note claimed, and still the largest piece of new machinery in any option considered. It needs no container engine at run time; whether it needs one at build time depends on whether the recipes stay Containerfiles. Build it if "sanduk with no container engine and no root" becomes a goal -- it is a better destination for that goal than gondolin, needing no Node and no second policy engine. Otherwise Kata gives the same kernel boundary for one configuration file.
