"""Container runtimes.

Every subprocess call to a container engine lives here, so adding Docker or
Podman is a subclass plus a registry entry rather than a grep for "container"
across the package.

Apple's `container` and `docker` are implemented. A third engine must supply
four things: the CLI name, the verb that deletes a container (`rm`, not
`delete`), how `network inspect` reports the gateway, and whether the host
bridge needs a placeholder container to exist at all.

`run_argv` is shared, which is not an accident of the two engines happening to
agree: `--name`, `--cpus`, `--memory`, `-v`, `-w`, `-e`, `--network` and
`--entrypoint` are the Docker CLI surface that Apple's engine adopted, and a
third engine that wants a different one overrides the method.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sanduk.errors import AgentboxError
from sanduk.util import note, run

# Every container sanduk starts is named from this, and every container it will
# stop or delete is found by it. Nothing else is touched.
CONTAINER_PREFIX = "sanduk-"

# Set by every shipped Containerfile to the uid its agent runs as.
AGENT_UID_LABEL = "sanduk.agent-uid"

# How long the network holder sleeps when the caller does not say. A day, which
# is what it always was; `run` sizes it to the run instead.
HOLDER_SECONDS = 86400


@dataclass(frozen=True)
class Container:
    """One container as sanduk sees it, whatever the engine's columns say."""

    name: str
    image: str
    state: str


@dataclass(frozen=True)
class Mount:
    """One host directory inside the container, beside the working directory.

    Rendered with `--mount` rather than `-v`: both engines spell the read-only
    flag the same way there, where `-v host:dest:ro` is Docker's alone.
    """

    host: Path
    dest: str
    ro: bool = False

    def argv(self) -> list[str]:
        spec = f"type=bind,source={self.host},target={self.dest}"
        return ["--mount", (spec + ",readonly") if self.ro else spec]


@dataclass
class ContainerSpec:
    """One container to run. Engine-neutral; `Runtime.run_argv` renders it."""

    name: str
    image: str
    command: list[str] = field(default_factory=list)
    cpus: int = 4
    memory: str = "4G"
    mount: tuple[Path, str] | None = None  # (host dir, path inside)
    mounts: list[Mount] = field(default_factory=list)  # everything else
    inherit_env: list[str] = field(default_factory=list)  # -e NAME: value from us
    env: list[str] = field(default_factory=list)  # -e K=V
    network: str | None = None
    detach: bool = False
    entrypoint: str | None = None
    oci_runtime: str | None = None  # what starts the container: runsc, Kata


