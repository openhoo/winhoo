"""Local Windows desktop lifecycle on Apple Silicon. No container or remote host."""
import argparse
import base64
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid

from . import media


def private(path):
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise ValueError(f'Expected a private (0700), locally owned directory: {path}')


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def read_state(directory):
    private(directory)
    state = json.loads((directory / 'desktop.json').read_text())
    if state.get('schema') != 'winhoo.macos-desktop/1' or state.get('directory') != str(directory) or str(uuid.UUID(state['uuid'])) != state['uuid']:
        raise ValueError('Desktop ownership mismatch')
    return state


def owned_receipt(directory, name, field, state):
    try:
        data = json.loads((directory / name).read_text())
    except FileNotFoundError:
        return False
    if not isinstance(data, dict) or data.get(field) != state['uuid']:
        raise ValueError(f'Unexpected owner in {name}')
    return True


@contextmanager
def lifecycle(directory, *, shared=False, filename='lifecycle.lock'):
    import fcntl
    with (directory / filename).open('a') as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another desktop lifecycle operation owns this instance') from None
        yield


@contextmanager
def operation_lock(directory, op):
    # A long test may write guest state; allow observation, but never another
    # command, upload or power transition on top of it.
    with lifecycle(directory,shared=op in ('exec','put','get','inspect')), (
        lifecycle(directory,filename='guest-operation.lock') if op in ('exec','put') else nullcontext()
    ):
        yield


def doctor():
    if (platform.system(), platform.machine()) != ('Darwin', 'arm64'):
        raise ValueError('This desktop backend requires Apple Silicon macOS')
    bins = ('qemu-system-aarch64', 'qemu-img', 'swtpm', 'xorriso', 'wimlib-imagex', 'ssh', 'ssh-keygen', 'scp', 'mformat', 'mcopy', 'mtype', 'curl')
    paths = {b: shutil.which(b) for b in bins}
    missing = [k for k,v in paths.items() if not v]
    if missing:
        raise ValueError('Missing host tools: ' + ', '.join(missing))
    if 'hvf' not in subprocess.check_output([paths[bins[0]], '-accel', 'help'], text=True).split():
        raise ValueError('QEMU does not support Hypervisor.framework')
    firmware = Path(paths[bins[0]]).resolve().parent.parent / 'share/qemu'
    for name in ('edk2-aarch64-code.fd', 'edk2-arm-vars.fd'):
        if not (firmware / name).is_file():
            raise ValueError(f'Missing UEFI firmware: {name}')
    return {'backend': 'hvf', 'host': 'macOS arm64', 'firmware': str(firmware)}


def forward_ports(pairs, ssh):
    result = [(ssh, 22)]
    for item in pairs:
        if not re.fullmatch(r'[0-9]+:[0-9]+', item):
            raise ValueError('A forward must be HOSTPORT:GUESTPORT')
        result.append(tuple(map(int, item.split(':'))))
    if any(not (1024 <= h <= 65535 and 1 <= g <= 65535) for h,g in result) or len({h for h,_ in result}) != len(result):
        raise ValueError('Invalid or duplicate host port')
    return result


def create(d, args):
    firmware = Path(doctor()['firmware'])
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,18}', args.account):
        raise ValueError('Invalid local account name')
    ports = forward_ports(args.forward, args.ssh_port)
    if len(os.fsencode(d / 'qmp.sock')) >= 103 or ',' in str(d):
        raise ValueError('Use a shorter private VM directory without commas for the QEMU sockets')
    if shutil.disk_usage(d.parent).free < 40 * 1024**3:
        raise ValueError('The local VM needs at least 40 GiB free')
    d.mkdir(mode=0o700)  # Never adopt or overwrite a preexisting disk.
    state = {'schema': 'winhoo.macos-desktop/1', 'directory': str(d), 'uuid': str(uuid.uuid4()), 'account': args.account,
             'sshPort': args.ssh_port, 'ports': ports}
    media.installer(d, args.setup.resolve(), args.openssh.resolve(), args.netkvm.resolve(), state)
    shutil.copyfile(firmware / 'edk2-arm-vars.fd', d / 'vars.fd')
    (d / 'tpm').mkdir()
    media.run('qemu-img', 'create', '-f', 'qcow2', d / 'windows.qcow2', '80G')
    save(d / 'desktop.json', state)
    return {'created': state['uuid'], 'directory': str(d)}


