#!/usr/bin/env python3
"""Create and operate disposable native Windows containers through Docker and SSH."""

from __future__ import annotations

import argparse
import base64
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable, Sequence
from urllib.parse import unquote, urlsplit
import uuid


ROOT = Path(__file__).resolve().parent
SCHEMA = 1
LABEL_OWNER = "io.openhoo.winhoo.managed"
LABEL_INSTANCE = "io.openhoo.winhoo.instance"
LABEL_SCHEMA = "io.openhoo.winhoo.schema"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
DEFAULT_IMAGE = "winhoo:0.1.0"
GUEST_USER = "winhoo"
PROBE = "[Console]::Out.WriteLine((@{winhoo='ready';user=[Environment]::UserName;platform=[Environment]::OSVersion.Platform.ToString()} | ConvertTo-Json -Compress))"
STDIN_BOOTSTRAP = "$utf8=New-Object System.Text.UTF8Encoding $false; [Console]::InputEncoding=$utf8; [Console]::OutputEncoding=$utf8; $OutputEncoding=$utf8; & ([ScriptBlock]::Create([Console]::In.ReadToEnd()))"


class WinhooError(RuntimeError):
    """An actionable controller or runtime failure."""


def encoded_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def valid_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise WinhooError("Instance names must start with a lowercase letter and contain only lowercase letters, digits and hyphens (maximum 48 characters).")
    return name