class Runtime:
    """A container engine driven through its CLI."""

    name = ""
    cli = ""
    delete_verb = "delete"
    install_hint = ""
    # Whether the program that starts a container can be swapped, for gVisor
    # or Kata in place of runc. See docs/dev/microvms.md.
    takes_oci_runtime = False
    # vmnet-style engines only create the host bridge while a container is
    # attached; Docker creates it with the network. Podman's netavark does not
    # (docs/dev/podman.md).
    needs_network_holder = False
    # Appended when the relay cannot bind the gateway, where an engine knows a
    # likely reason. --proxy is the whole point of sanduk, so a failure there
    # has to say what to do about it.
    gateway_hint = ""
    # Flags every container gets on top of its spec. The agent runs as the
    # image's unprivileged user and only reads, writes and forks, so no
    # capability it could keep is one it needs. Not --read-only: every shipped
    # agent writes under $HOME, and a tmpfs per agent would be a path this
    # class has no business knowing. Per engine, because Apple's CLI has
    # neither --pids-limit nor --security-opt.
    hardening: tuple[str, ...] = ("--cap-drop", "ALL", "--init")
    # Whether a bind mount keeps host ownership inside the container, so the
    # agent writes the workdir only as its owner. Apple's engine maps it.
    keeps_mount_owner = False

    # --- preflight ----------------------------------------------------------

    def require(self) -> None:
        if shutil.which(self.cli) is None:
            raise AgentboxError(f"`{self.cli}` not found on PATH. {self.install_hint}")
        self.require_service()

    def require_service(self) -> None:
        """Engines with a background daemon check it here."""

    def require_run(self) -> None:
        """`require`, plus whatever else this engine needs to start an agent.
        Only `run` calls it: the other verbs need the engine to answer, no more."""
        self.require()

    # --- the engine's own service -------------------------------------------

    def service_status(self) -> str:
        """One line: whether this engine can take a container right now."""
        try:
            self.require()
        except AgentboxError as e:
            return str(e)
        return f"{self.cli} is running"

    def _unmanaged(self) -> AgentboxError:
        return AgentboxError(
            f"{self.cli}'s service is managed outside sanduk. Start or stop it "
            "the way your system does."
        )

    def service_start(self) -> None:
        raise self._unmanaged()

    def service_stop(self) -> None:
        raise self._unmanaged()

    # --- images -------------------------------------------------------------

    def image_exists(self, image: str) -> bool:
        raise NotImplementedError

    def image_tags(self, repository: str) -> list[str]:
        """Every `repository:tag` this engine holds for one repository."""
        raise NotImplementedError

    def delete_image(self, image: str) -> None:
        # `image delete` against `image rm`: the same split as containers, so
        # the same verb answers for both.
        r = run([self.cli, "image", self.delete_verb, image], capture_output=True)
        note(f"deleted image {image}" if r.returncode == 0 else f"no image {image}")

    def build_args(self) -> list[str]:
        """Flags `build_image` adds for this engine."""
        return []

    def image_uid(self, image: str) -> int | None:
        """The agent's uid from the image's label, or None if it has none."""
        return None

    def build_image(self, image: str, containerfile: Path | str) -> None:
        cf = Path(containerfile).resolve()
        if not cf.is_file():
            raise AgentboxError(f"no Containerfile at {cf}")
        note(f"building {image} from {cf}")
        argv = [self.cli, "build", *self.build_args(), "-t", image, "-f", str(cf)]
        r = run([*argv, str(cf.parent)])
        if r.returncode != 0:
            raise AgentboxError(f"build failed (exit {r.returncode})")

    # --- networks -----------------------------------------------------------

    def network_info(self, name: str) -> tuple[str, str] | None:
        """(gateway, subnet), or None if the network does not exist."""
        raise NotImplementedError

    def ensure_network(self, name: str, internal: bool = True) -> tuple[str, str]:
        """Create `name` if it is not already there, and return (gateway, subnet).

        `internal` is the only difference between sanduk's two relayed modes:
        without a route off the host, the relay is the one address a container
        can reach; with one, the relay still holds the key and the container
        reaches everything else too.
        """
        info = self.network_info(name)
        if info:
            return info
        kind = "internal" if internal else "routable"
        note(f"creating {kind} network {name}")
        argv = [self.cli, "network", "create"]
        if internal:
            argv.append("--internal")
        r = run([*argv, name], capture_output=True)
        if r.returncode != 0:
            # Two runs that both found no network both create it, and the second
            # create fails. The network it wanted is the first run's: wait for it.
            # Apple's engine creates one in under 0.1s.
            for _ in range(20):
                info = self.network_info(name)
                if info:
                    return info
                time.sleep(0.5)
            raise AgentboxError(f"could not create network {name}: {r.stderr.strip()}")
        info = self.network_info(name)
        if not info:
            raise AgentboxError(f"network {name} created but has no address")
        return info

    def delete_network(self, name: str) -> None:
        r = run([self.cli, "network", self.delete_verb, name], capture_output=True)
        note(f"deleted network {name}" if r.returncode == 0 else f"no network {name}")

    def hold_network_up(
        self, network: str, image: str, seconds: int = HOLDER_SECONDS
    ) -> str | None:
        """Start a placeholder container so the host bridge exists.

        Without it the proxy cannot bind the gateway address and would have to
        fall back to 0.0.0.0, which puts it on Wi-Fi and LAN too. Returns the
        container to tear down, or None when the engine does not need one.

        `seconds` is how long it holds. It used to be a flat day, which is a
        ceiling nothing announced: a longer run lost its bridge mid-flight and
        failed as if the network had broken. The caller sizes it to the run.
        """
        if not self.needs_network_holder:
            return None
        spec = ContainerSpec(
            name=f"sanduk-hold-{uuid.uuid4().hex[:6]}",
            image=image,
            cpus=1,
            memory="256M",
            network=network,
            detach=True,
            entrypoint="sleep",
            command=[str(seconds)],
        )
        r = run(self.run_argv(spec), capture_output=True)
        if r.returncode != 0:
            raise AgentboxError(f"could not start network holder: {r.stderr.strip()}")
        return spec.name

    # --- containers ---------------------------------------------------------

    def list_containers(self, prefix: str = "") -> list[Container]:
        """Containers whose name starts with `prefix`, running or not."""
        raise NotImplementedError

    def shell_argv(self, image: str) -> list[str]:
        """An interactive shell in `image`, mounting nothing and joining no
        network. Not run_argv: that builds no tty, and the caller hands this
        straight to the terminal rather than reading a JSON stream off it."""
        return [self.cli, "run", "--rm", "-it", "--entrypoint", "sh", image]

    def run_argv(self, spec: ContainerSpec) -> list[str]:
        argv = [
            self.cli,
            "run",
            "--name",
            spec.name,
            "--cpus",
            str(spec.cpus),
            "--memory",
            spec.memory,
            *self.hardening,
        ]
        if spec.detach:
            argv.append("-d")
        if spec.mount:
            host, dest = spec.mount
            argv += ["-v", f"{host}:{dest}", "-w", dest]
        for mount in spec.mounts:
            argv += mount.argv()
        # Bare -e NAME: the engine inherits the value from this process, so the
        # value stays out of the argv and out of the host's process list.
        for key in spec.inherit_env:
            argv += ["-e", key]
        for kv in spec.env:
            argv += ["-e", kv]
        if spec.network:
            argv += ["--network", spec.network]
        if spec.entrypoint:
            argv += ["--entrypoint", spec.entrypoint]
        if spec.oci_runtime:
            argv += ["--runtime", spec.oci_runtime]
        argv.append(spec.image)
        return argv + spec.command

    def stop(self, name: str) -> None:
        """Stop a container, leaving it on disk. Already stopped is not an error."""
        run([self.cli, "stop", name], capture_output=True)

    def destroy(self, name: str, keep: bool = False) -> None:
        if keep:
            note(
                f"keeping container {name} (`{self.cli} inspect {name}` exposes "
                f"the API key; `{self.cli} {self.delete_verb} {name}` when done)"
            )
            return
        self.stop(name)
        r = run([self.cli, self.delete_verb, name], capture_output=True)
        if r.returncode != 0:
            note(f"could not delete {name}: {r.stderr.strip()}")
        else:
            note(f"deleted {name}")