class Monitor:
    def __init__(self, d, state):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(4)
        try:
            self.sock.connect(str(d / 'qmp.sock'))
            self.stream = self.sock.makefile('rwb', buffering=0)
            if 'QMP' not in self.receive():
                raise ValueError('Invalid QEMU greeting')
            self.send('qmp_capabilities')
            if self.send('query-uuid')['UUID'].lower() != state['uuid']:
                raise ValueError('Unexpected VM on the monitor socket')
        except BaseException:
            self.close()
            raise

    def close(self):
        if hasattr(self, 'stream'):
            self.stream.close()
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def receive(self):
        line = self.stream.readline(1024 * 1024)
        if not line:
            raise ConnectionError('QEMU monitor disconnected')
        return json.loads(line)

    def send(self, command, **kwargs):
        tag = uuid.uuid4().hex
        self.stream.write((json.dumps({'execute': command, 'arguments': kwargs, 'id': tag}) + '\n').encode())
        while True:
            msg = self.receive()
            if msg.get('id') != tag:
                continue
            if 'error' in msg:
                raise ValueError(f'{command}: {msg["error"]}')
            return msg['return']


class MonitorUnavailable(ValueError):
    pass


def process_id(directory):
    try:
        number = int((directory / 'qemu.pid').read_text())
    except FileNotFoundError:
        return None
    if number <= 1:
        raise ValueError('Invalid QEMU process id')
    return number


def alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def current(d, state):
    try:
        with Monitor(d, state) as q:
            return q.send('query-status')['status']
    except (FileNotFoundError, ConnectionError):
        # A live pid with no monitor is indeterminate; never start a second guest.
        if alive(process_id(d)):
            raise MonitorUnavailable('Guest process exists but its monitor is unavailable')
        return 'off'


class ReceiptUnavailable(ValueError):
    pass


