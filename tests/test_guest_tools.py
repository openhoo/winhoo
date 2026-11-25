"""Exercise the real serial client against fragmented/stale protocol replies."""
import importlib.util
import hashlib
import base64
import io
from contextlib import redirect_stdout
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch, mock_open
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('guest_tools', Path(__file__).resolve().parents[1] / 'desktop/guest-tools.py')
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


class GuestAgentTests(unittest.TestCase):
    def test_sparse_backup_refuses_low_space_before_modifying_disk(self):
        with tempfile.TemporaryDirectory() as root:
            disk = Path(root) / 'data.img'
            disk.write_bytes(b'Windows')
            with patch.object(tools.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
                with self.assertRaisesRegex(tools.ToolError, '8 GiB'):
                    tools.backup_disk(disk, sparse=True)
            self.assertEqual(list(disk.parent.iterdir()), [disk])
            self.assertEqual(disk.read_bytes(), b'Windows')

    def test_failed_copy_removes_only_its_partial_recovery_file(self):
        with tempfile.TemporaryDirectory() as root:
            disk = Path(root) / 'data.img'
            disk.write_bytes(b'original')
            prior = disk.with_suffix('.backup')
            prior.write_bytes(b'prior')
            with patch.object(tools.subprocess, 'run', side_effect=OSError('I/O error')):
                with self.assertRaises(OSError):
                    tools.backup_disk(disk)
            self.assertEqual(set(disk.parent.iterdir()), {disk, prior})
            self.assertEqual(disk.read_bytes(), b'original')

    def test_oem_receipt_requires_the_actual_installed_payload(self):
        sources = {'qemu-ga-x86_64.msi': 'a' * 64, 'RunHidden.cs': 'b' * 64}
        receipt = {'schema': 'winhoo.guest-tools/1', 'version': tools.VERSION, 'sha256': 'a' * 64}
        guest = SimpleNamespace(is_file=lambda path: True, filesize=lambda path: 150,
            read_file=lambda path: json.dumps(receipt).encode(),
            checksum=lambda algo, path: sources[Path(path).name])
        self.assertTrue(tools.oem_tools_installed(guest, sources))
        guest.checksum = lambda algo, path: 'c' * 64
        self.assertFalse(tools.oem_tools_installed(guest, sources))
        receipt['version'] = 'old'
        self.assertFalse(tools.oem_tools_installed(guest, sources))

    def test_readonly_file_handles_short_reads_and_explicit_truncation(self):
        calls = []
        class FileAgent:
            def call(self, name, arguments=None):
                calls.append((name, arguments))
                if name == 'guest-file-open':
                    self.parts = iter([b'first ', b'line\r\n', b'last'])
                    return 42
                if name == 'guest-file-close':
                    return {}
                data = next(self.parts)[:arguments['count']]
                return {'count': len(data), 'buf-b64': base64.b64encode(data).decode(), 'eof': data == b'last'}
        result = tools.read_file(FileAgent(), r'C:\logs\bootstrap.log', 12)
        self.assertEqual(base64.b64decode(result['dataBase64']), b'first line\r\n')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['bytes'], 12)
        self.assertEqual(calls[0], ('guest-file-open', {'path': r'C:\logs\bootstrap.log', 'mode': 'rb'}))
        self.assertEqual(calls[-1], ('guest-file-close', {'handle': 42}))
        self.assertFalse(any('exec' in name or 'write' in name for name, _ in calls))
        full = tools.read_file(FileAgent(), r'C:\logs\bootstrap.log', 100)
        self.assertEqual(base64.b64decode(full['dataBase64']), b'first line\r\nlast')
        self.assertFalse(full['truncated'])

    def test_readonly_file_closes_handle_after_invalid_agent_data(self):
        calls = []
        class BrokenFileAgent:
            def call(self, name, arguments=None):
                calls.append(name)
                if name == 'guest-file-open':
                    return 43
                if name == 'guest-file-read':
                    return {'count': 1000, 'buf-b64': base64.b64encode(b'short').decode(), 'eof': True}
                return {}
        with self.assertRaises(tools.ToolError):
            tools.read_file(BrokenFileAgent(), r'C:\logs\bootstrap.log', 32)
        self.assertEqual(calls[-1], 'guest-file-close')

    def test_short_process_wait_keeps_launch_transport_and_returns_pid(self):
        calls = []
        class SlowGuest:
            def call(self, name, arguments=None):
                calls.append(name)
                return {'pid': 4321} if name == 'guest-exec' else {'exited': False}
            def close(self):
                pass
        with patch.object(tools, 'Agent', return_value=SlowGuest()) as factory, patch('builtins.open', mock_open()), patch.object(tools.fcntl, 'flock'), patch.object(tools.sys, 'argv', ['guest-tools.py', 'exec', '--timeout', '2']), patch.object(tools.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'Write-Output example'))), patch.object(tools.time, 'monotonic', side_effect=[0, 3]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tools.main(), 124)
            receipt = json.loads(output.getvalue())
            self.assertEqual((receipt['status'], receipt['pid']), ('running', 4321))
            self.assertEqual(len(receipt['scriptSha256']), 64)
            self.assertEqual(calls, ['guest-exec', 'guest-exec-status'])
            self.assertGreaterEqual(factory.call_args.kwargs['timeout'], 60)

    def test_qga_time_units_and_json_safe_host_clock(self):
        epoch_ns = 1793852235888123456
        calls = []
        class ClockAgent:
            def call(self, name, arguments=None):
                calls.append((name, arguments))
                return epoch_ns if name == 'guest-get-time' else {}
            def close(self):
                pass
        with patch.object(tools, 'Agent', return_value=ClockAgent()), patch('builtins.open', mock_open()), patch.object(tools.fcntl, 'flock'):
            with patch.object(tools.sys, 'argv', ['guest-tools.py', 'status']), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(tools.main(), 0)
                result = json.loads(output.getvalue())['result']
                self.assertEqual(result['timeUnixMs'], 1793852235888)
                self.assertLess(result['timeUnixMs'], 2**53)
            with patch.object(tools.sys, 'argv', ['guest-tools.py', 'time-sync']), patch.object(tools.time, 'time_ns', return_value=epoch_ns), redirect_stdout(io.StringIO()):
                self.assertEqual(tools.main(), 0)
                self.assertEqual(calls[-1], ('guest-set-time', {'time': epoch_ns}))

    def exchange(self, answer):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / 'agent.sock')
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen()
        self.addCleanup(server.close)
        failures = []
        def peer():
            try:
                connection, _ = server.accept()
                with connection, connection.makefile('rb') as stream:
                    sync = json.loads(stream.readline().lstrip(b'\xff'))
                    connection.sendall(b'old partial JSON\n\xff' + json.dumps({'return': sync['arguments']['id']}).encode() + b'\n')
                    request = json.loads(stream.readline())
                    self.assertEqual(request['execute'], 'guest-get-osinfo')
                    data = json.dumps(answer).encode() + b'\n'
                    for byte in data:
                        connection.sendall(bytes([byte]))
            except Exception as error:
                failures.append(error)
        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        agent = tools.Agent(path, timeout=2)
        self.addCleanup(agent.close)
        return agent, thread, failures

    def test_stale_and_fragmented_serial_responses_are_synchronized(self):
        agent, thread, failures = self.exchange({'return': {'name': 'Windows', 'version': '11'}})
        self.assertEqual(agent.call('guest-get-osinfo'), {'name': 'Windows', 'version': '11'})
        thread.join(3)
        self.assertEqual(failures, [])

    def test_guest_agent_errors_fail_closed(self):
        agent, thread, failures = self.exchange({'error': {'class': 'CommandDisabled', 'desc': 'disabled'}})
        with self.assertRaisesRegex(tools.ToolError, 'CommandDisabled'):
            agent.call('guest-get-osinfo')
        thread.join(3)
        self.assertEqual(failures, [])

    def test_fresh_disk_uses_oem_without_offline_writes(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(tools.prepare(Path(root) / 'data.img')['status'], 'new-disk')
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_nonregular_and_dangling_disk_paths_cannot_be_treated_as_fresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            disk = Path(temporary) / 'data.img'
            disk.mkdir()
            with self.assertRaisesRegex(tools.ToolError, 'regular Windows disk'):
                tools.prepare(disk)
            disk.rmdir()
            disk.with_name('windows.img').symlink_to(Path(temporary) / 'missing')
            with self.assertRaisesRegex(tools.ToolError, 'regular Windows disk'):
                tools.prepare(disk)

    def test_foreign_startup_file_is_preserved_before_backup_or_writable_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disk = root / 'data.img'
            disk.write_bytes(b'unchanged')
            (root / '.winhoo-tools.json').write_text('{truncated')
            launcher = Path(__file__).resolve().parents[1] / 'desktop/WinHoo-GuestTools.cmd'
            script = launcher.read_text().replace('\n', '\r\n').encode('ascii')
            (root / launcher.name).write_bytes(script)
            foreign = script.replace(b'@echo off', b'@echo xyz')
            opened = []
            guest = SimpleNamespace(set_memsize=lambda n: None, add_drive_opts=lambda *a, **opts: opened.append(opts),
                launch=lambda: None, list_filesystems=lambda: {'/dev/sda1': 'ntfs'}, mount_vfs=lambda *args: None,
                case_sensitive_path=lambda p: p, is_file=lambda p: not p.endswith('installed.json'),
                exists=lambda p: p == tools.STARTUP, filesize=lambda p: len(foreign), read_file=lambda p: foreign,
                umount_all=lambda: None, close=lambda: None)
            with patch.object(tools, 'TOOLS', root), patch.dict('sys.modules', {'guestfs': SimpleNamespace(GuestFS=lambda **kw: guest)}):
                for force in [False, True]:
                    with self.assertRaisesRegex(tools.ToolError, 'unrelated startup file'):
                        tools.prepare(disk, force=force)
            self.assertTrue(all(opts.get('readonly') is True for opts in opened))
            self.assertEqual(len(opened), 2)
            self.assertEqual(disk.read_bytes(), b'unchanged')
            self.assertFalse(list(root.glob('*.backup')))

    def test_changed_payload_invalidates_prepared_disk_before_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disk = root / 'data.img'
            disk.write_bytes(b'fixture disk')
            payload = root / 'tools'
            payload.mkdir()
            source = payload / 'installer.ps1'
            source.write_text('first revision')
            digest = hashlib.sha256(b'installer.ps1\0' + hashlib.sha256(source.read_bytes()).digest()).hexdigest()
            identity = {'version': tools.VERSION, 'payloadSha256': digest,
                        'device': disk.stat().st_dev, 'inode': disk.stat().st_ino, 'bytes': disk.stat().st_size}
            (root / '.winhoo-tools.json').write_text(json.dumps({'disk': identity}))
            def probe(**kwargs):
                raise RuntimeError('read-only probe reached')
            with patch.object(tools, 'TOOLS', payload), patch.dict('sys.modules', {'guestfs': SimpleNamespace(GuestFS=probe)}):
                self.assertEqual(tools.prepare(disk), {'status': 'prepared', 'cached': True})
                source.write_text('corrected revision')
                with self.assertRaisesRegex(RuntimeError, 'read-only probe reached'):
                    tools.prepare(disk)
            self.assertEqual(disk.read_bytes(), b'fixture disk')


if __name__ == '__main__':
    unittest.main()