class AppleContainer(Runtime):
    """Apple's `container`: Linux containers as lightweight VMs on macOS."""

    name = "apple"
    cli = "container"
    delete_verb = "delete"
    install_hint = "Install from github.com/apple/container."
    needs_network_holder = True

    def require_service(self) -> None:
        st = run([self.cli, "system", "status"], capture_output=True)
        if st.returncode != 0 or "running" not in st.stdout:
            raise AgentboxError(
                "container system is not running. Start it with: container system start"
            )

    def service_start(self) -> None:
        if run([self.cli, "system", "start"]).returncode != 0:
            raise AgentboxError("could not start the container service")

    def service_stop(self) -> None:
        note("this stops the service for everything on the machine, not just sanduk")
        if run([self.cli, "system", "stop"]).returncode != 0:
            raise AgentboxError("could not stop the container service")

    def image_exists(self, image: str) -> bool:
        out = run([self.cli, "image", "list"], capture_output=True)
        if out.returncode != 0:
            return False
        name, _, tag = image.partition(":")
        tag = tag or "latest"
        for line in out.stdout.splitlines()[1:]:
            f = line.split()
            if len(f) >= 2 and f[0].endswith(name) and f[1] == tag:
                return True
        return False

    def image_tags(self, repository: str) -> list[str]:
        out = run([self.cli, "image", "list"], capture_output=True)
        if out.returncode != 0:
            return []
        found = []
        for line in out.stdout.splitlines()[1:]:
            f = line.split()
            if len(f) >= 2 and (f[0] == repository or f[0].endswith("/" + repository)):
                found.append(f"{f[0]}:{f[1]}")
        return found

    def network_info(self, name: str) -> tuple[str, str] | None:
        r = run([self.cli, "network", "inspect", name], capture_output=True)
        if r.returncode != 0:
            return None
        try:
            st = json.loads(r.stdout)[0]["status"]
            return str(st["ipv4Gateway"]), str(st["ipv4Subnet"])
        except (ValueError, KeyError, IndexError):
            return None

    def list_containers(self, prefix: str = "") -> list[Container]:
        # Columns, because this CLI has no --format. ID IMAGE OS ARCH STATE ...
        r = run([self.cli, "list", "-a"], capture_output=True)
        if r.returncode != 0:
            return []
        out = []
        for line in r.stdout.splitlines()[1:]:
            f = line.split()
            if len(f) >= 5 and f[0].startswith(prefix):
                out.append(Container(name=f[0], image=f[1], state=f[4]))
        return out


