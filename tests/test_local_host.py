"""Boundary tests for the separate local Windows container-host VM."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid


SOURCE = Path(__file__).resolve().parents[1] / "host" / "local-vm" / "local_host.py"
SPEC = importlib.util.spec_from_file_location("winhoo_local_host", SOURCE)
local_host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_host)


class LinuxEngine:
    def __init__(self):
        self.commands = []
        self.endpoint = "unix:///local/docker.sock"
        self.ram = 16 * local_host.GIB
        self.os = "linux"
        self.identity = "a" * 64
        self.state = None
        self.container = None
        self.volume = None
        self.on_build = lambda: None

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        def result(stdout="", code=0, stderr=""):
            return subprocess.CompletedProcess(argv, code, stdout, stderr)
        if argv[:3] == ["docker", "context", "show"]:
            return result("local-linux\n")
        if argv[:3] == ["docker", "context", "inspect"]:
            return result(json.dumps([{"Endpoints": {"docker": {"Host": self.endpoint}}}]))
        if argv[0] == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            key.write_text("test private key")
            key.with_suffix(".pub").write_text("ssh-ed25519 test-public")
            return result()
        if argv[0] == "ssh":
            return result(json.dumps({"user": "WinhooHost", "os": "windows", "version": "29.9.3"}))
        if argv[:2] != ["docker", "--context"]:
            raise AssertionError(argv)
        command = argv[3:]
        if command[0] == "info":
            return result(json.dumps({"OSType": self.os, "Architecture": "aarch64", "MemTotal": self.ram}))
        if command[:2] == ["container", "inspect"]:
            return result(json.dumps([self.container]))
        if command[0] == "build":
            self.on_build()
            return result()
        if command[:2] == ["volume", "create"]:
            self.volume = {"Labels": self.labels(command)}
            return result(command[-1])
        if command[:2] == ["volume", "inspect"]:
            return result(json.dumps([self.volume]))
        if command[0] == "create":
            self.container = {"Id": self.identity, "Config": {"Labels": self.labels(command)}, "State": {"Running": False, "Status": "created", "ExitCode": 0}}
            return result(self.identity)
        if command[0] == "start":
            self.container["State"].update(Running=True, Status="running")
            return result()
        if command[0] == "stop":
            self.container["State"].update(Running=False, Status="exited")
            return result()
        if command[0] == "logs":
            return result("boot diagnostic")
        raise AssertionError(command)

    @staticmethod
    def labels(command):
        return dict(command[index + 1].split("=", 1) for index, part in enumerate(command) if part == "--label")


class LocalHostSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "local-host"
        self.engine = LinuxEngine()
        self.free = 50 * local_host.GIB
        self.now = 0
        self.host = local_host.LocalHost(self.path, runner=self.engine, free_bytes=lambda: self.free, clock=lambda: self.now, sleep=self.advance)

    def advance(self, seconds):
        self.now += seconds

    def owned_state(self):
        self.path.mkdir(mode=0o700)
        state = {"schema": 1, "uuid": str(uuid.uuid4()), "context": "local-linux", "endpoint": self.engine.endpoint, "image": local_host.IMAGE, "containerId": self.engine.identity, "volume": "winhoo-owned-test-volume"}
        self.host.save(state)
        self.engine.state = state
        self.engine.container = {"Id": self.engine.identity, "Config": {"Labels": self.host.labels(state)}, "State": {"Running": True, "Status": "running", "ExitCode": 0}}
        return state

    def receipt(self, state, **overrides):
        raw = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"a" * 32
        receipt = {"instanceId": state["uuid"], "status": "ready", "phase": "ready", "user": "WinhooHost", "dockerVersion": "29.9.3", "sshHostPublicKey": "ssh-ed25519 " + base64.b64encode(raw).decode()}
        receipt.update(overrides)
        parent = self.path / "shared" / "bootstrap"
        parent.mkdir(parents=True, exist_ok=True)
        (parent / "host-status.json").write_text(json.dumps(receipt))
        return receipt

    def test_fresh_host_refuses_insufficient_disk_before_build_or_state_creation(self):
        self.free = 44 * local_host.GIB
        with self.assertRaisesRegex(local_host.HostError, "45 GiB"):
            self.host.up()
        self.assertFalse(self.path.exists())
        self.assertFalse(any("build" in command or "create" in command for command in self.engine.commands))

    def test_remote_linux_context_is_rejected_for_local_mounts(self):
        self.engine.endpoint = "ssh://linux-host"
        with self.assertRaisesRegex(local_host.HostError, "local Unix-socket"):
            self.host.doctor()
        self.assertFalse(self.path.exists())

    def test_insufficient_engine_ram_is_rejected(self):
        self.engine.ram = 11 * local_host.GIB
        with self.assertRaisesRegex(local_host.HostError, "12 GiB RAM"):
            self.host.up()
        self.assertFalse(self.path.exists())

    def test_low_disk_guard_stops_only_owned_id_and_retains_disk(self):
        state = self.owned_state()
        self.free = 7 * local_host.GIB
        with self.assertRaisesRegex(local_host.HostError, "was stopped"):
            self.host.wait(timeout=1)
        stops = [command for command in self.engine.commands if "stop" in command]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0][-1], state["containerId"])
        self.assertFalse(self.engine.container["State"]["Running"])
        self.assertTrue((self.path / "state.json").exists())
        self.assertFalse(any("rm" in command for command in self.engine.commands))

    def test_status_also_applies_low_disk_guard(self):
        self.owned_state()
        self.free = 7 * local_host.GIB
        with self.assertRaisesRegex(local_host.HostError, "was stopped"):
            self.host.status()
        self.assertFalse(self.engine.container["State"]["Running"])

    def test_foreign_ownership_prevents_even_emergency_stop(self):
        self.owned_state()
        self.engine.container["Config"]["Labels"] = {}
        self.free = 7 * local_host.GIB
        with self.assertRaisesRegex(local_host.HostError, "ownership"):
            self.host.wait(timeout=1)
        self.assertFalse(any("stop" in command for command in self.engine.commands))

    def test_bootstrap_receipt_cannot_pin_a_different_instance(self):
        state = self.owned_state()
        self.receipt(state, instanceId=str(uuid.uuid4()))
        with self.assertRaisesRegex(local_host.HostError, "different local-host"):
            self.host.wait(timeout=1)
        self.assertFalse((self.path / "known_hosts").exists())
        self.assertFalse(any(command[0] == "ssh" for command in self.engine.commands))

    def test_ready_requires_live_ssh_windows_engine_attestation(self):
        state = self.owned_state()
        self.receipt(state)
        with patch("sys.stderr", new=io.StringIO()):
            result = self.host.wait(timeout=1)
        self.assertTrue(result["ready"])
        self.assertFalse(result["nativeContainerTested"])
        self.assertEqual(result["dockerVersion"], "29.9.3")
        self.assertIn("--host-ssh-config", result["controllerDoctorCommand"])
        self.assertNotIn("PASSWORD", json.dumps(result))

    def test_timeout_stops_owned_vm_but_preserves_state(self):
        self.owned_state()
        with patch("sys.stderr", new=io.StringIO()):
            with self.assertRaisesRegex(local_host.HostError, "readiness timed out"):
                self.host.wait(timeout=2)
        self.assertFalse(self.engine.container["State"]["Running"])
        self.assertTrue((self.path / "state.json").exists())

    def test_postbuild_disk_refusal_is_retryable_without_replacing_identity(self):
        self.engine.on_build = lambda: setattr(self, "free", 44 * local_host.GIB)
        with patch.object(self.host, "ports_available"):
            with self.assertRaisesRegex(local_host.HostError, "45 GiB free space"):
                self.host.up()
            self.assertIsNone(self.engine.container)
            self.assertIsNone(self.engine.volume)
            state = self.host.load()
            original_key = (self.path / "id_ed25519").read_bytes()
            original_env = (self.path / "guest.env").read_bytes()
            self.assertEqual((self.path / "guest.env").stat().st_mode & 0o777, 0o600)
            self.free = 50 * local_host.GIB
            self.engine.on_build = lambda: None
            result = self.host.up()
        self.assertEqual(self.host.load()["uuid"], state["uuid"])
        self.assertEqual((self.path / "id_ed25519").read_bytes(), original_key)
        self.assertEqual((self.path / "guest.env").read_bytes(), original_env)
        self.assertEqual(result["containerId"], self.engine.identity)
        self.assertEqual(len([command for command in self.engine.commands if command[0] == "ssh-keygen"]), 1)
        create = next(command for command in self.engine.commands if "create" in command and "--name" in command)
        self.assertIn("127.0.0.1:2228:22/tcp", create)
        self.assertIn("127.0.0.1:8016:8006/tcp", create)
        self.assertNotIn("PASSWORD", " ".join(create))


if __name__ == "__main__":
    unittest.main()
