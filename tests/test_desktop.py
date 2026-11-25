"""Ownership and lifecycle boundary tests for the additional desktop VM backend."""
from __future__ import annotations
import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import time
import threading
import unittest

from winhoo_desktop import Desktop, DesktopError, OWNER


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("storage", "shared", "oem"):
            (self.root / name).mkdir()
        (self.root / ".env").write_text("WINDOWS_PASSWORD=not-a-real-password\n")
        self.configuration = {"schema": "winhoo.desktop/1", "name": "test-desktop", "project": "test-desktop", "volume": "test-volume", "engine": "docker", "architecture": "arm64", "acceleration": "tcg", "environmentFile": str(self.root / ".env"), "storage": str(self.root / "storage"), "shared": str(self.root / "shared"), "oem": str(self.root / "oem"), "legacyBind": False, "adoptLegacy": False, "environment": {"VERSION": "11", "RAM_SIZE": "8G"}, "ports": [{"host": 8008, "guest": 8006, "protocol": "tcp"}]}
        self.path = self.root / "desktop.json"
        self.calls = []
        self.record = None
        self.project_members = []
        self.active_mounts = {}
        self.engine_error = None
        self.result = 0
        self.info = {"OSType": "linux", "Architecture": "aarch64", "ID": "fixture-engine"}

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[1] == "info":
            return subprocess.CompletedProcess(args, 0, json.dumps(self.info), "")
        if args[1] == 'ps':
            if any(item.startswith('label=') for item in args):
                ids = self.project_members
            elif '--filter' in args:
                ids = [self.record['Id']] if self.record and self.record['State']['Running'] else []
            else:
                ids = self.active_mounts
            return subprocess.CompletedProcess(args, 0, '\n'.join(ids), '')
        if args[1:3] == ["container", "inspect"]:
            if self.engine_error:
                return subprocess.CompletedProcess(args, 1, "", self.engine_error)
            if '--format' in args:
                return subprocess.CompletedProcess(args, 0, '\n'.join(key + ' ' + json.dumps(mounts) for key, mounts in self.active_mounts.items()), '')
            return subprocess.CompletedProcess(args, 0, json.dumps([self.record]), "") if self.record else subprocess.CompletedProcess(args, 1, "", "No such container")
        return subprocess.CompletedProcess(args, self.result, "", "")

    def desktop(self):
        self.path.write_text(json.dumps(self.configuration))
        return Desktop(self.path, runner=self.runner)

    def existing(self, desktop, *, owner=True):
        self.record = {"Id": "a" * 64, "State": {"Running": False}, "Mounts": [{"Type": "volume", "Name": "test-volume", "Destination": "/storage"}, {"Type": "bind", "Source": str(self.root / "shared"), "Destination": "/shared"}], "Config": {"Labels": {OWNER: desktop.owner} if owner else {}}}

    def test_software_emulation_uses_native_arm_image_and_x64_windows(self):
        desktop = self.desktop()
        self.assertEqual(desktop.run("up"), 0)
        generated = json.loads(self.path.with_suffix(".compose.json").read_text())
        guest = generated["services"]["desktop"]
        self.assertEqual(guest["platform"], "linux/arm64")
        self.assertEqual(guest["environment"]["PLATFORM"], "x64")
        self.assertEqual(guest["environment"]["KVM"], "N")
        self.assertEqual(guest["environment"]["DISK_TYPE"], "sata")
        self.assertEqual(guest["restart"], "no")
        self.assertEqual(guest["ports"][0]["host_ip"], "127.0.0.1")
        self.assertNotIn("not-a-real-password", json.dumps(generated))
        self.assertEqual(self.path.with_suffix(".compose.json").stat().st_mode & 0o777, 0o600)

    def test_kvm_is_explicit_and_x64_only(self):
        self.configuration.update(architecture="amd64", acceleration="kvm")
        guest = self.desktop().compose_configuration()["services"]["desktop"]
        self.assertIn("/dev/kvm:/dev/kvm", guest["devices"])
        self.assertEqual(guest["environment"]["KVM"], "Y")
        self.assertEqual(guest["build"]["dockerfile"], "desktop/Dockerfile.amd64")
        self.configuration["architecture"] = "arm64"
        with self.assertRaises(DesktopError):
            self.desktop()

    def test_wrong_storage_or_owner_prevents_stop(self):
        desktop = self.desktop()
        self.existing(desktop)
        self.record["Mounts"][0]["Name"] = "foreign-volume"
        with self.assertRaisesRegex(DesktopError, "another owner"):
            desktop.run("stop")
        self.existing(desktop)
        self.record["Config"]["Labels"][OWNER] = "foreign-owner"
        with self.assertRaisesRegex(DesktopError, "another owner"):
            desktop.run("down")
        self.assertFalse(any("compose" in call for call in self.calls))

    def test_legacy_import_requires_explicit_opt_in_and_exact_mounts(self):
        desktop = self.desktop()
        self.existing(desktop, owner=False)
        with self.assertRaises(DesktopError):
            desktop.run("up")
        self.configuration["adoptLegacy"] = True
        desktop = self.desktop()
        self.assertEqual(desktop.run("up"), 0)
        self.record["Mounts"][1]["Source"] = "/different/root"
        with self.assertRaises(DesktopError):
            desktop.run("stop")

    def test_bind_storage_does_not_change_disk_controller_on_resume(self):
        self.configuration["legacyBind"] = True
        guest = self.desktop().compose_configuration()["services"]["desktop"]
        self.assertEqual(guest["environment"]["DISK_TYPE"], "scsi")
        self.assertEqual(guest["volumes"][0]["source"], str(self.root / "storage"))

    def test_prebuilt_image_runs_without_a_source_checkout_or_build(self):
        self.configuration.update(image="winhoo-desktop:0.3.0-arm64", buildImage=False)
        guest = self.desktop().compose_configuration()["services"]["desktop"]
        self.assertEqual(guest["image"], "winhoo-desktop:0.3.0-arm64")
        self.assertNotIn("build", guest)
        self.assertEqual(self.desktop().run("up"), 0)

    def test_engine_failure_is_not_treated_as_absent_container(self):
        self.engine_error = "Cannot connect to the Docker daemon"
        with self.assertRaisesRegex(DesktopError, "Cannot inspect"):
            self.desktop().run("up")
        self.assertFalse(any("compose" in call for call in self.calls))

    def test_engine_context_change_cannot_attach_or_stop_the_saved_disk(self):
        desktop = self.desktop()
        desktop.run("config")
        self.calls.clear()
        self.info["ID"] = "another-daemon"
        with self.assertRaisesRegex(DesktopError, "engine identity changed"):
            desktop.run("stop")
        self.assertFalse(any("compose" in call for call in self.calls))

    def test_windows_engine_is_rejected_before_build_or_mutation(self):
        self.info["OSType"] = "windows"
        with self.assertRaisesRegex(DesktopError, "Linux engine"):
            self.desktop().run("build")
        self.assertFalse(any("compose" in call for call in self.calls))

    def test_down_preserves_windows_disk_and_returns_engine_failure(self):
        desktop = self.desktop()
        self.existing(desktop)
        self.result = 23
        self.assertEqual(desktop.run("down"), 23)
        self.assertEqual(self.calls[-1][-1], "down")
        self.assertNotIn("--volumes", self.calls[-1])
        self.assertNotIn("-v", self.calls[-1])

    def test_tcg_shutdown_budget_reaches_the_engine_without_an_early_controller_timeout(self):
        # The real emulated Windows desktop was still shutting down after the
        # base image's 95-second grace. Model a slow engine call at the actual
        # subprocess boundary so neither wrapper can cut the guest short.
        desktop = self.desktop()
        normal_runner = self.runner
        self.existing(desktop)
        observed_timeout = []

        def slow_shutdown(args, **kwargs):
            if "compose" in args and args[-1] == "stop":
                observed_timeout.append(kwargs["timeout"])
                if kwargs["timeout"] < 600:
                    raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return normal_runner(args, **kwargs)

        desktop.runner = slow_shutdown
        self.assertEqual(desktop.run("stop"), 0)
        service = json.loads(self.path.with_suffix(".compose.json").read_text())["services"]["desktop"]
        grace = int(service["stop_grace_period"].removesuffix("s"))
        self.assertGreaterEqual(grace, 600)
        self.assertLess(int(service["environment"]["QEMU_TIMEOUT"]), grace)
        self.assertLess(grace, observed_timeout[0])
        self.assertEqual(service["environment"]["SHUTDOWN"], "Y")

    def test_concurrent_lifecycle_mutations_are_rejected(self):
        desktop = self.desktop()
        with self.path.with_suffix(".lock").open("w") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DesktopError, "in progress"):
                desktop.run("up")
        self.assertEqual(self.calls, [])

    def test_cleanup_waits_for_an_in_flight_guest_operation(self):
        desktop = self.desktop()
        self.existing(desktop)
        observed = []
        with self.path.with_suffix('.lock').open('w') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def complete_guest_operation():
                time.sleep(0.15)
                observed.append(any('compose' in call for call in self.calls))
                fcntl.flock(stream, fcntl.LOCK_UN)
            thread = threading.Thread(target=complete_guest_operation)
            thread.start()
            self.assertEqual(desktop.run('stop'), 0)
            thread.join(2)
        self.assertEqual(observed, [False])
        self.assertEqual(sum('compose' in call for call in self.calls), 1)

    def test_reserved_environment_and_port_injection_are_rejected(self):
        self.configuration["environment"]["PASSWORD"] = "plain-text-password"
        with self.assertRaises(DesktopError):
            self.desktop()
        del self.configuration["environment"]["PASSWORD"]
        self.configuration["ports"][0]["host"] = "0.0.0.0:22"
        with self.assertRaises(DesktopError):
            self.desktop()

    def test_guest_execution_checks_ownership_and_uses_stdin_without_shell_interpolation(self):
        desktop = self.desktop()
        self.existing(desktop)
        script = self.root / 'probe.ps1'
        script.write_text("Write-Output 'Grüße $literal'; exit 23")
        self.assertEqual(desktop.run('guest-exec', script=script, timeout=90), 0)
        call = self.calls[-1]
        self.assertEqual(call[1:3], ['exec', '--interactive'])
        self.assertIn('/usr/share/winhoo/guest-tools.py', call)
        self.assertNotIn('Grüße', ' '.join(call))
        self.record['Config']['Labels'][OWNER] = 'foreign-owner'
        before = len(self.calls)
        with self.assertRaises(DesktopError):
            desktop.run('guest-exec', script=script)
        self.assertFalse(any(c[1] == 'exec' for c in self.calls[before:]))

    def test_guest_file_reads_preserve_literal_paths_limits_and_ownership(self):
        desktop = self.desktop()
        self.existing(desktop)
        path = r'C:\logs\space ; $literal.log'
        self.assertEqual(desktop.run('guest-read', guest_path=path, max_bytes=4096), 0)
        call = self.calls[-1]
        self.assertIn('/usr/share/winhoo/guest-tools.py', call)
        self.assertEqual(call[-4:], ['--path', path, '--max-bytes', '4096'])
        self.assertNotIn('--interactive', call)
        before = len(self.calls)
        with self.assertRaises(DesktopError):
            desktop.run('guest-read', guest_path=path, max_bytes=1024 * 1024 + 1)
        self.assertFalse(any(c[1] == 'exec' for c in self.calls[before:]))
        self.record['Config']['Labels'][OWNER] = 'foreign-owner'
        before = len(self.calls)
        with self.assertRaises(DesktopError):
            desktop.run('guest-read', guest_path=path)
        self.assertFalse(any(c[1] == 'exec' for c in self.calls[before:]))

    def test_tools_repair_refuses_a_disk_used_by_any_running_container(self):
        desktop = self.desktop()
        self.existing(desktop)
        normal = desktop.runner
        def busy(args, **kwargs):
            if args[1] == 'ps':
                return subprocess.CompletedProcess(args, 0, 'other-running-container\n', '')
            return normal(args, **kwargs)
        desktop.runner = busy
        with self.assertRaisesRegex(DesktopError, 'Stop every container'):
            desktop.run('tools-repair')
        self.assertFalse(any(call[1] == 'run' for call in self.calls))

    def test_bind_parent_and_directly_mounted_disk_are_busy_at_any_destination(self):
        self.configuration['legacyBind'] = True
        desktop = self.desktop()
        for path in [self.root, self.root / 'storage' / 'windows.img']:
            with self.subTest(path=path):
                self.active_mounts['b' * 64] = [{'Type': 'bind', 'Source': str(path), 'Destination': '/other'}]
                with self.assertRaisesRegex(DesktopError, 'Stop every container'):
                    desktop.run('up')
        self.assertFalse(any(call[1] in ('compose', 'run') for call in self.calls))
        self.active_mounts['b' * 64][0]['Source'] = str(self.root / 'unrelated')
        self.assertEqual(desktop.run('up'), 0)

    def test_up_does_not_reconcile_or_attach_another_watchdog_to_a_running_guest(self):
        self.configuration['ownerGeneration'] = 'c' * 32
        desktop = self.desktop()
        self.existing(desktop)
        self.record['State']['Running'] = True
        self.project_members.append(self.record['Id'])
        self.assertEqual(desktop.run('up'), 0)
        self.assertFalse(any(call[1] in ('compose', 'exec', 'run') for call in self.calls))

    def test_renamed_or_other_project_members_cannot_be_stopped_or_adopted(self):
        desktop = self.desktop()
        self.project_members.append('b' * 64)
        for operation in ['up', 'down', 'stop', 'restart']:
            with self.subTest(operation=operation), self.assertRaisesRegex(DesktopError, 'another container'):
                desktop.run(operation)
        self.existing(desktop)
        self.project_members.append(self.record['Id'])
        with self.assertRaisesRegex(DesktopError, 'another container'):
            desktop.run('down')
        self.assertFalse(any(call[1] in ('compose', 'exec', 'run') for call in self.calls))

    def test_outer_exec_budget_allows_cold_transport_before_short_process_wait(self):
        desktop = self.desktop()
        self.existing(desktop)
        script = self.root / 'cold.ps1'
        script.write_text('Start-Sleep -Seconds 20')
        observed = []
        normal = desktop.runner
        def cold(args, **kwargs):
            if args[1] == 'exec':
                observed.append(kwargs['timeout'])
            return normal(args, **kwargs)
        desktop.runner = cold
        self.assertEqual(desktop.run('guest-exec', script=script, timeout=5), 0)
        self.assertGreater(observed[0], 3 * 60 + 5)

    def watchdog(self, initial_generation="a" * 32):
        heartbeat = self.root / "heartbeat"
        heartbeat.write_text(initial_generation + " 1\n")
        target = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: target.poll() is None and target.terminate())
        script = Path(__file__).resolve().parents[1] / "desktop" / "owner-watchdog.sh"
        watcher = subprocess.Popen(["bash", str(script), str(heartbeat), "a" * 32, "2", "0.05", str(target.pid)], stderr=subprocess.PIPE)
        self.addCleanup(lambda: watcher.poll() is None and watcher.terminate())
        self.addCleanup(watcher.stderr.close)
        return heartbeat, target, watcher

    def test_real_watchdog_terminates_its_target_when_owner_stops_renewing(self):
        _, target, watcher = self.watchdog()
        self.assertEqual(watcher.wait(timeout=4), 0)
        self.assertEqual(target.wait(timeout=1), -15)
        self.assertIn(b"heartbeat expired", watcher.stderr.read())

    def test_real_watchdog_stays_alive_while_heartbeat_changes(self):
        heartbeat, target, watcher = self.watchdog()
        deadline = time.monotonic() + 3.1
        tick = 1
        while time.monotonic() < deadline:
            tick += 1
            temporary = heartbeat.with_suffix(".tmp")
            temporary.write_text("a" * 32 + f" {tick}\n")
            temporary.replace(heartbeat)
            time.sleep(0.05)
        self.assertIsNone(target.poll())
        self.assertIsNone(watcher.poll())
        watcher.terminate()
        watcher.wait(timeout=1)
        target.terminate()
        target.wait(timeout=1)

    def test_real_watchdog_never_stops_a_new_owner(self):
        _, target, watcher = self.watchdog("b" * 32)
        self.assertEqual(watcher.wait(timeout=2), 0)
        self.assertIsNone(target.poll())
        target.terminate()
        target.wait(timeout=1)


if __name__ == "__main__":
    unittest.main()