class Docker(Runtime):
    """The Docker CLI against a local daemon.

    Two verbs differ from Apple's: `rm` deletes a container, and the gateway
    comes out of the IPAM block rather than a status object. The bridge is
    created with the network, so no placeholder container is needed.

    `--proxy` needs the bridge gateway to be an address on this host. That
    holds for a daemon on this kernel and not for one inside a VM -- Docker
    Desktop, Colima, Lima -- where the bridge lives in the VM. There the bind
    fails and the run stops, rather than the relay silently listening somewhere
    the container cannot reach.
    """

    name = "docker"
    cli = "docker"
    # A shared kernel, unlike Apple's VM per container, so the two flags that
    # engine does not have are the two that matter most here. The pid ceiling
    # is a fork bomb's, not a workload's: node plus a shell plus ripgrep is two
    # orders of magnitude below it.
    hardening = (
        *Runtime.hardening,
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "1024",
    )
    delete_verb = "rm"
    keeps_mount_owner = True
    install_hint = "Install from docs.docker.com/get-docker/."
    takes_oci_runtime = True
    needs_network_holder = False
    gateway_hint = (
        " A daemon inside a VM (Docker Desktop, Colima, Lima) keeps the bridge "
        "in the VM rather than on this host; --proxy needs a daemon running on "
        "this kernel."
    )

    def require_service(self) -> None:
        r = run([self.cli, "info", "--format", "{{.ServerVersion}}"], capture_output=True)
        if r.returncode != 0:
            raise AgentboxError(docker_daemon_error(r.stderr or ""))

    def require_run(self) -> None:
        # The snap reports its base as the daemon's OS, whatever the host runs:
        # "Ubuntu Core 24" on an Ubuntu 24.04 host. DockerRootDir would also
        # tell, but the snap lets a user move it.
        super().require_run()
        info = [self.cli, "info", "--format", "{{.OperatingSystem}}"]
        if run(info, capture_output=True).stdout.startswith("Ubuntu Core"):
            raise AgentboxError(
                "docker is the snap package, which cannot run an agent. Its "
                "AppArmor profile blocks every exec under --security-opt "
                "no-new-privileges, and its /tmp is not this host's. Install "
                "Docker Engine from docs.docker.com/engine/install/."
            )

    def build_args(self) -> list[str]:
        # A native daemon keeps host ownership on a bind mount, so an agent at
        # uid 1000 cannot write a workdir owned by uid 1001. Root keeps the
        # default: uid 0 inside would be root in the image.
        if os.getuid() == 0:
            return []
        return [
            "--build-arg",
            f"AGENT_UID={os.getuid()}",
            "--build-arg",
            f"AGENT_GID={os.getgid()}",
        ]

    def image_uid(self, image: str) -> int | None:
        labels = [self.cli, "image", "inspect", "--format", "{{json .Config.Labels}}"]
        r = run([*labels, image], capture_output=True)
        try:
            return int((json.loads(r.stdout) or {})[AGENT_UID_LABEL])
        except (ValueError, KeyError, TypeError):
            return None

    def image_exists(self, image: str) -> bool:
        # inspect rather than a parsed listing: it answers the same for a tag, a
        # digest and an id, and the exit status is the answer.
        return (
            run([self.cli, "image", "inspect", image], capture_output=True).returncode
            == 0
        )

    def image_tags(self, repository: str) -> list[str]:
        fmt = "{{.Repository}}:{{.Tag}}"
        out = run(
            [self.cli, "image", "ls", "--format", fmt, repository], capture_output=True
        )
        if out.returncode != 0:
            return []
        return [line for line in out.stdout.splitlines() if not line.endswith(":<none>")]

    def network_info(self, name: str) -> tuple[str, str] | None:
        r = run([self.cli, "network", "inspect", name], capture_output=True)
        if r.returncode != 0:
            return None
        try:
            config = json.loads(r.stdout)[0]["IPAM"]["Config"][0]
            return str(config["Gateway"]), str(config["Subnet"])
        except (ValueError, KeyError, IndexError):
            return None

    def list_containers(self, prefix: str = "") -> list[Container]:
        # --format over columns: a named field cannot shift under a value that
        # contains a space, and an unknown field fails loudly at the template.
        r = run(
            [self.cli, "ps", "-a", "--format", "{{.Names}}\t{{.Image}}\t{{.State}}"],
            capture_output=True,
        )
        if r.returncode != 0:
            return []
        out = []
        for line in r.stdout.splitlines():
            f = line.split("\t")
            if len(f) == 3 and f[0].startswith(prefix):
                out.append(Container(name=f[0], image=f[1], state=f[2]))
        return out


