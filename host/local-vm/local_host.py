#!/usr/bin/env python3
"""Manage a separate local x64 Windows Server VM that hosts native Windows containers."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Callable
import uuid


SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parents[1]
GIB = 1024 ** 3
MINIMUM_FREE = 45 * GIB
STOP_FREE = 8 * GIB
MINIMUM_RAM = 12 * GIB
IMAGE = "winhoo-container-host:0.1.0-arm64"
CONTAINER = "winhoo-container-host"
SSH_PORT = 2228
VIEWER_PORT = 8016
OWNER = "io.openhoo.winhoo.local-host"
INSTANCE = "io.openhoo.winhoo.instance"
SCHEMA = "io.openhoo.winhoo.schema"


class HostError(RuntimeError):
    pass


def private_file(path: Path, contents: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(contents)
    path.chmod(0o600)


def normalize_host_key(value: str) -> str:
    fields = value.strip().split()
    try:
        raw = base64.b64decode(fields[1], validate=True)
    except (IndexError, ValueError) as exc:
        raise HostError("The bootstrap receipt contains an invalid SSH host public key.") from exc
    if fields[0] != "ssh-ed25519" or len(raw) != 51 or not raw.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"):
        raise HostError("Expected an ED25519 SSH host public key in the bootstrap receipt.")
    return " ".join(fields[:2])


class LocalHost:
    def __init__(self, state_dir: Path | str = ROOT / ".local" / "local-host", context: str | None = None, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run, free_bytes: Callable[[], int] | None = None, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.directory = Path(state_dir).expanduser().resolve()
        self.context = context
        self.runner = runner
        self.free_bytes = free_bytes or (lambda: shutil.disk_usage(ROOT).free)
        self.clock = clock
        self.sleep = sleep

    def command(self, argv: list[str], *, check: bool = True, capture: bool = True, timeout: float | None = 60) -> subprocess.CompletedProcess:
        try:
            result = self.runner(argv, stdin=subprocess.DEVNULL, capture_output=capture, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except FileNotFoundError as exc:
            raise HostError(f"Required executable is unavailable: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise HostError(f"Command timed out: {argv[0]}") from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "").strip()[-5000:]
            raise HostError(f"{argv[0]} failed ({result.returncode}): {detail}")
        return result

    def docker(self, state: dict[str, Any], *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        return self.command(["docker", "--context", state["context"], *args], **kwargs)

    def load(self) -> dict[str, Any]:
        try:
            state = json.loads((self.directory / "state.json").read_text(encoding="utf-8"))
            if state["schema"] != 1:
                raise ValueError("schema")
            uuid.UUID(state["uuid"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HostError(f"No valid local-host state at {self.directory}.") from exc
        return state

    def save(self, state: dict[str, Any]) -> None:
        temporary = self.directory / "state.json.tmp"
        private_file(temporary, json.dumps(state, indent=2) + "\n")
        temporary.replace(self.directory / "state.json")

    def engine(self, state: dict[str, Any] | None = None, *, require_resources: bool = False) -> dict[str, Any]:
        saved_context = state.get("context") if state else None
        if self.context and saved_context and self.context != saved_context:
            raise HostError("The local VM belongs to a different Linux Docker context.")
        context = self.context or saved_context or self.command(["docker", "context", "show"]).stdout.strip()
        try:
            endpoint = json.loads(self.command(["docker", "context", "inspect", context]).stdout)[0]["Endpoints"]["docker"]["Host"]
            if state and endpoint != state["endpoint"]:
                raise HostError("The saved Linux Docker context endpoint changed; refusing to operate on its resources.")
            if not endpoint.startswith("unix://"):
                raise HostError("The local VM needs a local Unix-socket Docker context; remote engines cannot mount local bootstrap/shared paths.")
            info = json.loads(self.command(["docker", "--context", context, "info", "--format", "{{json .}}"]).stdout)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise HostError("Invalid Docker context or engine metadata.") from exc
        if info.get("OSType") != "linux" or info.get("Architecture") not in ("aarch64", "arm64"):
            raise HostError("The supplied local host image requires a Linux ARM64 Docker engine.")
        if require_resources and info.get("MemTotal", 0) < MINIMUM_RAM:
            raise HostError("The Linux Docker engine needs at least 12 GiB RAM before starting the Windows host VM.")
        return {"context": context, "endpoint": endpoint, "engineRamGiB": round(info["MemTotal"] / GIB, 2), "freeGiB": round(self.free_bytes() / GIB, 2)}

    def doctor(self) -> dict[str, Any]:
        state = self.load() if (self.directory / "state.json").exists() else None
        info = self.engine(state, require_resources=True)
        required = STOP_FREE if state else MINIMUM_FREE
        if self.free_bytes() < required:
            raise HostError(f"Local storage has {self.free_bytes()/GIB:.1f} GiB free; at least {required/GIB:.0f} GiB is required {'to resume this VM' if state else 'before creating this VM'}. No files were pruned.")
        return {**info, "minimumFreeGiB": required // GIB, "mode": "x64 Windows Server emulated on ARM64; native process-isolated Windows containers inside"}

    @staticmethod
    def labels(state: dict[str, Any]) -> dict[str, str]:
        return {OWNER: "true", INSTANCE: state["uuid"], SCHEMA: "1"}

    def verify_labels(self, state: dict[str, Any], labels: dict[str, str] | None) -> None:
        if not labels or any(labels.get(key) != value for key, value in self.labels(state).items()):
            raise HostError("Resource ownership does not match this local Windows host; refusing to change it.")

    def inspect(self, state: dict[str, Any]) -> dict[str, Any]:
        self.engine(state)
        if not re.fullmatch(r"[0-9a-f]{64}", state.get("containerId") or ""):
            raise HostError("No full immutable container ID is recorded for this local host.")
        try:
            record = json.loads(self.docker(state, "container", "inspect", state["containerId"]).stdout)[0]
            if record["Id"] != state["containerId"]:
                raise HostError("The inspected container ID does not match the saved local host.")
            self.verify_labels(state, record["Config"].get("Labels"))
            return record
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise HostError("Invalid local-host container metadata.") from exc

    def ports_available(self) -> None:
        sockets = []
        try:
            for port in (SSH_PORT, VIEWER_PORT):
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(server)
                server.bind(("127.0.0.1", port))
        except OSError as exc:
            raise HostError(f"A local winhoo port is already occupied ({port}); refusing to reuse another service.") from exc
        finally:
            for server in sockets:
                server.close()

    def prepare(self, state: dict[str, Any]) -> None:
        for path in (self.directory / "oem", self.directory / "shared" / "bootstrap"):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        private_key = self.directory / "id_ed25519"
        public_key = self.directory / "id_ed25519.pub"
        if private_key.exists() != public_key.exists():
            raise HostError("The local SSH identity is incomplete; refusing to replace its remaining key file.")
        if not private_key.exists():
            self.command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"winhoo-local-host-{state['uuid']}", "-f", str(private_key)])
        (self.directory / "id_ed25519").chmod(0o600)
        for name in ("install.bat", "Provision-WinhooContainerHost.ps1"):
            source = SOURCE / "oem" / name
            if not source.is_file():
                raise HostError(f"Local host bootstrap source is missing: {source}")
            shutil.copyfile(source, self.directory / "oem" / name)
        shutil.copyfile(ROOT / "dependencies.json", self.directory / "oem" / "dependencies.json")
        shutil.copyfile(self.directory / "id_ed25519.pub", self.directory / "oem" / "authorized_keys")
        private_file(self.directory / "oem" / "instance-id", state["uuid"] + "\n")
        settings = {"HOST": "WINHOO-HOST", "VERSION": "2022", "EDITION": "core", "PLATFORM": "x64", "KVM": "N", "NETWORK": "slirp", "CPU_CORES": "2", "RAM_SIZE": "4G", "DISK_SIZE": "64G", "DISK_TYPE": "sata", "DISK_FMT": "raw", "ALLOCATE": "N", "LANGUAGE": "English", "REGION": "en-US", "KEYBOARD": "en-US", "USERNAME": "WinhooHost", "PASSWORD": secrets.token_urlsafe(36), "VERIFY": "Y", "USER_PORTS": "22"}
        if not (self.directory / "guest.env").exists():
            private_file(self.directory / "guest.env", "".join(f"{key}={value}\n" for key, value in settings.items()))
        config = f"Host *\n    StrictHostKeyChecking yes\n    BatchMode yes\n\nHost winhoo-local-host 127.0.0.1\n    HostName 127.0.0.1\n    Port {SSH_PORT}\n    User WinhooHost\n    IdentityFile \"{(self.directory/'id_ed25519').as_posix()}\"\n    IdentitiesOnly yes\n    HostKeyAlias winhoo-local-host-{state['uuid']}\n    UserKnownHostsFile \"{(self.directory/'known_hosts').as_posix()}\"\n"
        if any(character in str(self.directory) for character in ('"', "\n", "\r")):
            raise HostError("The local state path contains unsupported SSH configuration characters.")
        private_file(self.directory / "ssh_config", config)
        if not shutil.which("ssh"):
            raise HostError("OpenSSH client is missing.")

    def up(self) -> dict[str, Any]:
        if (self.directory / "state.json").exists():
            state = self.load()
            self.doctor()
            if state.get("containerId"):
                record = self.inspect(state)
                if not record["State"].get("Running"):
                    self.ports_available()
                    self.docker(state, "start", state["containerId"])
                return self.summary(state)
            self.ports_available()
            self.prepare(state)
        else:
            info = self.doctor()
            self.ports_available()
            self.directory.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.directory.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise HostError("The local-host state directory already exists without valid state; inspect it before retrying.") from exc
            identity = str(uuid.uuid4())
            state = {"schema": 1, "uuid": identity, "context": info["context"], "endpoint": info["endpoint"], "image": IMAGE, "containerId": None, "volume": f"winhoo-container-host-{identity[:8]}"}
            self.save(state)
            self.prepare(state)
        self.docker(state, "build", "--platform", "linux/arm64", "--file", str(SOURCE / "Dockerfile.arm64"), "--tag", IMAGE, str(SOURCE), capture=False, timeout=None)
        if self.free_bytes() < MINIMUM_FREE:
            raise HostError("At least 45 GiB free space is required before launching a fresh Windows host. Image preparation completed; no VM was launched, and state is retained for a retry.")
        volume = ["volume", "create"]
        for key, value in self.labels(state).items():
            volume.extend(["--label", f"{key}={value}"])
        volume.append(state["volume"])
        self.docker(state, *volume)
        volume_record = json.loads(self.docker(state, "volume", "inspect", state["volume"]).stdout)[0]
        self.verify_labels(state, volume_record.get("Labels"))
        create = ["create", "--name", CONTAINER, "--hostname", "WINHOO-HOST", "--platform", "linux/arm64", "--memory", "6g", "--cpus", "2", "--restart", "no", "--stop-timeout", "120", "--env-file", str(self.directory / "guest.env"), "--publish", f"127.0.0.1:{SSH_PORT}:22/tcp", "--publish", f"127.0.0.1:{VIEWER_PORT}:8006/tcp", "--mount", f"type=volume,source={state['volume']},target=/storage", "--mount", f"type=bind,source={self.directory/'oem'},target=/oem,readonly", "--mount", f"type=bind,source={self.directory/'shared'},target=/shared"]
        for key, value in self.labels(state).items():
            create.extend(["--label", f"{key}={value}"])
        create.append(IMAGE)
        state["containerId"] = self.docker(state, *create).stdout.strip()
        self.save(state)
        self.inspect(state)
        self.docker(state, "start", state["containerId"])
        return self.summary(state)

    def receipt(self, state: dict[str, Any]) -> dict[str, Any] | None:
        path = self.directory / "shared" / "bootstrap" / "host-status.json"
        if not path.is_file():
            return None
        if path.stat().st_size > 262144:
            raise HostError("The local host bootstrap receipt is unexpectedly large.")
        try:
            receipt = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not isinstance(receipt, dict) or receipt.get("instanceId") != state["uuid"]:
            raise HostError("The bootstrap receipt belongs to a different local-host instance.")
        return receipt

    def pin(self, state: dict[str, Any], receipt: dict[str, Any]) -> None:
        value = receipt.get("sshHostPublicKey")
        if not isinstance(value, str):
            raise HostError("The ready bootstrap receipt omitted its SSH host public key.")
        expected = f"winhoo-local-host-{state['uuid']} {normalize_host_key(value)}\n"
        path = self.directory / "known_hosts"
        if path.exists():
            if path.read_text(encoding="ascii") != expected:
                raise HostError("The local Windows host SSH key changed; refusing to replace its pin.")
        else:
            private_file(path, expected)

    def ssh(self, *remote: str) -> list[str]:
        return ["ssh", "-F", str(self.directory / "ssh_config"), "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "winhoo-local-host", *remote]

    def stop(self, state: dict[str, Any] | None = None) -> None:
        state = state or self.load()
        record = self.inspect(state)
        if record["State"].get("Running"):
            self.docker(state, "stop", "--time", "120", state["containerId"], timeout=150)

    def guard_disk(self, state: dict[str, Any]) -> None:
        free = self.free_bytes()
        if free < STOP_FREE:
            self.stop(state)
            raise HostError(f"Local storage fell to {free/GIB:.1f} GiB free. The owned Windows host VM was stopped to preserve at least 8 GiB headroom; its disk was retained.")

    def wait(self, timeout: float = 7200) -> dict[str, Any]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise HostError("Wait timeout must be finite and positive.")
        state = self.load()
        self.inspect(state)
        deadline = self.clock() + timeout
        last_phase = None
        last_error = "No bootstrap receipt yet."
        print(f"Windows Server installation uses software emulation. Viewer: http://127.0.0.1:{VIEWER_PORT}", file=sys.stderr)
        while self.clock() < deadline:
            self.guard_disk(state)
            record = self.inspect(state)
            if not record["State"].get("Running"):
                logs = self.docker(state, "logs", "--tail", "60", state["containerId"], check=False)
                raise HostError(f"The Windows host container stopped (exit {record['State'].get('ExitCode')}).\n{logs.stdout or ''}{logs.stderr or ''}")
            receipt = self.receipt(state)
            if receipt:
                phase = receipt.get("phase") or receipt.get("status")
                if phase != last_phase:
                    print(f"winhoo local host: {phase}", file=sys.stderr)
                    last_phase = phase
                if receipt.get("status") == "failed":
                    raise HostError(f"Windows host provisioning failed: {receipt.get('error') or receipt.get('details') or phase}")
                if receipt.get("status") == "ready":
                    if str(receipt.get("user", "")).lower() != "winhoohost":
                        raise HostError("The ready receipt has an unexpected SSH user.")
                    self.pin(state, receipt)
                    probe = "$ErrorActionPreference='Stop'; $raw=& docker.exe -H npipe:////./pipe/docker_engine info --format '{{json .}}'; if($LASTEXITCODE -ne 0){exit 1}; $info=$raw|ConvertFrom-Json; if($info.OSType -ne 'windows'){throw 'Expected native Windows engine'}; @{user=[Environment]::UserName;os=$info.OSType;version=$info.ServerVersion}|ConvertTo-Json -Compress"
                    encoded = base64.b64encode(probe.encode("utf-16le")).decode("ascii")
                    try:
                        result = self.command(self.ssh("powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded), check=False, timeout=min(30, max(1, deadline - self.clock())))
                        check = json.loads(result.stdout.strip().lstrip("\ufeff")) if result.returncode == 0 else {}
                        if check.get("os") == "windows" and str(check.get("user", "")).lower() == "winhoohost" and check.get("version") == receipt.get("dockerVersion"):
                            return {**self.summary(state), "ready": True, "dockerVersion": check["version"], "nativeContainerTested": False}
                        last_error = (result.stderr or result.stdout or "SSH engine attestation failed.").strip()[-2000:]
                    except (HostError, ValueError) as exc:
                        last_error = str(exc)
            remaining = deadline - self.clock()
            if remaining > 0:
                self.sleep(min(5, remaining))
        self.stop(state)
        raise HostError(f"Windows host readiness timed out after {timeout:g}s. {last_error} The owned VM was stopped; its disk and credentials remain available for up/wait.")

    def summary(self, state: dict[str, Any]) -> dict[str, Any]:
        config = str(self.directory / "ssh_config")
        return {"linuxContext": state["context"], "containerId": state["containerId"], "volume": state["volume"], "viewer": f"http://127.0.0.1:{VIEWER_PORT}", "sshCommand": shlex.join(self.ssh()), "sshConfig": config, "windowsDockerContext": "winhoo-local-host", "windowsDockerEndpoint": "ssh://winhoo-local-host", "createWindowsContextCommand": shlex.join(["docker", "context", "create", "winhoo-local-host", "--docker", "host=ssh://winhoo-local-host"]), "controllerDoctorCommand": shlex.join(["python3", str(ROOT/'winhoo.py'), "--host-ssh-config", config, "--context", "winhoo-local-host", "doctor"]), "freeGiB": round(self.free_bytes()/GIB, 2)}

    def status(self) -> dict[str, Any]:
        state = self.load()
        record = self.inspect(state)
        if record["State"].get("Running"):
            self.guard_disk(state)
        receipt = self.receipt(state)
        return {**self.summary(state), "state": record["State"].get("Status"), "bootstrapStatus": receipt.get("status") if receipt else None, "bootstrapPhase": receipt.get("phase") if receipt else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", help="Explicit local Linux ARM64 Docker context; saved at first creation.")
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".local" / "local-host")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "up", "status", "stop"):
        commands.add_parser(name)
    wait = commands.add_parser("wait")
    wait.add_argument("--timeout", type=float, default=7200)
    args = parser.parse_args(argv)
    host = LocalHost(args.state_dir, args.context)
    try:
        if args.command == "doctor":
            result = host.doctor()
        elif args.command == "up":
            result = host.up()
        elif args.command == "wait":
            result = host.wait(args.timeout)
        elif args.command == "status":
            result = host.status()
        else:
            host.stop()
            result = {"stopped": True, "diskRetained": True}
        print(json.dumps(result, indent=2))
        return 0
    except (HostError, OSError) as exc:
        print(f"winhoo local host: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        try:
            host.stop()
        except (HostError, OSError) as exc:
            print(f"winhoo local host: could not stop safely after interruption: {exc}", file=sys.stderr)
        print("winhoo local host: interrupted; VM disk retained", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