def jump_host(endpoint: str, *, local_windows: bool | None = None) -> str | None:
    """Accept only a verified SSH route or a local Windows named pipe."""
    local_windows = os.name == "nt" if local_windows is None else local_windows
    parsed = urlsplit(endpoint)
    if parsed.scheme == "npipe" and local_windows:
        if parsed.netloc or not re.fullmatch(r"/{1,2}\./pipe/[A-Za-z0-9_.-]+", parsed.path):
            raise WinhooError("Only a local Windows named pipe is supported; remote named pipes cannot route guest SSH safely.")
        return None
    if parsed.scheme != "ssh":
        raise WinhooError("Native Windows containers need a local Windows named-pipe context or a remote ssh:// Docker context. TCP and local Unix-socket contexts are not supported.")
    if parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise WinhooError("The Docker SSH endpoint must contain only a host, optional user and optional port.")
    hostname = parsed.hostname
    username = unquote(parsed.username or "")
    if not hostname or not re.fullmatch(r"[A-Za-z0-9_.:%-]+", hostname):
        raise WinhooError("Unsupported Docker SSH hostname.")
    if username and not re.fullmatch(r"[A-Za-z0-9_.\\-]+", username):
        raise WinhooError("Unsupported Docker SSH username.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise WinhooError("Invalid Docker SSH port.") from exc
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"{username + '@' if username else ''}{hostname}{':' + str(port) if port else ''}"


def host_key_from_tar(data: bytes) -> str:
    """Read the one public key without extracting paths from the archive."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            entries = [entry for entry in archive.getmembers() if entry.isfile()]
            if len(entries) != 1 or entries[0].size > 16384:
                raise WinhooError("Docker returned an unexpected SSH host-key archive.")
            stream = archive.extractfile(entries[0])
            if stream is None:
                raise WinhooError("The SSH host public key is missing.")
            fields = stream.read().decode("ascii").strip().split()
        if len(fields) < 2 or fields[0] != "ssh-ed25519":
            raise WinhooError("Expected an ED25519 guest host public key.")
        raw = base64.b64decode(fields[1], validate=True)
        if len(raw) != 51 or not raw.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"):
            raise WinhooError("Invalid ED25519 guest host public key.")
        return " ".join(fields[:2])
    except (tarfile.TarError, UnicodeError, ValueError) as exc:
        raise WinhooError(f"Could not read guest host public key: {exc}") from exc


class Controller:
    def __init__(self, context: str | None = None, state_dir: Path | str = ROOT / ".local", *, host_ssh_config: Path | str | None = None, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.context = context
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.runner = runner
        self.clock = clock
        self.sleep = sleep
        self.host_ssh_config = Path(host_ssh_config).expanduser().resolve() if host_ssh_config else None

    def command(self, argv: Sequence[str], *, input: str | bytes | None = None, check: bool = True, capture: bool = True, binary: bool = False, timeout: float | None = 120, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        try:
            options = {"input": input, "stdin": None if input is not None else subprocess.DEVNULL, "capture_output": capture, "text": not binary, "timeout": timeout}
            if env is not None:
                options["env"] = env
            if not binary:
                options.update(encoding="utf-8", errors="replace")
            result = self.runner(list(argv), **options)
        except FileNotFoundError as exc:
            raise WinhooError(f"Required executable is unavailable: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise WinhooError(f"Command timed out: {argv[0]} {argv[1] if len(argv) > 1 else ''}") from exc
        if check and result.returncode:
            detail = result.stderr or result.stdout or ""
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            raise WinhooError(f"{argv[0]} failed ({result.returncode}): {detail.strip()[-6000:]}")
        return result

    def docker(self, context: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        command = ["docker", "--context", context, *args]
        if self.host_ssh_config is None:
            return self.command(command, **kwargs)
        self.bind_host_ssh_config()
        real_ssh = shutil.which("ssh")
        if real_ssh is None:
            raise WinhooError("Required executable is unavailable: ssh")
        # Docker's SSH connection helper invokes `ssh` through PATH. Scope this
        # adapter to its subprocess so neither global SSH config nor PATH changes.
        with tempfile.TemporaryDirectory(prefix="winhoo-docker-ssh-") as directory:
            wrapper = Path(directory) / "ssh"
            wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(real_ssh)} -F {shlex.quote(str(self.host_ssh_config))} \"$@\"\n", encoding="utf-8")
            wrapper.chmod(0o700)
            environment = dict(os.environ)
            environment["PATH"] = directory + os.pathsep + environment.get("PATH", os.defpath)
            return self.command(command, env=environment, **kwargs)

    def bind_host_ssh_config(self, state: dict[str, Any] | None = None) -> None:
        if state is not None:
            saved = state.get("host_ssh_config")
            if self.host_ssh_config is not None and str(self.host_ssh_config) != saved:
                raise WinhooError("This instance belongs to a different host SSH configuration; refusing to change its transport.")
            self.host_ssh_config = Path(saved) if saved else None
        if self.host_ssh_config is not None:
            if os.name == "nt":
                raise WinhooError("--host-ssh-config is supported on macOS and Linux controllers. On Windows use its ordinary SSH config or a local named-pipe context.")
            if not self.host_ssh_config.is_absolute() or not self.host_ssh_config.is_file() or any(character in str(self.host_ssh_config) for character in ('\"', "\n", "\r", "\x00")):
                raise WinhooError("The private host SSH configuration must be an existing absolute path without quotes or newlines.")

    def resolve_context(self, state: dict[str, Any] | None = None) -> tuple[str, str]:
        self.bind_host_ssh_config(state)
        saved = state.get("context") if state else None
        if saved and self.context and self.context != saved:
            raise WinhooError(f"This instance belongs to Docker context {saved!r}; refusing to operate through {self.context!r}.")
        context = self.context or saved
        if not context:
            context = self.command(["docker", "context", "show"]).stdout.strip()
        try:
            records = json.loads(self.command(["docker", "context", "inspect", context]).stdout)
            endpoint = records[0]["Endpoints"]["docker"]["Host"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise WinhooError("Docker returned an invalid context description.") from exc
        if state and endpoint != state.get("endpoint"):
            raise WinhooError("The saved Docker context endpoint changed. Verify the host before recreating this instance.")
        return context, endpoint

    def doctor(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        context, endpoint = self.resolve_context(state)
        try:
            info = json.loads(self.docker(context, "info", "--format", "{{json .}}").stdout)
        except (ValueError, TypeError) as exc:
            raise WinhooError("Docker did not return valid engine information.") from exc
        if info.get("OSType") != "windows":
            raise WinhooError(f"Docker context {context!r} uses a {info.get('OSType', 'unknown')} engine. Select a Windows engine; native Windows containers cannot run on this engine.")
        if info.get("Architecture") not in ("x86_64", "amd64", "x64"):
            raise WinhooError("The pinned Windows base requires an x64 Windows container engine.")
        jump = jump_host(endpoint)
        for executable in ("ssh", "ssh-keygen"):
            if shutil.which(executable) is None:
                raise WinhooError(f"Required executable is unavailable: {executable}")
        return {"context": context, "endpoint": endpoint, "os": info.get("OperatingSystem"), "kernel": info.get("KernelVersion"), "architecture": info.get("Architecture"), "jump_host": jump, "desktop": False}

    def instance_dir(self, name: str) -> Path:
        return self.state_dir / valid_name(name)

    def load(self, name: str) -> dict[str, Any]:
        directory = self.instance_dir(name)
        try:
            state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WinhooError(f"No valid state for instance {name!r} under {directory}.") from exc
        if state.get("schema") != SCHEMA or state.get("name") != name:
            raise WinhooError("Instance state has an unsupported schema or mismatched name.")
        try:
            uuid.UUID(state["uuid"])
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise WinhooError("Instance state has an invalid ownership identity.") from exc
        self.bind_host_ssh_config(state)
        return state

    def save(self, state: dict[str, Any]) -> None:
        directory = self.instance_dir(state["name"])
        temporary = directory / "state.json.tmp"
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(directory / "state.json")

    @staticmethod
    def labels(state: dict[str, Any]) -> dict[str, str]:
        return {LABEL_OWNER: "true", LABEL_INSTANCE: state["uuid"], LABEL_SCHEMA: str(SCHEMA)}

    def assert_labels(self, state: dict[str, Any], labels: dict[str, str] | None) -> None:
        if not labels or any(labels.get(key) != value for key, value in self.labels(state).items()):
            raise WinhooError("Resource ownership does not match this winhoo instance; refusing to operate on it.")

    def inspect(self, state: dict[str, Any], *, missing_ok: bool = False) -> dict[str, Any] | None:
        context, _ = self.resolve_context(state)
        container_id = state.get("container_id")
        if not container_id:
            if missing_ok:
                return None
            raise WinhooError("This instance has no container. Start it with up.")
        result = self.docker(context, "container", "inspect", container_id, check=False)
        if result.returncode:
            error = (result.stderr or "").lower()
            if missing_ok and ("no such" in error or "not found" in error):
                return None
            raise WinhooError(f"Cannot inspect the saved container ID: {(result.stderr or result.stdout).strip()}")
        try:
            record = json.loads(result.stdout)[0]
            if record["Id"] != container_id:
                raise WinhooError("Docker returned a different container ID than the saved identity.")
            self.assert_labels(state, record["Config"].get("Labels"))
            return record
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise WinhooError("Docker returned invalid container metadata.") from exc

    def assert_volume(self, state: dict[str, Any]) -> None:
        context, _ = self.resolve_context(state)
        try:
            record = json.loads(self.docker(context, "volume", "inspect", state["volume"]).stdout)[0]
            self.assert_labels(state, record.get("Labels"))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise WinhooError("Docker returned invalid host-key volume metadata.") from exc

    def build(self, image: str = DEFAULT_IMAGE, *, isolation: str = "hyperv", base_image: str | None = None, source_dir: Path | None = None) -> int:
        source = Path(source_dir).expanduser().resolve() if source_dir else ROOT
        if not (source / "Dockerfile").is_file() or not (source / "container").is_dir():
            raise WinhooError("The Windows image source is missing. Run from the winhoo checkout or supply build --source-dir PATH to that checkout.")
        info = self.doctor()
        args = ["build", "--isolation", isolation, "--tag", image]
        if base_image:
            args.extend(["--build-arg", f"WINDOWS_BASE_IMAGE={base_image}"])
        args.append(str(source))
        return self.docker(info["context"], *args, capture=False, check=False, timeout=None).returncode

    def prepare_identity(self, state: dict[str, Any]) -> None:
        directory = self.instance_dir(state["name"])
        private = directory / "id_ed25519"
        self.command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"winhoo-{state['uuid']}", "-f", str(private)])
        private.chmod(0o600)
        (directory / "id_ed25519.pub").chmod(0o600)
        user_config = (Path.home() / ".ssh" / "config").as_posix()
        if any(character in user_config for character in ('"', "\n", "\r")):
            raise WinhooError("The SSH configuration path contains unsupported characters.")
        config = "Host *\n    StrictHostKeyChecking yes\n    BatchMode yes\n"
        if self.host_ssh_config is not None:
            config += f'Include "{self.host_ssh_config.as_posix()}"\nHost *\n'
        if Path(user_config).is_file():
            config += f'Include "{user_config}"\n'
        (directory / "ssh_config").write_text(config, encoding="utf-8")
        (directory / "ssh_config").chmod(0o600)

    def up(self, name: str, *, image: str | None = None, isolation: str | None = None, timeout: float = 300) -> dict[str, Any]:
        valid_name(name)
        if not math.isfinite(timeout) or timeout <= 0:
            raise WinhooError("Readiness timeout must be a finite positive number.")
        directory = self.instance_dir(name)
        created_state = not directory.exists()
        if directory.exists():
            state = self.load(name)
            if image is not None and image != state["image"]:
                raise WinhooError("This instance already uses a different image. Create a new named instance, or deliberately purge this one before changing its image.")
            if isolation is not None and isolation != state["isolation"]:
                raise WinhooError("This instance already uses a different isolation mode. Create a new named instance to change it.")
            self.doctor(state)
            if state.get("container_id"):
                record = self.inspect(state)
                if not record["State"].get("Running"):
                    self.docker(state["context"], "start", state["container_id"])
                return self.wait_ready(state, timeout=timeout)
            self.assert_volume(state)
        else:
            info = self.doctor()
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise WinhooError("Another controller is creating this instance.") from exc
            identity = str(uuid.uuid4())
            state = {"schema": SCHEMA, "name": name, "uuid": identity, "context": info["context"], "endpoint": info["endpoint"], "host_ssh_config": str(self.host_ssh_config) if self.host_ssh_config else None, "image": image or DEFAULT_IMAGE, "isolation": isolation or "hyperv", "volume": f"winhoo-{name}-{identity[:8]}-ssh", "container_id": None, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self.save(state)
            try:
                self.prepare_identity(state)
                args = ["volume", "create"]
                for key, value in self.labels(state).items():
                    args.extend(["--label", f"{key}={value}"])
                args.append(state["volume"])
                self.docker(state["context"], *args)
                self.assert_volume(state)
            except BaseException:
                self.rollback(state)
                raise
        try:
            args = ["create", "--name", f"winhoo-{name}", "--isolation", state["isolation"], "--network", "nat", "--mount", f"type=volume,source={state['volume']},target=C:/ProgramData/ssh"]
            for key, value in self.labels(state).items():
                args.extend(["--label", f"{key}={value}"])
            args.append(state["image"])
            state["container_id"] = self.docker(state["context"], *args).stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{64}", state["container_id"]):
                raise WinhooError("Docker did not return a full container ID.")
            self.save(state)
            self.inspect(state)
            self.docker(state["context"], "cp", str(directory / "id_ed25519.pub"), f"{state['container_id']}:C:/winhoo/access/authorized_keys")
            self.docker(state["context"], "start", state["container_id"])
            return self.wait_ready(state, timeout=timeout)
        except BaseException:
            self.rollback(state, purge=created_state)
            raise

    def rollback(self, state: dict[str, Any], *, purge: bool = True) -> None:
        try:
            self.down(state["name"], purge=purge)
        except Exception as exc:
            print(f"winhoo: rollback could not safely finish: {exc}. State retained at {self.instance_dir(state['name'])}.", file=sys.stderr)

    def guest_ip(self, state: dict[str, Any], record: dict[str, Any] | None = None) -> str:
        record = record or self.inspect(state)
        try:
            network = record["NetworkSettings"]["Networks"]["nat"]
            address = ipaddress.IPv4Address(network["IPAddress"])
            if not address.is_private or address.is_loopback or address.is_unspecified:
                raise WinhooError("The container NAT address is not a private IPv4 address.")
            return str(address)
        except (ValueError, KeyError, TypeError) as exc:
            raise WinhooError("The container has no usable private IPv4 address on its NAT network.") from exc

    def pin_host_key(self, state: dict[str, Any]) -> None:
        self.inspect(state)
        result = self.docker(state["context"], "cp", f"{state['container_id']}:C:/ProgramData/ssh/ssh_host_ed25519_key.pub", "-", binary=True, timeout=20)
        key = host_key_from_tar(result.stdout)
        path = self.instance_dir(state["name"]) / "known_hosts"
        expected = f"winhoo-{state['uuid']} {key}\n"
        if path.exists():
            if path.read_text(encoding="ascii") != expected:
                raise WinhooError("The guest SSH host key changed. Refusing to replace its pin; verify and deliberately recreate the instance.")
        else:
            with path.open("x", encoding="ascii") as stream:
                stream.write(expected)
            path.chmod(0o600)

    def ssh_command(self, state: dict[str, Any], *, record: dict[str, Any] | None = None, interactive: bool = False) -> list[str]:
        self.resolve_context(state)
        address = self.guest_ip(state, record)
        directory = self.instance_dir(state["name"])
        if not (directory / "known_hosts").is_file():
            raise WinhooError("No authenticated guest host-key pin exists; run up to complete readiness.")
        args = ["ssh", "-F", str(directory / "ssh_config"), "-i", str(directory / "id_ed25519"), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={directory / 'known_hosts'}", "-o", f"HostKeyAlias=winhoo-{state['uuid']}", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
        if not interactive:
            args.extend(["-T", "-o", "BatchMode=yes"])
        jump = jump_host(state["endpoint"])
        if jump:
            args.extend(["-J", jump])
        args.append(f"{GUEST_USER}@{address}")
        return args

    def wait_ready(self, state: dict[str, Any], *, timeout: float = 300) -> dict[str, Any]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise WinhooError("Readiness timeout must be a finite positive number.")
        deadline = self.clock() + timeout
        last_error = "Guest SSH has not responded."
        while self.clock() < deadline:
            record = self.inspect(state)
            if not record["State"].get("Running"):
                logs = self.docker(state["context"], "logs", "--tail", "80", state["container_id"], check=False)
                raise WinhooError(f"Container exited before SSH readiness (exit {record['State'].get('ExitCode')}).\n{logs.stdout or ''}{logs.stderr or ''}")
            try:
                self.pin_host_key(state)
                command = self.ssh_command(state, record=record) + ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_powershell(PROBE)]
                result = self.command(command, check=False, timeout=min(30, max(1, deadline - self.clock())))
                if result.returncode == 0:
                    probe = json.loads(result.stdout.strip().lstrip("\ufeff"))
                    if probe.get("winhoo") == "ready" and probe.get("user", "").lower() == GUEST_USER and probe.get("platform") == "Win32NT":
                        state["ready"] = True
                        self.save(state)
                        return {"name": state["name"], "container_id": state["container_id"], "image": state["image"], "ip": self.guest_ip(state, record), "ssh_ready": True, "context": state["context"]}
                    last_error = "SSH returned an unexpected readiness identity."
                else:
                    last_error = (result.stderr or result.stdout or "SSH probe failed.").strip()[-2500:]
            except WinhooError as exc:
                if "host key changed" in str(exc):
                    raise
                last_error = str(exc)
            except (ValueError, TypeError) as exc:
                last_error = f"SSH did not return the readiness JSON: {exc}"
            remaining = deadline - self.clock()
            if remaining > 0:
                self.sleep(min(2, remaining))
        logs = self.docker(state["context"], "logs", "--tail", "80", state["container_id"], check=False)
        raise WinhooError(f"SSH readiness timed out after {timeout:g}s. {last_error}\n{logs.stdout or ''}{logs.stderr or ''}")

    def execute(self, name: str, script: str, *, capture: bool = False, timeout: float | None = None) -> subprocess.CompletedProcess:
        state = self.load(name)
        record = self.inspect(state)
        if not record["State"].get("Running"):
            raise WinhooError("The instance is stopped; start it with up.")
        wrapped = "$global:LASTEXITCODE=0; $ErrorActionPreference='Stop'; try { Set-Location -LiteralPath 'C:/work'; & {\n" + script + "\n}; $winhooOK=$?; if($global:LASTEXITCODE -ne 0){exit $global:LASTEXITCODE}; if(-not $winhooOK){exit 1}; exit 0 } catch { [Console]::Error.WriteLine(($_ | Out-String)); exit 1 }"
        command = self.ssh_command(state, record=record) + ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_powershell(STDIN_BOOTSTRAP)]
        return self.command(command, input=wrapped, capture=capture, check=False, timeout=timeout)

    def script(self, name: str, path: Path, *, capture: bool = False) -> subprocess.CompletedProcess:
        """Stage a real script file so $PSScriptRoot identifies C:\\work."""
        path = path.expanduser().resolve()
        try:
            source_text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise WinhooError(f"PowerShell scripts must use UTF-8, with or without a BOM: {path}") from exc
        guest_path = f"C:/work/.winhoo-script-{uuid.uuid4().hex}.ps1"
        # Windows PowerShell 5.1 otherwise reads UTF-8 without a BOM as ANSI.
        self.load(name)
        with tempfile.TemporaryDirectory(prefix=".script-", dir=self.instance_dir(name)) as directory:
            staged = Path(directory) / "script.ps1"
            staged.write_bytes(b"\xef\xbb\xbf" + source_text.encode("utf-8"))
            staged.chmod(0o600)
            self.copy(name, str(staged), guest_path)
        source = f"try {{ & '{guest_path}' }} finally {{ Remove-Item -LiteralPath '{guest_path}' -Force -ErrorAction SilentlyContinue }}"
        return self.execute(name, source, capture=capture)

    def copy(self, name: str, source: str, destination: str, *, download: bool = False) -> None:
        state = self.load(name)
        self.inspect(state)
        remote = source if download else destination
        if not re.match(r"^[A-Za-z]:[/\\]", remote):
            raise WinhooError("The guest path must be an absolute Windows path, such as C:/work/file.txt.")
        container_path = f"{state['container_id']}:{remote}"
        if download:
            local = str(Path(destination).expanduser().resolve())
            self.docker(state["context"], "cp", container_path, local, capture=False)
        else:
            local = str(Path(source).expanduser().resolve())
            if not Path(local).exists():
                raise WinhooError(f"Local upload source does not exist: {local}")
            self.docker(state["context"], "cp", local, container_path, capture=False)

    def status(self, name: str) -> dict[str, Any]:
        state = self.load(name)
        record = self.inspect(state, missing_ok=True)
        result = {"name": name, "context": state["context"], "container_id": state.get("container_id"), "image": state["image"], "state": "removed" if record is None else record["State"].get("Status"), "ssh_ready": False}
        if record and record["State"].get("Running"):
            result["ip"] = self.guest_ip(state, record)
            # Status is a live check, never the persisted readiness flag.
            try:
                probe = self.execute(name, PROBE, capture=True, timeout=30)
                data = json.loads(probe.stdout.strip().lstrip("\ufeff")) if probe.returncode == 0 else {}
                result["ssh_ready"] = data.get("winhoo") == "ready" and data.get("platform") == "Win32NT" and data.get("user", "").lower() == GUEST_USER
            except (WinhooError, ValueError):
                pass
        return result

    def logs(self, name: str, *, follow: bool = False) -> int:
        state = self.load(name)
        self.inspect(state)
        args = ["logs", "--tail", "100"]
        if follow:
            args.append("--follow")
        args.append(state["container_id"])
        return self.docker(state["context"], *args, capture=False, check=False, timeout=None).returncode

    def down(self, name: str, *, purge: bool = False) -> None:
        state = self.load(name)
        record = self.inspect(state, missing_ok=True)
        if record:
            self.docker(state["context"], "rm", "--force", state["container_id"])
        state["container_id"] = None
        state["ready"] = False
        self.save(state)
        if purge:
            result = self.docker(state["context"], "volume", "inspect", state["volume"], check=False)
            error = (result.stderr or "").lower()
            if result.returncode == 0:
                try:
                    volume = json.loads(result.stdout)[0]
                    self.assert_labels(state, volume.get("Labels"))
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise WinhooError("Cannot verify the host-key volume for deletion.") from exc
                self.docker(state["context"], "volume", "rm", state["volume"])
            elif "no such" not in error and "not found" not in error:
                raise WinhooError(f"Cannot verify host-key volume: {result.stderr or result.stdout}")
            shutil.rmtree(self.instance_dir(name))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--context", help="Docker context using a Windows engine (ssh:// remotely).")
    root.add_argument("--host-ssh-config", type=Path, help="Private host SSH config for Docker and the guest jump host (macOS/Linux); saved per instance.")
    root.add_argument("--state-dir", type=Path, default=ROOT / ".local", help="Private per-instance state directory.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check the selected native Windows container engine.")
    build = commands.add_parser("build", help="Build the reusable base image.")
    build.add_argument("--image", default=DEFAULT_IMAGE)
    build.add_argument("--base-image")
    build.add_argument("--source-dir", type=Path, help="Image source checkout (required after a noneditable package install).")
    build.add_argument("--isolation", choices=("hyperv", "process"), default="hyperv")
    up = commands.add_parser("up", help="Create/start an instance and wait for actual SSH readiness.")
    up.add_argument("name", nargs="?", default="dev")
    up.add_argument("--image", help=f"Image for a new instance (default: {DEFAULT_IMAGE}); existing instances retain their image.")
    up.add_argument("--isolation", choices=("hyperv", "process"), help="Isolation for a new instance (default: hyperv).")
    up.add_argument("--timeout", type=float, default=300)
    execute = commands.add_parser("exec", help="Run a PowerShell script, preserving its exit status.")
    execute.add_argument("name")
    execute.add_argument("script", help="PowerShell source; use -- before source beginning with a dash.")
    script = commands.add_parser("script", help="Stage and execute a local PowerShell file in C:/work; remove it afterwards.")
    script.add_argument("name")
    script.add_argument("path", type=Path)
    for operation in ("put", "get"):
        command = commands.add_parser(operation, help="Copy files through the selected Docker context.")
        command.add_argument("name")
        command.add_argument("source")
        command.add_argument("destination")
    ssh = commands.add_parser("ssh", help="Connect directly to guest SSH through its host, if remote.")
    ssh.add_argument("name")
    ssh.add_argument("--print-command", action="store_true")
    status = commands.add_parser("status", help="Inspect container identity and live SSH readiness.")
    status.add_argument("name", nargs="?", default="dev")
    logs = commands.add_parser("logs", help="Read guest bootstrap and SSH service logs.")
    logs.add_argument("name", nargs="?", default="dev")
    logs.add_argument("--follow", action="store_true")
    down = commands.add_parser("down", help="Remove the owned container; preserve host keys unless --purge.")
    down.add_argument("name", nargs="?", default="dev")
    down.add_argument("--purge", action="store_true", help="Also delete the owned SSH host-key volume and local identity.")
    run = commands.add_parser("run", help="Run a PowerShell script in a fresh disposable instance.")
    run.add_argument("--image", default=DEFAULT_IMAGE)
    run.add_argument("--isolation", choices=("hyperv", "process"), default="hyperv")
    run.add_argument("--timeout", type=float, default=300)
    run.add_argument("--file", type=Path, help="Read PowerShell source from a file.")
    run.add_argument("script", nargs="?", help="PowerShell source (or use --file).")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    controller = Controller(arguments.context, arguments.state_dir, host_ssh_config=arguments.host_ssh_config)
    try:
        if arguments.command == "doctor":
            print(json.dumps(controller.doctor(), indent=2))
        elif arguments.command == "build":
            return controller.build(arguments.image, isolation=arguments.isolation, base_image=arguments.base_image, source_dir=arguments.source_dir)
        elif arguments.command == "up":
            print(json.dumps(controller.up(arguments.name, image=arguments.image, isolation=arguments.isolation, timeout=arguments.timeout), indent=2))
        elif arguments.command == "exec":
            return controller.execute(arguments.name, arguments.script).returncode
        elif arguments.command == "script":
            return controller.script(arguments.name, arguments.path).returncode
        elif arguments.command in ("put", "get"):
            controller.copy(arguments.name, arguments.source, arguments.destination, download=arguments.command == "get")
        elif arguments.command == "ssh":
            state = controller.load(arguments.name)
            command = controller.ssh_command(state, interactive=True)
            if arguments.print_command:
                if os.name == "nt":
                    print(subprocess.list2cmdline(command))
                else:
                    print(shlex.join(command))
            else:
                # Interactive SSH needs the caller's terminal stdin.
                return subprocess.call(command)
        elif arguments.command == "status":
            print(json.dumps(controller.status(arguments.name), indent=2))
        elif arguments.command == "logs":
            return controller.logs(arguments.name, follow=arguments.follow)
        elif arguments.command == "down":
            controller.down(arguments.name, purge=arguments.purge)
        elif arguments.command == "run":
            if (arguments.file is None) == (arguments.script is None):
                raise WinhooError("Supply exactly one script argument or --file.")
            if arguments.file and not arguments.file.is_file():
                raise WinhooError(f"Local PowerShell script does not exist: {arguments.file}")
            name = f"run-{uuid.uuid4().hex[:12]}"
            outcome = None
            try:
                controller.up(name, image=arguments.image, isolation=arguments.isolation, timeout=arguments.timeout)
                outcome = controller.script(name, arguments.file).returncode if arguments.file else controller.execute(name, arguments.script).returncode
            finally:
                if (controller.instance_dir(name) / "state.json").is_file():
                    try:
                        controller.down(name, purge=True)
                    except Exception as exc:
                        print(f"winhoo: disposable-instance cleanup failed: {exc}", file=sys.stderr)
                        if outcome == 0:
                            outcome = 1
            return outcome
        return 0
    except WinhooError as exc:
        print(f"winhoo: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"winhoo: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("winhoo: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