def docker_daemon_error(
    stderr: str, platform: str = sys.platform, systemd: bool | None = None
) -> str:
    """Why `docker info` failed, with the command that fixes it on this host."""
    found = re.search(r"\b(?:unix|tcp|ssh|npipe)://[^\s;]+", stderr)
    endpoint = found.group(0).rstrip(":,.") if found else ""
    where = f"the docker daemon at {endpoint}" if endpoint else "the docker daemon"
    if "permission denied" in stderr.lower():
        return (
            f"permission denied on {where}. Add yourself to the docker group "
            "(sudo usermod -aG docker $USER) and log in again. Membership is "
            "root-equivalent on this host."
        )
    if endpoint and not endpoint.startswith("unix://"):
        # Starting a local daemon would not help; the CLI points elsewhere.
        return (
            f"{where} is not reachable. Check that host, or point DOCKER_HOST or "
            "`docker context use` at a local daemon."
        )
    if platform == "darwin":
        fix = "open -a Docker, or colima start"
    elif "/run/user/" in endpoint:
        fix = "systemctl --user start docker"
    elif systemd if systemd is not None else shutil.which("systemctl"):
        fix = "sudo systemctl start docker"
    else:
        fix = "sudo service docker start"
    return f"{where} is not reachable. Start it with `{fix}`, then re-run."


RUNTIMES: dict[str, type[Runtime]] = {"apple": AppleContainer, "docker": Docker}
# Engines tried in order when --runtime is not given. Apple's engine exists only
# on macOS; elsewhere a `container` on PATH is some other program.
RUNTIME_ORDER: dict[str, tuple[str, ...]] = {"darwin": ("apple", "docker")}
FALLBACK_ORDER = ("docker",)


def runtime_order(platform: str = sys.platform) -> tuple[str, ...]:
    return RUNTIME_ORDER.get(platform, FALLBACK_ORDER)


def default_runtime(platform: str = sys.platform) -> str:
    """The first engine in this platform's order whose CLI is on PATH.

    Falls back to the first in the order, so the not-found error names it.
    """
    order = runtime_order(platform)
    return next((n for n in order if shutil.which(RUNTIMES[n].cli)), order[0])


def get_runtime(name: str | None = None) -> Runtime:
    name = name or default_runtime()
    try:
        return RUNTIMES[name]()
    except KeyError:
        known = ", ".join(sorted(RUNTIMES))
        raise AgentboxError(f"unknown runtime {name!r}; known: {known}") from None


def wait_for_gateway(gateway: str, timeout: float = 30) -> bool:
    """Poll until the gateway address is bindable on this host."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.socket()
            s.bind((gateway, 0))
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False
