"""Windows desktop VM backend for GUI and assistive-technology workloads.

The guest is a full Windows VM inside a Linux container. Application owners
provide OEM provisioning and authenticated application readiness checks.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
VERSION = "0.4.0"
BASE = "docker.io/dockurr/windows@sha256:0cff9eb0e7aee9953e55bc682852ca4fdca233145a58ae1ec94f0b0c01a2ed30"
OWNER = "io.openhoo.winhoo.desktop"
OPERATIONS = ("build", "up", "restart", "stop", "down", "status", "logs", "config", "tools-prepare", "tools-repair", "guest-status", "guest-exec", "guest-process", "guest-read", "guest-time-sync", "guest-shutdown")


class DesktopError(RuntimeError):
    pass


class Desktop:
    def __init__(self, configuration: Path, *, runner: Callable = subprocess.run):
        self.path = configuration.resolve()
        self.runner = runner
        try:
            self.config = json.loads(self.path.read_text(encoding="utf-8"))
            value = self.config
            if value["schema"] != "winhoo.desktop/1":
                raise ValueError("schema")
            for key in ("name", "project", "volume"):
                if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value[key]):
                    raise ValueError(key)
            if value["engine"] not in ("docker", "podman"):
                raise ValueError("engine")
            if value["architecture"] not in ("amd64", "arm64"):
                raise ValueError("architecture")
            if value["acceleration"] not in ("kvm", "tcg") or (value["acceleration"] == "kvm" and value["architecture"] != "amd64"):
                raise ValueError("acceleration")
            for key in ("shared", "oem", "storage", "environmentFile"):
                if not Path(value[key]).is_absolute() or not Path(value[key]).exists():
                    raise ValueError(key)
                if any(character in value[key] for character in ("\n", "\r", "$")):
                    raise ValueError(key)
            if not isinstance(value["legacyBind"], bool) or not isinstance(value["adoptLegacy"], bool):
                raise ValueError("storage flags")
            seen = set()
            for port in value["ports"]:
                if set(port) != {"host", "guest", "protocol"} or port["protocol"] not in ("tcp", "udp"):
                    raise ValueError("port")
                for key in ("host", "guest"):
                    if type(port[key]) is not int or not 1 <= port[key] <= 65535:
                        raise ValueError("port")
                identity = (port["host"], port["protocol"])
                if identity in seen:
                    raise ValueError("duplicate host port")
                seen.add(identity)
            if not isinstance(value["environment"], dict) or any(
                not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not isinstance(item, str)
                or any(char in item for char in ("\n", "\r", "$"))
                for key, item in value["environment"].items()
            ):
                raise ValueError("environment")
            if set(value["environment"]) & {"PASSWORD", "KVM", "NETWORK", "PLATFORM", "DISK_TYPE", "DISK_FMT", "ALLOCATE", "QEMU_TIMEOUT", "SHUTDOWN", "QMP", "ARGUMENTS"}:
                raise ValueError("reserved environment")
            if value.get("ownerGeneration") is not None and not re.fullmatch(r"[a-f0-9]{32}", value["ownerGeneration"]):
                raise ValueError("ownerGeneration")
            if "image" in value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,254}", value["image"]):
                raise ValueError("image")
            if value.get("diskBackup", "reflink") not in ("reflink", "sparse"):
                raise ValueError("diskBackup")
            if "buildImage" in value and type(value["buildImage"]) is not bool:
                raise ValueError("buildImage")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DesktopError(f"Invalid WinHoo desktop configuration: {self.path} ({exc})") from exc
        self.owner = hashlib.sha256(str(self.path).encode()).hexdigest()

    def command(self, *arguments: str, check=True, capture=True, timeout=180, input_data=None):
        io = {"stdin": subprocess.DEVNULL} if input_data is None else {"input": input_data}
        result = self.runner([self.config["engine"], *arguments], **io,
                             capture_output=capture, text=True, timeout=timeout)
        if check and result.returncode:
            # Compose output can contain expanded environment values. Keep
            # diagnostics in the caller's terminal, never stringify its config.
            raise DesktopError(f"WinHoo desktop engine command failed (exit {result.returncode}).")
        return result

    def assert_storage(self):
        result = self.command("container", "inspect", self.config["name"], check=False)
        if result.returncode:
            if "No such" in (result.stderr or "") or "no container" in (result.stderr or "").lower():
                return
            raise DesktopError("Cannot inspect the existing desktop container; refusing to change it.")
        try:
            record = json.loads(result.stdout)[0]
            mounts = {mount["Destination"]: mount for mount in record["Mounts"]}
            storage, shared = mounts["/storage"], mounts["/shared"]
            valid = (storage.get("Type") == "bind" and storage.get("Source") == self.config["storage"]
                     if self.config["legacyBind"] else storage.get("Type") == "volume" and storage.get("Name") == self.config["volume"])
            valid = valid and shared.get("Type") == "bind" and shared.get("Source") == self.config["shared"]
            label = (record["Config"].get("Labels") or {}).get(OWNER)
            valid = valid and (label == self.owner or (label is None and self.config["adoptLegacy"]))
            if not valid:
                raise DesktopError("Existing Windows desktop belongs to another owner or storage root.")
            return record
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise DesktopError("Invalid desktop container metadata; refusing to change it.") from exc

    def assert_engine(self):
        try:
            info = json.loads(self.command("info", "--format", "{{json .}}").stdout)
            host = info.get("host", {})
            operating_system = info.get("OSType", host.get("os"))
            architecture = info.get("Architecture", host.get("arch"))
            expected = {"arm64": {"arm64", "aarch64"}, "amd64": {"amd64", "x86_64", "x64"}}[self.config["architecture"]]
            if operating_system != "linux" or architecture not in expected:
                raise DesktopError("The desktop backend requires a Linux engine matching its native host architecture.")
            identity = info.get("ID") or (str(host.get("hostname", "")) + ":" + str(info.get("store", {}).get("graphRoot", "")))
            if not identity or identity == ":":
                raise DesktopError("The desktop engine did not provide a stable identity.")
            binding = {"engine": self.config["engine"], "identity": identity}
            path = self.path.with_suffix(".engine.json")
            if path.exists():
                if json.loads(path.read_text(encoding="utf-8")) != binding:
                    raise DesktopError("The desktop engine identity changed; verify its context before using another runtime state.")
            else:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(binding, stream)
                    stream.write("\n")
        except (ValueError, KeyError, TypeError) as exc:
            raise DesktopError("Invalid Linux engine metadata.") from exc

    def compose_configuration(self) -> dict[str, Any]:
        value = self.config
        environment = {**value["environment"], "PASSWORD": "${WINDOWS_PASSWORD:?WINDOWS_PASSWORD must be set}",
                       "PLATFORM": "x64", "KVM": "Y" if value["acceleration"] == "kvm" else "N",
                       "NETWORK": "Y" if value["acceleration"] == "kvm" else "slirp",
                       "DISK_TYPE": "scsi" if value["legacyBind"] else "sata", "DISK_FMT": "raw", "ALLOCATE": "N",
                       "SHUTDOWN": "Y", "QEMU_TIMEOUT": str(self.shutdown_seconds - 15)}
        image = value.get("image", f"winhoo-desktop:{VERSION}-{value['architecture']}")
        service = {"image": image, "platform": "linux/" + value["architecture"],
                   "container_name": value["name"], "hostname": value.get("hostname", "WINHOO-DESKTOP"),
                   "labels": {OWNER: self.owner}, "stop_grace_period": f"{self.shutdown_seconds}s", "restart": "no",
                   "environment": environment,
                   "ports": [{"host_ip": "127.0.0.1", "published": str(port["host"]), "target": port["guest"], "protocol": port["protocol"]} for port in value["ports"]],
                   "volumes": [{"type": "bind" if value["legacyBind"] else "volume", "source": value["storage"] if value["legacyBind"] else "desktop-state", "target": "/storage"},
                               {"type": "bind", "source": value["shared"], "target": "/shared"},
                               {"type": "bind", "source": value["oem"], "target": "/oem", "read_only": True}]}
        if value.get("buildImage", "image" not in value):
            source = ROOT / "desktop"
            dockerfile = f"Dockerfile.{value['architecture']}"
            if not (source / dockerfile).is_file():
                raise DesktopError("WinHoo desktop image source is missing; use the WinHoo source checkout.")
            service["build"] = {"context": str(ROOT), "dockerfile": f"desktop/{dockerfile}"}
        if value["acceleration"] == "kvm":
            service.update(devices=["/dev/kvm:/dev/kvm", "/dev/net/tun:/dev/net/tun"], cap_add=["NET_ADMIN"])
        return {"services": {"desktop": service}, "volumes": {"desktop-state": {"name": value["volume"]}}}

    @property
    def shutdown_seconds(self) -> int:
        # Software CPU emulation also slows Windows service shutdown. Keep the
        # inner QEMU deadline below Compose's grace, and both below our caller.
        return 600 if self.config["acceleration"] == "tcg" else 120

    def run(self, operation: str, *, script=None, timeout=120, pid=None, guest_path=None, max_bytes=65536) -> int:
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            # Cleanup allows an owned in-flight guest command to finish.
            # Startup and other mutations still fail immediately on contention.
            deadline = time.monotonic() + (180 if operation in ('stop', 'down', 'restart') else 0)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise DesktopError("Another WinHoo desktop lifecycle operation is in progress.") from exc
                    time.sleep(0.1)
            return self._run_locked(operation, script=script, timeout=timeout, pid=pid, guest_path=guest_path, max_bytes=max_bytes)
        finally:
            os.close(descriptor)

    def _run_locked(self, operation: str, *, script=None, timeout=120, pid=None, guest_path=None, max_bytes=65536) -> int:
        if operation not in OPERATIONS:
            raise DesktopError("Unsupported desktop operation.")
        self.assert_engine()
        current = self.assert_storage()
        if operation.startswith('guest-'):
            return self.guest(operation[6:], script=script, timeout=timeout, pid=pid, guest_path=guest_path, max_bytes=max_bytes)
        if operation in ('tools-prepare', 'tools-repair'):
            return self.prepare_tools(current, force=operation == 'tools-repair')
        if operation in ('up', 'stop', 'down', 'restart'):
            # Compose selects by project label, even after a container is
            # renamed. Validate its selection before changing any guest.
            members = self.command('ps', '--all', '--quiet', '--no-trunc', '--filter',
                                   'label=com.docker.compose.project=' + self.config['project']).stdout.splitlines()
            if any(member != (current or {}).get('Id') for member in members):
                raise DesktopError('Compose project contains another container; refusing to change it.')
            if current is None and operation in ('down', 'stop'):
                return 0
            if current is None and operation == 'restart':
                raise DesktopError('No existing desktop to restart; use up to start it.')
        if operation == 'up':
            result = self.prepare_tools(current)
            if result or (current and current.get('State', {}).get('Running')):
                # Compose up could recreate a running guest after a config
                # change. Reusing an owned guest must leave its session intact.
                return result
        if operation in ('stop', 'down', 'restart'):
            if current.get('State', {}).get('Running'):
                # The guest agent closes applications using Windows' native
                # shutdown. If unavailable, the existing ACPI grace still runs.
                try:
                    self.guest('shutdown', timeout=15)
                except (DesktopError, OSError, subprocess.TimeoutExpired):
                    pass
        generated = self.path.with_suffix(".compose.json")
        temporary = generated.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(self.compose_configuration(), stream, indent=2)
            stream.write("\n")
        temporary.replace(generated)
        arguments = {"up": ["up", "--detach"], "status": ["ps"], "config": ["config", "--quiet"]}.get(operation, [operation])
        # down intentionally has no --volumes: Windows and its identity persist.
        timeout = 600 if operation == "build" else self.shutdown_seconds + 60 if operation in ("stop", "down", "restart") else 180
        result = self.command("compose", "--env-file", self.config["environmentFile"],
                            "--file", str(generated), "--project-name", self.config["project"],
                            *arguments, check=False, capture=False, timeout=timeout).returncode
        if result == 0 and operation in ("up", "restart") and self.config.get("ownerGeneration"):
            script = (ROOT / "desktop" / "owner-watchdog.sh").read_text(encoding="utf-8")
            self.command("exec", "--detach", self.config["name"], "bash", "-c", script, "winhoo-owner-watchdog",
                         "/shared/bootstrap/winhoo-owner-heartbeat", self.config["ownerGeneration"])
        return result

    def storage_users(self, source):
        if not self.config['legacyBind']:
            return set(self.command('ps', '--quiet', '--no-trunc', '--filter', 'volume=' + source).stdout.splitlines())
        # Docker matches the exact bind source or destination in --filter
        # volume, but misses a parent bind or a directly mounted disk file.
        ids = self.command('ps', '--quiet', '--no-trunc').stdout.splitlines()
        if not ids:
            return set()
        rows = self.command('container', 'inspect', '--format', '{{.Id}} {{json .Mounts}}', *ids).stdout.splitlines()
        try:
            mounts_by_id = {}
            for row in rows:
                identity, raw = row.split(' ', 1)
                mounts_by_id[identity] = json.loads(raw)
            if set(mounts_by_id) != set(ids):
                raise ValueError('incomplete inventory')
            storage = Path(source).resolve()
            users = set()
            for identity, mounts in mounts_by_id.items():
                for mount in mounts:
                    if mount['Type'] != 'bind':
                        continue
                    path = Path(mount['Source'])
                    if not path.is_absolute():
                        raise ValueError('invalid mount source')
                    path = path.resolve()
                    if path == storage or path in storage.parents or storage in path.parents:
                        users.add(identity)
            return users
        except (ValueError, TypeError, KeyError) as exc:
            raise DesktopError('Cannot verify active disk mounts; refusing to prepare guest tools.') from exc

    def prepare_tools(self, current, *, force=False):
        # All users of the storage must be stopped, including containers with
        # a different name. This check precedes the helper's writable mount.
        source = self.config['storage'] if self.config['legacyBind'] else self.config['volume']
        users = self.storage_users(source)
        running = current and current.get('State', {}).get('Running')
        if users or running:
            # up against an already running owned container is idempotent.
            if not force and running and users == {current.get('Id')}:
                return 0
            raise DesktopError('Stop every container using the Windows disk before preparing guest tools.')
        image = self.config.get('image', f"winhoo-desktop:{VERSION}-{self.config['architecture']}")
        mount = 'type=' + ('bind' if self.config['legacyBind'] else 'volume') + ',source=' + source + ',target=/storage'
        args = ['run', '--rm', '--network', 'none', '--mount', mount,
                '--entrypoint', '/usr/bin/python3', image, '/usr/share/winhoo/guest-tools.py', 'prepare']
        if self.config.get('diskBackup') == 'sparse':
            args.append('--sparse-backup')
        if force:
            args.append('--force')
        return self.command(*args, check=False, capture=False, timeout=900).returncode

    def guest(self, operation, *, script=None, timeout=120, pid=None, guest_path=None, max_bytes=65536):
        if not 1 <= timeout <= 14400:
            raise DesktopError('Guest timeout must be between 1 and 14400 seconds.')
        arguments = ['exec']
        data = None
        if operation == 'exec':
            if script is None:
                raise DesktopError('guest-exec requires --script with a UTF-8 PowerShell file.')
            path = Path(script)
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise DesktopError('Guest script must be a file no larger than 1 MiB.')
            data = path.read_text(encoding='utf-8')
            arguments.append('--interactive')
        arguments.extend([self.config['name'], '/usr/bin/python3', '/usr/share/winhoo/guest-tools.py', operation, '--timeout', str(timeout)])
        if pid is not None:
            arguments.extend(['--pid', str(pid)])
        if operation == 'read':
            if not guest_path or '\0' in guest_path or not 1 <= max_bytes <= 1024 * 1024:
                raise DesktopError('guest-read requires --path and a byte limit between 1 and 1048576.')
            arguments.extend(['--path', guest_path, '--max-bytes', str(max_bytes)])
        overhead = 195 if operation == 'exec' else 45
        return self.command(*arguments, check=False, capture=False, timeout=timeout + overhead, input_data=data).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument('--script', type=Path)
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--path', dest='guest_path')
    parser.add_argument('--max-bytes', type=int, default=65536)
    args = parser.parse_args(argv)
    try:
        return Desktop(args.config).run(args.operation, script=args.script, timeout=args.timeout, pid=args.pid,
                                       guest_path=args.guest_path, max_bytes=args.max_bytes)
    except (DesktopError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"winhoo desktop: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
