"""Container-local QGA transport and stopped-disk guest-tools preparation."""
from __future__ import annotations
import argparse
import base64
import fcntl
import hashlib
import json
import ntpath
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time

TOOLS = Path('/usr/share/winhoo/guest-tools')
SOCKET = '/run/winhoo-control/qga.sock'
VERSION = '0.4.0'
MAX_RESPONSE = 4 * 1024 * 1024


class ToolError(RuntimeError):
    pass


class Agent:
    def __init__(self, path=SOCKET, timeout=30):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(timeout)
        self.socket.connect(path)
        self.buffer = b''
        identity = secrets.randbits(52)
        self.socket.sendall(b'\xff' + json.dumps({'execute': 'guest-sync-delimited', 'arguments': {'id': identity}}).encode() + b'\n')
        while self.receive().get('return') != identity:
            pass

    def receive(self):
        while b'\n' not in self.buffer:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise ToolError('Guest agent disconnected')
            self.buffer += chunk
            if len(self.buffer) > MAX_RESPONSE:
                raise ToolError('Guest-agent response too large')
        line, self.buffer = self.buffer.split(b'\n', 1)
        line = line.rsplit(b'\xff', 1)[-1]
        try:
            return json.loads(line)
        except (ValueError, UnicodeError):
            return {}  # Ignore stale bytes until the unique sync response.

    def call(self, name, arguments=None):
        request = {'execute': name}
        if arguments is not None:
            request['arguments'] = arguments
        self.socket.sendall(json.dumps(request).encode() + b'\n')
        response = self.receive()
        if 'error' in response:
            raise ToolError(str(response['error']))
        if 'return' not in response:
            raise ToolError('Invalid guest-agent response')
        return response['return']

    def close(self):
        self.socket.close()


def read_file(agent, path, max_bytes):
    if not path or '\0' in path or not ntpath.isabs(path) or not ntpath.splitdrive(path)[0]:
        raise ToolError('Guest file path must be an absolute Windows path')
    if not 1 <= max_bytes <= 1024 * 1024:
        raise ToolError('Guest file read limit must be between 1 byte and 1 MiB')
    handle = agent.call('guest-file-open', {'path': path, 'mode': 'rb'})
    try:
        data = bytearray()
        while len(data) <= max_bytes:
            count = min(65536, max_bytes + 1 - len(data))
            value = agent.call('guest-file-read', {'handle': handle, 'count': count})
            chunk = base64.b64decode(value.get('buf-b64', ''), validate=True)
            if value.get('count') != len(chunk) or len(chunk) > count or not isinstance(value.get('eof'), bool):
                raise ToolError('Invalid guest file read response')
            data.extend(chunk)
            if value['eof']:
                break
            if not chunk:
                raise ToolError('Guest file read made no progress')
        result = bytes(data[:max_bytes])
        return {'path': path, 'bytes': len(result), 'truncated': len(data) > max_bytes,
                'dataBase64': base64.b64encode(result).decode('ascii')}
    finally:
        agent.call('guest-file-close', {'handle': handle})


