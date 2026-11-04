"""Exercise controller trust, rollback, routing and failure boundaries without an engine."""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import winhoo


def key_archive(seed: bytes = b"a") -> bytes:
    raw = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + seed * 32
    public = b"ssh-ed25519 " + base64.b64encode(raw) + b" test\n"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo("ssh_host_ed25519_key.pub")
        member.size = len(public)
        archive.addfile(member, io.BytesIO(public))
    return output.getvalue()


class Engine:
    """A small stateful engine double: foreign resources and failures remain visible."""
    def __init__(self):
        self.commands = []
        self.inputs = []
        self.uploaded_files = []
        self.endpoint = "ssh://Jan@windows-test:222"
        self.os_type = "windows"
        self.volumes = {}
        self.containers = {}
        self.next_id = 1
        self.ssh_code = 0
        self.host_key = key_archive()
        self.exit_on_start = False
        self.exit_code = 19
        self.fail_copy = False
        self.fail_volume_create = False
        self.ssh_json = {"winhoo": "ready", "user": "winhoo", "platform": "Win32NT"}

    def result(self, argv, kwargs, code=0, output="", error=""):
        if not kwargs.get("text", True):
            output = output.encode() if isinstance(output, str) else output
            error = error.encode() if isinstance(error, str) else error
        return subprocess.CompletedProcess(argv, code, output, error)

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        self.inputs.append(kwargs.get("input"))
        if argv[:3] == ["docker", "context", "show"]:
            return self.result(argv, kwargs, output="test-context\n")
        if argv[:3] == ["docker", "context", "inspect"]:
            return self.result(argv, kwargs, output=json.dumps([{"Endpoints": {"docker": {"Host": self.endpoint}}}]))
        if argv[0] == "ssh-keygen":
            private = Path(argv[argv.index("-f") + 1])
            private.write_text("private test key")
            private.with_suffix(".pub").write_text("ssh-ed25519 test")
            return self.result(argv, kwargs)
        if argv[0] == "ssh":
            return self.result(argv, kwargs, code=self.ssh_code, output=json.dumps(self.ssh_json) if self.ssh_code == 0 else "", error="authentication failed" if self.ssh_code else "")
        if argv[:2] != ["docker", "--context"]:
            raise AssertionError(f"Unexpected command: {argv}")
        command = argv[3:]
        if command[:1] == ["info"]:
            return self.result(argv, kwargs, output=json.dumps({"OSType": self.os_type, "OperatingSystem": "Windows Test", "Architecture": "x86_64", "KernelVersion": "test"}))
        if command[:2] == ["volume", "create"]:
            if self.fail_volume_create:
                return self.result(argv, kwargs, 1, error="Simulated volume provisioning failure")
            labels = self.get_labels(command)
            self.volumes.setdefault(command[-1], {"Name": command[-1], "Labels": labels})
            return self.result(argv, kwargs, output=command[-1])
        if command[:2] == ["volume", "inspect"]:
            record = self.volumes.get(command[-1])
            return self.result(argv, kwargs, output=json.dumps([record])) if record else self.result(argv, kwargs, 1, error="Error response from daemon: no such volume")
        if command[:2] == ["volume", "rm"]:
            self.volumes.pop(command[-1])
            return self.result(argv, kwargs)
        if command[0] == "create":
            name = command[command.index("--name") + 1]
            if any(record["Name"] == name for record in self.containers.values()):
                return self.result(argv, kwargs, 1, error="Conflict: container name in use")
            identity = f"{self.next_id:064x}"
            self.next_id += 1
            self.containers[identity] = {"Id": identity, "Name": name, "Config": {"Labels": self.get_labels(command)}, "State": {"Running": False, "Status": "created", "ExitCode": 0}, "NetworkSettings": {"Networks": {"nat": {"IPAddress": "172.28.0.7"}}}}
            return self.result(argv, kwargs, output=identity)
        if command[:2] == ["container", "inspect"]:
            record = self.containers.get(command[-1])
            return self.result(argv, kwargs, output=json.dumps([record])) if record else self.result(argv, kwargs, 1, error="Error response from daemon: no such container")
        if command[0] == "cp":
            if self.fail_copy:
                return self.result(argv, kwargs, 1, error="Simulated staging failure")
            if command[-1] == "-":
                return self.result(argv, kwargs, output=self.host_key)
            source = Path(command[-2])
            if source.is_file():
                self.uploaded_files.append((str(source), command[-1], source.read_bytes()))
            return self.result(argv, kwargs)
        if command[0] == "start":
            state = self.containers[command[-1]]["State"]
            state.update(Running=not self.exit_on_start, Status="exited" if self.exit_on_start else "running", ExitCode=self.exit_code if self.exit_on_start else 0)
            return self.result(argv, kwargs)
        if command[0] == "logs":
            return self.result(argv, kwargs, output="bootstrap diagnostic\n")
        if command[0] == "rm":
            self.containers.pop(command[-1])
            return self.result(argv, kwargs)
        if command[0] == "build":
            return self.result(argv, kwargs)
        raise AssertionError(f"Unexpected Docker command: {command}")

    @staticmethod
    def get_labels(command):
        labels = {}
        for index, value in enumerate(command):
            if value == "--label":
                key, label = command[index + 1].split("=", 1)
                labels[key] = label
        return labels


class ControllerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.engine = Engine()
        self.now = 0
        self.controller = winhoo.Controller("test-context", self.directory / "state", runner=self.engine, clock=lambda: self.now, sleep=self.advance)

    def advance(self, seconds):
        self.now += seconds

    def up(self):
        return self.controller.up("dev", timeout=3)

    def test_linux_engine_fails_before_creating_resources(self):
        self.engine.os_type = "linux"
        with self.assertRaisesRegex(winhoo.WinhooError, "Linux|linux engine"):
            self.up()
        self.assertEqual(self.engine.containers, {})
        self.assertEqual(self.engine.volumes, {})
        self.assertFalse(self.controller.instance_dir("dev").exists())

    def test_nonfinite_timeout_is_rejected_before_creating_resources(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(winhoo.WinhooError, "finite positive"):
                    self.controller.up("dev", timeout=timeout)
                self.assertFalse(self.engine.commands)

    def test_authenticated_readiness_and_private_remote_route(self):
        result = self.up()
        self.assertTrue(result["ssh_ready"])
        state = self.controller.load("dev")
        command = self.controller.ssh_command(state)
        self.assertEqual(command[command.index("-J") + 1], "Jan@windows-test:222")
        self.assertEqual(command[-1], "winhoo@172.28.0.7")
        create = next(command for command in self.engine.commands if "create" in command and "--name" in command)
        self.assertNotIn("--publish", create)
        self.assertNotIn("-p", create)
        config = (self.controller.instance_dir("dev") / "ssh_config").read_text()
        self.assertIn("StrictHostKeyChecking yes", config)
        self.assertTrue((self.controller.instance_dir("dev") / "known_hosts").read_text().startswith("winhoo-" + state["uuid"]))

    def test_failed_staging_rolls_back_only_new_owned_resources(self):
        self.engine.fail_copy = True
        foreign_id = "f" * 64
        self.engine.containers[foreign_id] = {"Id": foreign_id, "Name": "unrelated", "Config": {"Labels": {}}, "State": {"Running": True}}
        with self.assertRaisesRegex(winhoo.WinhooError, "staging failure"):
            self.up()
        self.assertEqual(set(self.engine.containers), {foreign_id})
        self.assertEqual(self.engine.volumes, {})
        self.assertFalse(self.controller.instance_dir("dev").exists())

    def test_failed_volume_creation_cleans_partial_state_with_lowercase_missing_error(self):
        self.engine.fail_volume_create = True
        with self.assertRaisesRegex(winhoo.WinhooError, "volume provisioning failure"):
            self.up()
        self.assertEqual(self.engine.volumes, {})
        self.assertEqual(self.engine.containers, {})
        self.assertFalse(self.controller.instance_dir("dev").exists())

    def test_foreign_name_collision_is_not_adopted_or_removed(self):
        foreign_id = "f" * 64
        self.engine.containers[foreign_id] = {"Id": foreign_id, "Name": "winhoo-dev", "Config": {"Labels": {}}, "State": {"Running": True}}
        with self.assertRaisesRegex(winhoo.WinhooError, "Conflict"):
            self.up()
        self.assertEqual(set(self.engine.containers), {foreign_id})

    def test_reused_name_cannot_redirect_down_to_new_container(self):
        result = self.up()
        self.engine.containers.pop(result["container_id"])
        foreign_id = "f" * 64
        self.engine.containers[foreign_id] = {"Id": foreign_id, "Name": "winhoo-dev", "Config": {"Labels": {}}, "State": {"Running": True}}
        self.controller.down("dev", purge=True)
        self.assertEqual(set(self.engine.containers), {foreign_id})

    def test_changed_ownership_blocks_all_removal(self):
        result = self.up()
        self.engine.containers[result["container_id"]]["Config"]["Labels"] = {}
        with self.assertRaisesRegex(winhoo.WinhooError, "ownership"):
            self.controller.down("dev", purge=True)
        self.assertIn(result["container_id"], self.engine.containers)
        self.assertTrue(self.engine.volumes)

    def test_changed_context_endpoint_blocks_operation(self):
        self.up()
        self.engine.endpoint = "ssh://Jan@different-host"
        with self.assertRaisesRegex(winhoo.WinhooError, "endpoint changed"):
            self.controller.down("dev")
        self.assertTrue(self.engine.containers)

    def test_existing_instance_does_not_silently_accept_a_different_image(self):
        self.up()
        with self.assertRaisesRegex(winhoo.WinhooError, "different image"):
            self.controller.up("dev", image="different:latest")
        self.assertEqual(len(self.engine.containers), 1)

    def test_changed_guest_host_key_is_not_adopted(self):
        self.up()
        original = (self.controller.instance_dir("dev") / "known_hosts").read_bytes()
        self.engine.host_key = key_archive(b"b")
        with self.assertRaisesRegex(winhoo.WinhooError, "host key changed"):
            self.controller.wait_ready(self.controller.load("dev"), timeout=3)
        self.assertEqual((self.controller.instance_dir("dev") / "known_hosts").read_bytes(), original)

    def test_running_but_unauthenticated_guest_times_out(self):
        self.engine.ssh_code = 255
        with self.assertRaisesRegex(winhoo.WinhooError, "readiness timed out.*authentication failed"):
            self.up()
        self.assertEqual(self.engine.containers, {})
        self.assertEqual(self.engine.volumes, {})

    def test_exited_guest_reports_exit_and_logs(self):
        self.engine.exit_on_start = True
        with self.assertRaisesRegex(winhoo.WinhooError, r"exit 19.*\nbootstrap diagnostic"):
            self.up()
        self.assertEqual(self.engine.containers, {})

    def test_wrong_guest_identity_is_not_ready(self):
        self.engine.ssh_json["user"] = "OtherAdmin"
        with self.assertRaisesRegex(winhoo.WinhooError, "unexpected readiness identity"):
            self.up()

    def test_native_command_exit_code_is_preserved_and_source_is_encoded(self):
        self.up()
        self.engine.ssh_code = 23
        source = "Write-Output 'hello $world; ` &'; cmd.exe /c exit 23"
        result = self.controller.execute("dev", source, capture=True)
        self.assertEqual(result.returncode, 23)
        command = self.engine.commands[-1]
        decoded = base64.b64decode(command[-1]).decode("utf-16le")
        self.assertIn("[Console]::In.ReadToEnd()", decoded)
        self.assertIn(source, self.engine.inputs[-1])
        self.assertIn("exit $global:LASTEXITCODE", self.engine.inputs[-1])
        self.assertNotIn(source, command)

    def test_large_unicode_script_uses_utf8_stdin_and_short_command(self):
        self.up()
        source = "Write-Output 'Grüße 🦉'\n" * 5000
        self.controller.execute("dev", source, capture=True)
        command = self.engine.commands[-1]
        self.assertLess(sum(len(part) for part in command), 3000)
        self.assertIn(source, self.engine.inputs[-1])
        bootstrap = base64.b64decode(command[-1]).decode("utf-16le")
        self.assertIn("[Console]::InputEncoding=$utf8", bootstrap)

    def test_script_stages_actual_file_and_removes_it_in_finally(self):
        self.up()
        script = self.directory / "uses root.ps1"
        script.write_text("Write-Output $PSScriptRoot")
        self.controller.script("dev", script, capture=True)
        uploads = [upload for upload in self.engine.uploaded_files if ":C:/work/.winhoo-script-" in upload[1]]
        self.assertEqual(len(uploads), 1)
        self.assertFalse(Path(uploads[0][0]).exists())
        self.assertIn("try { & 'C:/work/.winhoo-script-", self.engine.inputs[-1])
        self.assertIn("finally { Remove-Item -LiteralPath", self.engine.inputs[-1])

    def test_script_adds_one_utf8_bom_at_upload_without_modifying_source(self):
        self.up()
        for has_bom in (False, True):
            with self.subTest(has_bom=has_bom):
                script = self.directory / "unicode.ps1"
                source = "Write-Output 'Grüße 🦉'\r\nWrite-Output $PSScriptRoot\r\n".encode("utf-8")
                original = (b"\xef\xbb\xbf" if has_bom else b"") + source
                script.write_bytes(original)
                self.controller.script("dev", script, capture=True)
                staged_path, destination, staged_bytes = self.engine.uploaded_files[-1]
                self.assertTrue(destination.endswith(".ps1"))
                self.assertEqual(staged_bytes, b"\xef\xbb\xbf" + source)
                self.assertEqual(script.read_bytes(), original)
                self.assertFalse(Path(staged_path).exists())

    def test_script_rejects_unsupported_encoding_before_upload(self):
        self.up()
        script = self.directory / "legacy.ps1"
        script.write_bytes("Write-Output 'Grüße'".encode("cp1252"))
        count = len(self.engine.uploaded_files)
        with self.assertRaisesRegex(winhoo.WinhooError, "scripts must use UTF-8"):
            self.controller.script("dev", script, capture=True)
        self.assertEqual(len(self.engine.uploaded_files), count)

    def test_remote_file_upload_preserves_literal_local_path(self):
        self.up()
        source = self.directory / "file with spaces $and; quotes'.txt"
        source.write_text("test")
        self.controller.copy("dev", str(source), "C:/work/file with spaces.txt")
        command = self.engine.commands[-1]
        self.assertEqual(command[-2], str(source.resolve()))
        self.assertTrue(command[-1].endswith(":C:/work/file with spaces.txt"))

    def test_down_preserves_key_volume_and_up_reuses_its_trust(self):
        first = self.up()
        original_pin = (self.controller.instance_dir("dev") / "known_hosts").read_bytes()
        self.controller.down("dev")
        self.assertEqual(self.engine.containers, {})
        self.assertEqual(len(self.engine.volumes), 1)
        second = self.up()
        self.assertNotEqual(first["container_id"], second["container_id"])
        self.assertEqual((self.controller.instance_dir("dev") / "known_hosts").read_bytes(), original_pin)

    def test_failed_recreation_preserves_existing_owned_volume(self):
        self.up()
        self.controller.down("dev")
        self.engine.fail_copy = True
        with self.assertRaisesRegex(winhoo.WinhooError, "staging failure"):
            self.up()
        self.assertEqual(self.engine.containers, {})
        self.assertEqual(len(self.engine.volumes), 1)
        self.assertTrue(self.controller.instance_dir("dev").is_dir())

    def test_status_does_not_trust_stale_ready_flag(self):
        self.up()
        self.engine.ssh_code = 255
        self.assertFalse(self.controller.status("dev")["ssh_ready"])


class TransportAndArchiveTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "The private Docker SSH adapter is for POSIX controllers")
    def test_private_config_reaches_real_docker_child_without_changing_parent_path(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = folder / "host 'config' $(unused)"
            config.write_text("Host private-host\n    StrictHostKeyChecking yes\n")
            docker = folder / "docker"
            docker.write_text('#!/bin/sh\nexec ssh private-host docker system dial-stdio\n')
            docker.chmod(0o700)
            ssh = folder / "ssh"
            ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            ssh.chmod(0o700)
            expected_path = str(folder) + os.pathsep + os.environ.get("PATH", os.defpath)
            with patch.dict(os.environ, {"PATH": expected_path}):
                controller = winhoo.Controller(host_ssh_config=config)
                result = controller.docker("private", "info")
                self.assertEqual(result.stdout.splitlines(), ["-F", str(config.resolve()), "private-host", "docker", "system", "dial-stdio"])
                self.assertEqual(os.environ["PATH"], expected_path)

    def test_remote_named_pipe_and_tcp_endpoints_are_rejected(self):
        for endpoint, local in (("tcp://host:2376", False), ("unix:///var/run/docker.sock", False), ("npipe:////remote/pipe/docker_engine", True), ("npipe:////./pipe/docker_engine", False)):
            with self.subTest(endpoint=endpoint, local=local):
                with self.assertRaises(winhoo.WinhooError):
                    winhoo.jump_host(endpoint, local_windows=local)

    def test_local_windows_named_pipe_has_no_jump(self):
        self.assertIsNone(winhoo.jump_host("npipe:////./pipe/docker_engine", local_windows=True))

    def test_ipv6_and_ssh_config_aliases_are_preserved(self):
        self.assertEqual(winhoo.jump_host("ssh://admin@[fd7a:115c:a1e0::1]:2222"), "admin@[fd7a:115c:a1e0::1]:2222")
        self.assertEqual(winhoo.jump_host("ssh://windows-alias"), "windows-alias")

    def test_endpoint_with_embedded_password_is_rejected(self):
        with self.assertRaises(winhoo.WinhooError):
            winhoo.jump_host("ssh://user:secret@host")

    def test_invalid_or_oversized_public_key_archive_is_rejected(self):
        with self.assertRaises(winhoo.WinhooError):
            winhoo.host_key_from_tar(b"not a tar")
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            member = tarfile.TarInfo("key")
            member.size = 17000
            archive.addfile(member, io.BytesIO(b"x" * member.size))
        with self.assertRaises(winhoo.WinhooError):
            winhoo.host_key_from_tar(output.getvalue())

    def test_instance_path_traversal_is_rejected(self):
        for name in ("../other", "foo/bar", "A", "foo bar", "-flag"):
            with self.subTest(name=name):
                with self.assertRaises(winhoo.WinhooError):
                    winhoo.valid_name(name)


class PrivateHostTransportTests(unittest.TestCase):
    setUp = ControllerSafetyTests.setUp
    advance = ControllerSafetyTests.advance
    up = ControllerSafetyTests.up
    def test_private_host_config_is_retained_for_docker_and_guest_jump(self):
        config = self.directory / "host_config"
        config.write_text("Host windows-test\n    HostName 127.0.0.1\n")
        self.controller.host_ssh_config = config
        self.up()
        state = self.controller.load("dev")
        self.assertEqual(state["host_ssh_config"], str(config))
        guest_config = (self.controller.instance_dir("dev") / "ssh_config").read_text()
        self.assertIn(f'Include "{config}"', guest_config)
        resumed = winhoo.Controller(state_dir=self.controller.state_dir, runner=self.engine)
        resumed.load("dev")
        self.assertEqual(resumed.host_ssh_config, config)

    def test_private_host_config_cannot_change_for_existing_instance(self):
        config = self.directory / "host_config"
        config.write_text("Host windows-test\n")
        self.controller.host_ssh_config = config
        self.up()
        other = self.directory / "other_config"
        other.write_text("Host different-host\n")
        changed = winhoo.Controller(state_dir=self.controller.state_dir, host_ssh_config=other, runner=self.engine)
        with self.assertRaisesRegex(winhoo.WinhooError, "different host SSH configuration"):
            changed.down("dev", purge=True)
        self.assertTrue(self.engine.containers)


class DisposableRunTests(unittest.TestCase):
    def test_failure_and_interrupt_still_cleanup(self):
        for failure in (None, KeyboardInterrupt()):
            with self.subTest(failure=failure):
                with tempfile.TemporaryDirectory() as directory:
                    controller = winhoo.Controller(state_dir=directory)
                    names = []
                    def up(name, **kwargs):
                        names.append(name)
                        controller.instance_dir(name).mkdir()
                        (controller.instance_dir(name) / "state.json").write_text("{}")
                    with patch("winhoo.Controller", return_value=controller), patch.object(controller, "up", side_effect=up), patch.object(controller, "execute", side_effect=failure, return_value=subprocess.CompletedProcess([], 23)), patch.object(controller, "down") as down:
                        result = winhoo.main(["run", "exit 23"])
                    self.assertEqual(result, 130 if failure else 23)
                    down.assert_called_once_with(names[0], purge=True)

    def test_cleanup_failure_preserves_failed_test_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = winhoo.Controller(state_dir=directory)
            def up(name, **kwargs):
                controller.instance_dir(name).mkdir()
                (controller.instance_dir(name) / "state.json").write_text("{}")
            with patch("winhoo.Controller", return_value=controller), patch.object(controller, "up", side_effect=up), patch.object(controller, "execute", return_value=subprocess.CompletedProcess([], 23)), patch.object(controller, "down", side_effect=winhoo.WinhooError("ownership changed")):
                self.assertEqual(winhoo.main(["run", "exit 23"]), 23)


if __name__ == "__main__":
    unittest.main()