def phase(d, state):
    def read(name):
        r = subprocess.run(['mtype', '-i', str(d/'phases.img'), '::'+name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4)
        return r.stdout.decode('ascii', errors='replace').strip() if r.returncode == 0 else ''
    owner = read('WHSTATE.TAG')
    if not owner:
        raise ReceiptUnavailable('Installation receipt disk is temporarily unreadable')
    if owner != state['uuid']:
        raise ValueError('Installation receipt disk has an unexpected owner')
    return read('phase.txt') or 'waiting-for-winpe'


def up(d, state):
    running = current(d, state)
    if running != 'off':
        return {'state': running, 'uuid': state['uuid']}
    firmware = Path(doctor()['firmware'])
    for h,_ in state['ports']:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', h))
    # swtpm terminates when the owning QEMU disconnects. Its state stays with this disk.
    tpm = subprocess.Popen(['swtpm', 'socket', '--tpm2', '--tpmstate', 'dir=tpm', '--ctrl', 'type=unixio,path=tpm.sock',
                            '--terminate', '--log', 'file=tpm.log'], cwd=d, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        until = time.monotonic() + 5
        while not (d / 'tpm.sock').exists():
            if tpm.poll() is not None or time.monotonic() > until:
                raise ValueError('Local TPM did not start')
            time.sleep(0.1)
        net = 'user,id=n,' + ','.join(f'hostfwd=tcp:127.0.0.1:{h}-:{g}' for h,g in state['ports'])
        cmd = ['qemu-system-aarch64', '-machine', 'virt,accel=hvf,gic-version=3', '-cpu', 'host', '-smp', '4', '-m', '6G',
               '-uuid', state['uuid'], '-name', 'WinHoo-' + state['uuid'], '-nodefaults', '-no-shutdown', '-daemonize',
               '-pidfile', 'qemu.pid', '-qmp', 'unix:qmp.sock,server=on,wait=off', '-display', 'none',
               '-drive', f'if=pflash,format=raw,readonly=on,file={firmware}/edk2-aarch64-code.fd',
               '-drive', 'if=pflash,format=raw,file=vars.fd', '-device', 'ramfb', '-device', 'qemu-xhci,id=xhci',
               '-device', 'usb-kbd', '-device', 'usb-tablet',
               # Expose a Windows playback endpoint without using the host speakers or microphone.
               '-audiodev', 'none,id=silent', '-device', 'usb-audio,audiodev=silent',
               '-drive', 'if=none,id=os,format=qcow2,file=windows.qcow2',
               '-device', 'nvme,drive=os,serial=WinHooDesktop,bootindex=1', '-netdev', net, '-device', 'virtio-net-pci,netdev=n',
               '-chardev', 'socket,id=tp,path=tpm.sock', '-tpmdev', 'emulator,id=tpm,chardev=tp', '-device', 'tpm-tis-device,tpmdev=tpm,ppi=off',
               '-serial', 'file:serial.log', '-rtc', 'base=utc,clock=host']
        stage = phase(d, state)
        if stage == 'failed':
            raise ValueError('WinPE deployment failed; inspect the retained phase image')
        if not owned_receipt(d, 'installed.json', 'owner', state) and stage != 'image-applied':
            cmd += ['-drive', 'if=none,id=setup,media=cdrom,readonly=on,format=raw,file=installer.iso',
                    '-device', 'usb-storage,drive=setup,bootindex=2',
                    '-drive', 'if=none,id=receipt,format=raw,file=phases.img,cache=directsync',
                    '-device', 'usb-storage,drive=receipt,removable=on']
        with (d / 'qemu.log').open('ab') as log:
            subprocess.run(cmd, cwd=d, stdin=subprocess.DEVNULL, stdout=log, stderr=log, check=True, timeout=25)
    except BaseException:
        if tpm.poll() is None:
            tpm.terminate()
        raise
    return {'state': current(d, state), 'uuid': state['uuid'], 'backend': 'hvf'}


def ssh_args(d, s):
    return ['-F', '/dev/null', '-i', str(d / 'id_ed25519'), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={d}/known_hosts', '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', f'HostKeyAlias=winhoo-{s["uuid"]}', '-o', 'ConnectTimeout=3', '-o', 'ConnectionAttempts=1',
            '-o', f'Port={s["sshPort"]}']


def guest(d, s, script, *, timeout, capture):
    bootstrap = ("$u=New-Object System.Text.UTF8Encoding $false;[Console]::InputEncoding=$u;"
                 "$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=$u;$OutputEncoding=$u;$ErrorActionPreference='Stop';"
                 "try { & ([ScriptBlock]::Create([Console]::In.ReadToEnd())) } catch { "
                 "[Console]::Error.WriteLine($_.Exception.Message);exit 1 }")
    remote = 'powershell.exe -NoLogo -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand ' + base64.b64encode(bootstrap.encode('utf-16le')).decode()
    return subprocess.run(['ssh', *ssh_args(d, s), s['account']+'@127.0.0.1', remote], capture_output=capture,
                          encoding='utf-8', errors='replace', input=script, timeout=timeout)


ATTEST = r"""$ErrorActionPreference='Stop'
$o=Get-Content C:\ProgramData\WinHoo\owner.json -Raw | ConvertFrom-Json
$c=Get-CimInstance Win32_ComputerSystem
@{owner=$o.uuid; account=$env:USERNAME; console=$c.UserName; build=[Environment]::OSVersion.Version.Build; arch=$env:PROCESSOR_ARCHITECTURE} | ConvertTo-Json -Compress"""


def inspect_guest(d, s, timeout):
    # Collect only metadata for the desktop and its declared TCP listeners.
    ports = ','.join(str(int(guest_port)) for _, guest_port in s['ports'])
    script = r"""
$owner = Get-Content C:\ProgramData\WinHoo\owner.json -Raw | ConvertFrom-Json
$sys = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$audio = @(Get-PnpDevice -PresentOnly -Class AudioEndpoint -ErrorAction SilentlyContinue | Select-Object Status,FriendlyName)
$listeners = @(Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in @(PORTS) } | Select-Object LocalAddress,LocalPort,OwningProcess)
$programs = @($listeners | ForEach-Object { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).Path } | Where-Object { $_ })
$profiles = @(Get-NetConnectionProfile | Select-Object InterfaceAlias,@{n='category';e={[string]$_.NetworkCategory}})
$policies = @(Get-ExecutionPolicy -List | Select-Object @{n='scope';e={[string]$_.Scope}},@{n='policy';e={[string]$_.ExecutionPolicy}})
$blocks = @(Get-NetFirewallRule -PolicyStore ActiveStore -Enabled True -Direction Inbound -Action Block | ForEach-Object {
    $r=$_; $p=($r | Get-NetFirewallApplicationFilter).Program
    if ($p -and $p -in $programs) { @{program=$p;profile=[string]$r.Profile;rule=$r.DisplayName} }
})
@{owner=$owner.uuid;account=$env:USERNAME;console=$sys.UserName;build=$os.BuildNumber;arch=$env:PROCESSOR_ARCHITECTURE;
  freeMemoryMiB=[int]($os.FreePhysicalMemory/1024);audio=$audio;listeners=$listeners;
  networkProfiles=$profiles;inboundProgramBlocks=$blocks;
  scriptPolicy=[string](Get-ExecutionPolicy);scriptPolicyScopes=$policies} | ConvertTo-Json -Depth 4 -Compress
""".replace('PORTS', ports)
    result = guest(d,s,script,timeout=timeout,capture=True)
    if result.returncode:
        raise ValueError('Could not collect guest diagnostics: ' + transport_reason(result.stderr))
    info = json.loads(result.stdout)
    if info.get('owner') != s['uuid'] or info.get('account','').lower() != s['account'].lower():
        raise ValueError('Authenticated diagnostic owner mismatch')
    return info


def transport_reason(error):
    msg = error.lower()
    for fragment, reason in [('closed by remote host','SSH connection closed by the guest'),
                             ('connection reset','SSH connection reset'), ('timed out','SSH connection timed out'),
                             ('connection refused','SSH is not listening')]:
        if fragment in msg:
            return reason
    return 'SSH command did not complete'


def wait(d, s, timeout):
    until = time.monotonic() + timeout
    last = 'Waiting for authenticated guest control'
    while time.monotonic() < until:
        running = current(d, s)
        try:
            stage = phase(d, s)
        except ReceiptUnavailable:
            if running != 'running':
                raise
            time.sleep(.5)
            continue
        if stage == 'failed':
            raise ValueError('WinPE failed; read the retained phase logs')
        if running in ('off', 'shutdown') and stage == 'image-applied' and not owned_receipt(d, 'handover.json', 'uuid', s):
            if running == 'shutdown':
                down(d, s, 15)
            save(d/'handover.json', {'uuid':s['uuid'], 'phase':stage})
            up(d,s)
            continue
        if running != 'running':
            raise ValueError(f'Guest is {running}; installation phase: {stage}')
        if stage != 'image-applied':
            time.sleep(1)
            continue
        try:
            result = guest(d, s, ATTEST, timeout=min(12, max(.1, until-time.monotonic())), capture=True)
        except subprocess.TimeoutExpired:
            result = None
            last = 'Read-only guest attestation timed out'
        if result and not result.returncode:
            info = json.loads(result.stdout.strip())
            if info.get('owner') != s['uuid'] or info.get('account', '').lower() != s['account'].lower():
                raise ValueError('Authenticated guest identity mismatch')
            if (info.get('console') or '').split('\\')[-1].lower() == s['account'].lower() and info.get('arch') == 'ARM64':
                save(d / 'installed.json', info)
                return info
            last = 'Windows is reachable; the dedicated account has no active console'
        elif result and ('HOST IDENTIFICATION HAS CHANGED' in result.stderr or 'Permission denied' in result.stderr):
            raise ValueError('Guest key or login rejected; inspect this owned instance')
        elif result:
            last = transport_reason(result.stderr)
        time.sleep(min(2, max(0, until-time.monotonic())))
    raise ValueError(last + '; see status, screenshot, and the guest installation log')


def down(d, s, timeout):
    if current(d, s) == 'off':
        return {'state': 'off', 'shutdown': 'already off'}
    with Monitor(d, s) as q:
        pid = process_id(d)
        if pid is None:
            raise ValueError('Cannot confirm the monitored QEMU process id')
        if q.send('query-status')['status'] != 'shutdown':
            q.send('system_powerdown')
        end = time.monotonic() + timeout
        while q.send('query-status')['status'] != 'shutdown':
            if time.monotonic() >= end:
                raise ValueError('Guest shutdown unconfirmed; disk and process retained')
            time.sleep(.5)
        try:
            q.send('quit')
        except (ConnectionError, BrokenPipeError):
            pass  # QEMU can close QMP before acknowledging quit; confirm exit below.
    while True:
        try:
            if current(d, s) == 'off' and not alive(pid):
                break
        except MonitorUnavailable:
            pass
        if time.monotonic() >= end:
            raise ValueError('Guest shut down but QEMU exit was not confirmed')
        time.sleep(.25)
    info = {'state': 'off', 'uuid': s['uuid'], 'shutdown': 'guest-confirmed', 'timeUnix': time.time()}
    save(d / 'shutdown.json', info)
    return info


def arguments(parser):
    commands = parser.add_subparsers(dest='desktop_op', required=True)
    commands.add_parser('doctor')
    commands.add_parser('sources', help='Fetch or verify the pinned Windows media, OpenSSH and network driver').add_argument('cache',type=Path)
    p = commands.add_parser('media')
    p.add_argument('--esd', type=Path, required=True)
    p.add_argument('--sha256', required=True)
    p.add_argument('--output', type=Path, required=True)
    for op in ('create','up','wait','status','inspect','screenshot','exec','put','get','down'):
        p = commands.add_parser(op)
        p.add_argument('directory', type=Path, help='Private retained VM directory')
        if op == 'create':
            for flag in ('setup','openssh','netkvm'):
                p.add_argument('--'+flag, type=Path, required=True)
            p.add_argument('--account', default='WinHooTest')
            p.add_argument('--ssh-port', type=int, default=2226)
            p.add_argument('--forward', action='append', default=[], metavar='HOST:GUEST')
        if op in ('exec','put','get','wait','inspect','down'):
            p.add_argument('--timeout', type=int, default=900 if op=='wait' else 120)
        if op == 'exec':
            src = p.add_mutually_exclusive_group(required=True)
            src.add_argument('--file', type=Path)
            src.add_argument('--stdin', action='store_true')
        if op == 'put':
            p.add_argument('source', type=Path)
            p.add_argument('basename', help='Filename inside the dedicated user profile')
        if op == 'get':
            p.add_argument('basename', help='Regular file inside the dedicated user profile')
            p.add_argument('destination', type=Path, help='New local file; never overwrites evidence')


def dispatch(a):
    os.umask(0o077)
    op = a.desktop_op
    if op == 'doctor':
        return doctor()
    if op == 'sources':
        return media.sources(a.cache.expanduser().resolve())
    if op == 'media':
        return media.prepare(a.esd, a.sha256, a.output)
    d = a.directory.expanduser().absolute()
    if d != d.resolve():
        raise ValueError('Use a canonical VM directory without symlinks')
    if op == 'create':
        return create(d, a)
    if op == 'status':
        s = read_state(d)
        state = current(d,s)
        try:
            stage = phase(d,s)
        except ReceiptUnavailable:
            if state != 'running':
                raise
            stage = 'receipt-unavailable'
        return {'uuid': s['uuid'], 'state': state, 'installed': owned_receipt(d,'installed.json','owner',s),
                'deployment': stage, 'forwards': s['ports']}
    if op == 'screenshot':
        s = read_state(d)
        with Monitor(d, s) as q:
            q.send('screendump', filename=str(d / 'screen.ppm'))
        return {'path': str(d / 'screen.ppm')}
    with operation_lock(d,op):
        s = read_state(d)
        if op == 'up':
            return up(d,s)
        if op == 'down':
            return down(d,s,a.timeout)
        if op == 'wait':
            return wait(d,s,a.timeout)
        if current(d,s) != 'running' or not owned_receipt(d,'installed.json','owner',s):
            raise ValueError('Run desktop wait to attest this instance first')
        if op == 'inspect':
            return inspect_guest(d,s,a.timeout)
        if op == 'exec':
            return guest(d,s,sys.stdin.read() if a.stdin else a.file.read_text(),timeout=a.timeout,capture=False).returncode
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', a.basename):
            raise ValueError('The guest destination must be a plain filename')
        if op == 'get':
            get_file(d,s,a.basename,a.destination,a.timeout)
            return {'path':str(a.destination.resolve())}
        return subprocess.run(['scp', *ssh_args(d,s), str(a.source.resolve()), f"{s['account']}@127.0.0.1:{a.basename}"],
                              timeout=a.timeout, stdin=subprocess.DEVNULL).returncode


def get_file(d, state, name, destination, timeout):
    import tempfile
    if destination.exists() or destination.is_symlink():
        raise ValueError('Refusing to replace an existing local file')
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        result = Path(staging)/'download'
        subprocess.run(['scp', *ssh_args(d,state), f"{state['account']}@127.0.0.1:{name}", str(result)],
                       stdin=subprocess.DEVNULL, check=True, timeout=timeout)
        if not stat.S_ISREG(result.lstat().st_mode):
            raise ValueError('Expected a regular file from the guest')
        result.chmod(0o600)
        os.link(result,destination)  # Atomic publication, including a concurrent writer.