def backup_disk(disk: Path, *, sparse=False):
    # Reserve a unique destination on the same filesystem; never overwrite a
    # previous recovery copy, even for two preparations within the same second.
    backup = disk.with_name(f'{disk.name}.winhoo-tools-{secrets.token_hex(8)}.backup')
    info = disk.stat()
    if sparse:
        gib = 1024 ** 3
        # Bound even the worst case if the filesystem materializes every hole.
        if info.st_size > 128 * gib or info.st_blocks * 512 > 64 * gib:
            raise ToolError('Stopped disk exceeds the sparse backup limit (128 GiB logical, 64 GiB allocated)')
        if shutil.disk_usage(disk.parent).free < info.st_size + 8 * gib:
            raise ToolError('Sparse backup requires the full logical disk size plus 8 GiB free')
    with os.fdopen(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as target:
        try:
            flags = ['--reflink=never', '--sparse=always'] if sparse else ['--reflink=always', '--sparse=auto']
            subprocess.run(['cp', *flags, '--', str(disk), str(backup)], check=True)
            if backup.stat().st_size != info.st_size:
                raise ToolError('Incomplete stopped disk backup')
            os.fsync(target.fileno())
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
    return backup


def oem_tools_installed(g, sources):
    """Accept a completed OEM install only when its actual files match the image."""
    directory = '/ProgramData/WinHoo/tools/'
    receipt = directory + 'installed.json'
    if not g.is_file(receipt) or g.filesize(receipt) > 4096:
        return False
    try:
        record = json.loads(g.read_file(receipt).decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        return False
    if (not isinstance(record, dict) or record.get('schema') != 'winhoo.guest-tools/1'
            or record.get('version') != VERSION or record.get('sha256') != sources.get('qemu-ga-x86_64.msi')):
        return False
    return all(g.is_file(directory + name) and g.checksum('sha256', directory + name) == expected
               for name, expected in sources.items())


def write_marker(marker, receipt):
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps(receipt) + '\n')
    temporary.replace(marker)


def prepare(disk: Path, *, force=False, sparse_backup=False):
    if not disk.exists() and disk.name == 'data.img' and disk.with_name('windows.img').is_file():
        disk = disk.with_name('windows.img')
    if not disk.is_file():
        return {'status': 'new-disk', 'message': 'Guest tools will install through the generic OEM bootstrap'}
    marker = disk.parent / '.winhoo-tools.json'
    payload = hashlib.sha256()
    sources = {}
    for source in sorted(TOOLS.rglob('*')):
        if source.is_file():
            payload.update(source.relative_to(TOOLS).as_posix().encode() + b'\0')
            digest = hashlib.sha256(source.read_bytes())
            sources[source.relative_to(TOOLS).as_posix()] = digest.hexdigest()
            payload.update(digest.digest())
    identity = {'version': VERSION, 'payloadSha256': payload.hexdigest(), 'device': disk.stat().st_dev, 'inode': disk.stat().st_ino, 'bytes': disk.stat().st_size}
    if marker.is_file() and not force:
        if json.loads(marker.read_text()).get('disk') == identity:
            return {'status': 'prepared', 'cached': True}
    # libguestfs boots its own Linux appliance with software emulation. It does
    # not need a privileged container, loop devices, FUSE or host disk mounts.
    import guestfs
    os.environ['LIBGUESTFS_BACKEND'] = 'direct'
    os.environ['LIBGUESTFS_BACKEND_SETTINGS'] = 'force_tcg'
    g = guestfs.GuestFS(python_return_dict=True)
    g.set_memsize(768)
    g.add_drive_opts(str(disk), format='raw', readonly=True)
    try:
        g.launch()
        # Identify the installed Windows filesystem directly. General OS
        # inspection can miss new Windows builds even when NTFS is readable.
        windows = []
        for partition, kind in g.list_filesystems().items():
            if kind != 'ntfs':
                continue
            try:
                g.mount_vfs('ro', 'ntfs-3g', partition, '/')
                system = g.case_sensitive_path('/Windows/System32/config/SYSTEM')
                software = g.case_sensitive_path('/Windows/System32/config/SOFTWARE')
                if g.is_file(system) and g.is_file(software):
                    windows.append(partition)
            except RuntimeError:
                pass
            finally:
                g.umount_all()
        if len(windows) != 1:
            raise ToolError('Expected exactly one installed Windows root; disk was not changed')
        root = windows[0]
        if not force:
            g.mount_vfs('ro', 'ntfs-3g', root, '/')
            try:
                if oem_tools_installed(g, sources):
                    write_marker(marker, {'disk': identity, 'method': 'verified-oem'})
                    return {'status': 'prepared', 'method': 'verified-oem'}
            finally:
                g.umount_all()
    finally:
        g.close()
    backup = backup_disk(disk, sparse=sparse_backup)
    g = guestfs.GuestFS(python_return_dict=True)
    g.set_memsize(768)
    g.add_drive_opts(str(disk), format='raw', readonly=False)
    try:
        g.launch()
        # ntfs-3g refuses a hibernated/unclean Windows volume. Do not add force,
        # remove_hiberfile or any filesystem-repair option here.
        g.mount_vfs('', 'ntfs-3g', root, '/')
        destination = '/ProgramData/WinHoo/tools'
        g.mkdir_p(destination)
        for source in TOOLS.rglob('*'):
            if source.is_file():
                relative = source.relative_to(TOOLS).as_posix()
                target = destination + '/' + relative
                g.mkdir_p(str(Path(target).parent))
                g.upload(str(source), target)
        startup = '/ProgramData/Microsoft/Windows/Start Menu/Programs/StartUp/WinHoo-GuestTools.cmd'
        if g.exists(startup):
            existing = g.read_file(startup)
            if b'WinHoo' not in existing:
                raise ToolError('Refusing to replace an unrelated startup file')
        g.mkdir_p(str(Path(startup).parent))
        g.write(startup, b'@echo off\r\npowershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\\ProgramData\\WinHoo\\tools\\Install-GuestTools.ps1 >>C:\\ProgramData\\WinHoo\\guest-tools.log 2>&1\r\n')
        g.sync()
        g.umount_all()
        g.shutdown()
    finally:
        g.close()
    receipt = {'disk': identity, 'backup': str(backup), 'preparedUtc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    write_marker(marker, receipt)
    return {'status': 'prepared', 'backup': str(backup), 'guestRoot': root}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'status', 'exec', 'process', 'read', 'time-sync', 'shutdown'])
    parser.add_argument('--disk', type=Path, default=Path('/storage/data.img'))
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--path')
    parser.add_argument('--max-bytes', type=int, default=65536)
    parser.add_argument('--sparse-backup', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 14400:
        parser.error('timeout must be between 1 and 14400 seconds')
    if args.operation == 'prepare':
        print(json.dumps(prepare(args.disk, force=args.force, sparse_backup=args.sparse_backup)))
        return 0
    # Exactly one client owns the serial protocol at a time, including polling.
    with open('/run/winhoo-control/client.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Creating PowerShell during an early TCG boot can exceed a short
        # process wait. Preserve the launch response/PID instead of timing out
        # the transport and losing track of a possibly running guest process.
        agent = Agent(timeout=60 if args.operation == 'exec' else min(args.timeout, 30))
        try:
            if args.operation == 'status':
                # QGA uses nanoseconds. Normalize before JSON reaches JavaScript,
                # where contemporary epoch nanoseconds exceed safe integers.
                result = {'agent': agent.call('guest-info'), 'os': agent.call('guest-get-osinfo'), 'timeUnixMs': agent.call('guest-get-time') // 1_000_000}
            elif args.operation == 'time-sync':
                result = agent.call('guest-set-time', {'time': time.time_ns()})
            elif args.operation == 'read':
                result = read_file(agent, args.path, args.max_bytes)
            elif args.operation == 'shutdown':
                # QGA shutdown has no response by protocol.
                agent.socket.sendall(b'{"execute":"guest-shutdown","arguments":{"mode":"powerdown"}}\n')
                result = {'requested': True}
            else:
                if args.operation == 'exec':
                    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise ToolError('Guest script exceeds 1 MiB')
                    script = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n" + raw.decode('utf-8')
                    encoded = base64.b64encode(script.encode('utf-16le')).decode()
                    pid = agent.call('guest-exec', {'path': r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe', 'arg': ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded], 'capture-output': True})['pid']
                else:
                    if args.pid is None or args.pid < 1:
                        raise ToolError('A positive process PID is required')
                    pid = args.pid
                deadline = time.monotonic() + args.timeout
                while True:
                    result = agent.call('guest-exec-status', {'pid': pid})
                    if result.get('exited'):
                        break
                    if time.monotonic() >= deadline:
                        receipt = {'schema': 'winhoo.guest-result/1', 'pid': pid, 'status': 'running', 'message': 'Process remains in guest; query this PID instead of launching twice'}
                        if args.operation == 'exec':
                            receipt['scriptSha256'] = hashlib.sha256(raw).hexdigest()
                        print(json.dumps(receipt))
                        return 124
                    time.sleep(1)
                result['pid'] = pid
                for key in ('out-data', 'err-data'):
                    if key in result:
                        result[key.replace('-data', '')] = base64.b64decode(result.pop(key)).decode('utf-8', errors='replace')
                if args.operation == 'exec':
                    result['scriptSha256'] = hashlib.sha256(raw).hexdigest()
            print(json.dumps({'schema': 'winhoo.guest-result/1', 'operation': args.operation, 'result': result}))
            if isinstance(result, dict) and result.get('exited'):
                return int(result.get('exitcode', 1))
            return 0
        finally:
            agent.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({'schema': 'winhoo.guest-result/1', 'status': 'failed', 'error': str(error)}))
        raise SystemExit(1)
