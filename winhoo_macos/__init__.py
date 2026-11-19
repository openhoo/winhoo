"""Local Windows desktop lifecycle on Apple Silicon. No container or remote host."""
import argparse
import base64
from contextlib import contextmanager, nullcontext
import json
import hashlib
import ipaddress
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
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


def controller_identity(launcher, package, expected=None):
    """Identify the host code and bundled Windows assets used by this desktop CLI."""
    if expected is not None and not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('Expected a lowercase SHA-256 fingerprint')
    # Python modules plus every asset type distributed by pyproject.toml.
    suffixes = {'.py', '.xml', '.ps1', '.cmd', '.ini', '.cs'}
    paths = {'winhoo.py': launcher}
    paths.update((f'winhoo_macos/{p.name}', p) for p in package.iterdir()
                 if p.suffix in suffixes and p.is_file())
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sorted(paths.items())}
    payload = json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode('ascii')
    digest = hashlib.sha256(payload).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError(f'Desktop controller {digest} does not match expected {expected}')
    return {'schema': 'winhoo.desktop-controller/1', 'sha256': digest, 'files': hashes}


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


def archive_shutdown(directory, state):
    if owned_receipt(directory,'shutdown.json','uuid',state):
        history=directory/'shutdown-history'
        history.mkdir(mode=0o700,exist_ok=True)
        private(history)
        (directory/'shutdown.json').rename(history/(uuid.uuid4().hex+'.json'))


@contextmanager
def lifecycle(directory, *, shared=False, filename='lifecycle.lock'):
    import fcntl
    with (directory / filename).open('a') as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another desktop lifecycle operation owns this instance') from None
        yield


def observed_lock(path):
    """Inspect an existing flock on Darwin without taking a competing lock."""
    if sys.platform != 'darwin':
        return 'unavailable'
    import fcntl
    # Darwin <sys/fcntl.h>: off_t start, off_t len, pid_t pid, short type/whence.
    layout = struct.Struct('@qqihh')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return 'free'
    except OSError:
        return 'unavailable'
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return 'unavailable'
        # Query a hypothetical writer: it conflicts with both shared and
        # exclusive locks. F_GETLK never acquires it, even on an idle VM.
        request = layout.pack(0, 0, 0, fcntl.F_WRLCK, os.SEEK_SET)
        reply = fcntl.fcntl(fd, fcntl.F_GETLK, request)
        kind = layout.unpack(reply)[3]
        return {fcntl.F_UNLCK: 'free', fcntl.F_RDLCK: 'shared',
                fcntl.F_WRLCK: 'exclusive'}.get(kind, 'unavailable')
    except OSError:
        return 'unavailable'
    finally:
        os.close(fd)


def controller_locks(directory):
    return {'lifecycle': observed_lock(directory / 'lifecycle.lock'),
            'guestOperation': observed_lock(directory / 'guest-operation.lock')}


@contextmanager
def operation_lock(directory, op):
    # A long test may write guest state; allow observation, but never another
    # command, upload or power transition on top of it.
    with lifecycle(directory,shared=op in ('exec','put','archive','get','inspect','foreground')), (
        lifecycle(directory,filename='guest-operation.lock') if op in ('exec','put','archive') else nullcontext()
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
    return {'backend': 'hvf', 'host': 'macOS arm64', 'firmware': str(firmware), 'lid': lid_state()}


def lid_state():
    try:
        result = subprocess.run(['/usr/sbin/ioreg','-r','-k','AppleClamshellState','-d','1'],
                                capture_output=True,text=True,timeout=3,check=True)
        match = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)\b',result.stdout)
        if match:
            return 'closed' if match[1] == 'Yes' else 'open'
    except (OSError, subprocess.SubprocessError):
        pass
    return 'unavailable'


def guard_idle_sleep(pid):
    if not pid or pid <= 1:
        raise ValueError('Cannot associate idle-sleep assertion with a VM process')
    # macOS releases this assertion when this particular process exits. No
    # display assertion, global power setting, or lid override is needed.
    return subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(pid)],stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)


def forward_ports(pairs, ssh):
    result = [(ssh, 22)]
    for item in pairs:
        if not re.fullmatch(r'[0-9]+:[0-9]+', item):
            raise ValueError('A forward must be HOSTPORT:GUESTPORT')
        result.append(tuple(map(int, item.split(':'))))
    if any(not (1024 <= h <= 65535 and 1 <= g <= 65535) for h,g in result) or len({h for h,_ in result}) != len(result):
        raise ValueError('Invalid or duplicate host port')
    return result


def set_forwards(d, state, pairs):
    if current(d,state) != 'off':
        raise ValueError('Shut down this desktop before changing its port forwards')
    state = {**state, 'ports': forward_ports(pairs,state['sshPort'])}
    save(d/'desktop.json',state)
    return {'uuid':state['uuid'],'forwards':state['ports']}


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
             'sshPort': args.ssh_port, 'ports': ports, 'allowIdleSleep': args.allow_idle_sleep}
    media.installer(d, args.setup.resolve(), args.openssh.resolve(), args.netkvm.resolve(), state)
    shutil.copyfile(firmware / 'edk2-arm-vars.fd', d / 'vars.fd')
    (d / 'tpm').mkdir()
    media.run('qemu-img', 'create', '-f', 'qcow2', d / 'windows.qcow2', '80G')
    save(d / 'desktop.json', state)
    return {'created': state['uuid'], 'directory': str(d)}


class Monitor:
    def __init__(self, d, state):
        self.shutdown_event = None
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
        message = json.loads(line)
        if message.get('event') == 'SHUTDOWN':
            self.shutdown_event = message.get('data')
        return message

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
        # After any new boot a previous clean stop no longer proves how this
        # generation ended. Retain the old proof rather than accepting stale data.
        archive_shutdown(d,state)
        with (d / 'qemu.log').open('ab') as log:
            subprocess.run(cmd, cwd=d, stdin=subprocess.DEVNULL, stdout=log, stderr=log, check=True, timeout=25)
    except BaseException:
        if tpm.poll() is None:
            tpm.terminate()
        raise
    result = {'state': current(d, state), 'uuid': state['uuid'], 'backend': 'hvf'}
    if result['state'] == 'running' and not state.get('allowIdleSleep',False):
        try:
            guard = guard_idle_sleep(process_id(d))
            result['idleSleepGuardPid'] = guard.pid
        except (OSError, ValueError) as exc:
            # QEMU has already started; preserve its state and tell the caller.
            result['idleSleepGuardError'] = str(exc)
    return result


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


CONSOLE = Path(__file__).with_name('console-state.ps1').read_text(encoding='utf-8') + '\n'
IDENTITY = r"""$ErrorActionPreference='Stop'
$o=Get-Content C:\ProgramData\WinHoo\owner.json -Raw | ConvertFrom-Json
$who=@{owner=$o.uuid; account=$env:USERNAME; computer=$env:COMPUTERNAME; build=[Environment]::OSVersion.Version.Build; arch=$env:PROCESSOR_ARCHITECTURE}
"""
ATTEST = CONSOLE + IDENTITY + r"""$console=Get-WinHooConsole
$who.console=$console.account; $who.consoleSession=$console
$who | ConvertTo-Json -Compress"""
ATTEST_TRANSPORT = IDENTITY + '$who | ConvertTo-Json -Compress'


def foreground(directory, state, expected_pid=None, *, activate=False, started_ticks=None, timeout=90):
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise ValueError('Foreground timeout must be between 1 and 300 seconds')
    if activate and expected_pid is None:
        raise ValueError('Window activation requires the expected process ID')
    if expected_pid is not None and not 0 < expected_pid <= 0x7fffffff:
        raise ValueError('Expected a positive Windows process ID')
    if started_ticks is not None and (not activate or not 0 < started_ticks <= 3155378975999999999):
        raise ValueError('Expected Windows process start time in UTC DateTime ticks')
    request = {'owner': state['uuid'], 'account': state['account'], 'id': uuid.uuid4().hex,
               'mode': 'focus' if activate else 'observe', 'pid': expected_pid, 'startedTicks': started_ticks,
               'probeSource': Path(__file__).with_name('focus-caption.cs' if activate else 'foreground.cs').read_text(encoding='utf-8')}
    encoded = base64.b64encode(json.dumps(request).encode()).decode('ascii')
    script = CONSOLE + IDENTITY + (
        "$request = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "')) | ConvertFrom-Json\n"
    ) + Path(__file__).with_name('foreground-sample.ps1').read_text(encoding='utf-8') + '\n' + Path(__file__).with_name('foreground.ps1').read_text(encoding='utf-8')
    try:
        result = guest(directory, state, script, timeout=timeout, capture=True)
    except subprocess.TimeoutExpired as error:
        partial = error.stderr or b''
        if isinstance(partial, bytes):
            partial = partial.decode('utf-8', errors='replace')
        # Report only known phases from this request. A partial receipt, even
        # during cleanup, cannot confirm success or authorize another click.
        marker = 'winhoo-foreground:' + request['id'] + ':'
        phases = [line[len(marker):] for line in partial.splitlines() if line.startswith(marker)
                  and line[len(marker):] in ('console','compile','register','dispatch','await-sample','verify','cleanup')]
        last = phases[-1] if phases else 'transport or guest startup'
        raise ValueError(f'Foreground observation timed out after {timeout} seconds (last phase: {last}); no window was confirmed') from None
    if result.returncode:
        lines = [line.strip() for line in result.stderr.splitlines() if line.strip() and not line.startswith(('#< CLIXML', '<Objs ', 'winhoo-foreground:'))]
        raise ValueError('Could not observe the foreground: ' + next(iter(lines), 'no diagnostic')[:300])
    info = json.loads(result.stdout)
    if (info.get('owner') != state['uuid'] or info.get('request') != request['id']
            or (info.get('account') or '').casefold() != state['account'].casefold()
            or console_state(info, state['account']) != 'ready'
            or info['consoleSession'].get('account') != info.get('console')
            or type(info.get('session')) is not int or info['session'] != info['consoleSession']['id']
            or type(info.get('pid')) is not int or info['pid'] <= 0 or not isinstance(info.get('process'), str) or not info['process']):
        raise ValueError('Foreground sample does not belong to this request and its unlocked console')
    if expected_pid is not None and info['pid'] != expected_pid:
        raise ValueError(f"Foreground is {info['process']} (PID {info['pid']}); expected PID {expected_pid}")
    if activate and info.get('method') not in ('already-active', 'caption-click'):
        raise ValueError('Windows did not confirm activation of the selected process')
    return info


def check_loopback(listeners, ports):
    for port in sorted(set(ports)):
        addresses = [ipaddress.ip_address(item['LocalAddress']) for item in listeners if item['LocalPort'] == port]
        if not addresses:
            raise ValueError(f'Expected a guest loopback listener on TCP {port}; no listener found')
        if not all(address.is_loopback for address in addresses):
            raise ValueError(f'Guest TCP {port} is also listening outside loopback')


def inspect_guest(d, s, timeout, required=()):
    if len(required)>16 or any(not 1 <= port <= 65535 for port in required):
        raise ValueError('Specify at most 16 valid guest TCP ports')
    # Query Windows itself; this also works with only SSH forwarded on the host.
    ports = ','.join(map(str,sorted({int(g) for _,g in s['ports']} | set(required))))
    script = CONSOLE + r"""
$owner = Get-Content C:\ProgramData\WinHoo\owner.json -Raw | ConvertFrom-Json
$console = Get-WinHooConsole
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
@{owner=$owner.uuid;account=$env:USERNAME;computer=$env:COMPUTERNAME;console=$console.account;consoleSession=$console;build=$os.BuildNumber;arch=$env:PROCESSOR_ARCHITECTURE;
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
    check_loopback(info['listeners'],required)
    return info


def transport_reason(error):
    msg = error.lower()
    for fragment, reason in [('closed by remote host','SSH connection closed by the guest'),
                             ('connection reset','SSH connection reset'), ('timed out','SSH connection timed out'),
                             ('connection refused','SSH is not listening')]:
        if fragment in msg:
            return reason
    return 'SSH command did not complete'


def console_state(info, account):
    session = info.get('consoleSession') or {}
    session_id = session.get('id')
    if type(session_id) is not int or not 0 < session_id < 0xffffffff or session.get('connection') != 'active':
        return 'Windows has no confirmed active physical console'
    if session.get('lock') == 'locked':
        return 'console-locked'
    machine = info.get('computer')
    if not machine or (info.get('console') or '').casefold() != (machine + '\\' + account).casefold():
        return 'The dedicated local account is not on the physical console'
    if session.get('lock') != 'unlocked':
        return 'Windows console lock state is unknown'
    return 'ready'


def wait(d, s, timeout, *, interactive=True):
    until = time.monotonic() + timeout
    last = 'Waiting for authenticated guest control'
    locked = None
    while time.monotonic() < until:
        running = current(d, s)
        try:
            stage = phase(d, s)
        except ReceiptUnavailable:
            locked = None
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
            locked = None
            time.sleep(1)
            continue
        try:
            result = guest(d, s, ATTEST if interactive else ATTEST_TRANSPORT,
                           timeout=min(12, max(.1, until-time.monotonic())), capture=True)
        except subprocess.TimeoutExpired:
            result = None
            last = 'Read-only guest attestation timed out'
        if result and not result.returncode:
            info = json.loads(result.stdout.strip())
            if info.get('owner') != s['uuid'] or info.get('account', '').lower() != s['account'].lower():
                raise ValueError('Authenticated guest identity mismatch')
            if info.get('arch') != 'ARM64':
                raise ValueError('Unexpected guest architecture')
            if not interactive:
                return info  # File recovery needs keyed SSH, not an interactive desktop.
            last = console_state(info, s['account'])
            if last == 'ready':
                save(d / 'installed.json', info)
                return info
            if last == 'console-locked':
                identity = (info['consoleSession']['id'], info.get('console'))
                now = time.monotonic()
                if locked is None or locked[0] != identity:
                    locked = (identity, now)
                elif now - locked[1] >= 45:
                    raise ValueError('console-locked: Windows is reachable but the physical console remains locked; sign in to the dedicated local account')
        elif result and ('HOST IDENTIFICATION HAS CHANGED' in result.stderr or 'Permission denied' in result.stderr):
            raise ValueError('Guest key or login rejected; inspect this owned instance')
        elif result:
            last = transport_reason(result.stderr)
        if last != 'console-locked':
            locked = None
        time.sleep(min(5 if locked else 2, max(0, until-time.monotonic())))
    raise ValueError(last + '; see status, screenshot, and the guest installation log')


def request_windows_shutdown(d, s, timeout):
    owner = str(uuid.UUID(s['uuid']))
    account = s['account']
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,18}', account):
        raise ValueError('Unexpected dedicated Windows account')
    script = IDENTITY + f"if ($who.owner -cne '{owner}' -or $o.account -ine '{account}' -or $who.account -ine '{account}') {{ throw 'Different Windows shutdown owner' }}\n" + r"""
& "$env:SystemRoot\System32\shutdown.exe" /s /t 0
if ($LASTEXITCODE -ne 0) { throw 'Windows rejected the shutdown request' }
@{owner=$who.owner;account=$who.account;accepted=$true} | ConvertTo-Json -Compress
"""
    result = guest(d,s,script,timeout=timeout,capture=True)
    if result.returncode:
        raise ValueError('Windows did not acknowledge shutdown; no power-button fallback was sent')
    ack = json.loads(result.stdout)
    if (ack.get('owner') != owner or (ack.get('account') or '').casefold() != account.casefold()
            or ack.get('accepted') is not True):
        raise ValueError('Windows shutdown acknowledgement does not identify this desktop')


def down(d, s, timeout):
    if current(d, s) == 'off':
        return {'state': 'off', 'shutdown': 'already off'}
    with Monitor(d, s) as q:
        pid = process_id(d)
        if pid is None:
            raise ValueError('Cannot confirm the monitored QEMU process id')
        end = time.monotonic() + timeout
        method = 'previously-stopped'
        if q.send('query-status')['status'] != 'shutdown':
            if owned_receipt(d,'installed.json','owner',s):
                # QMP system_powerdown alone can turn off an ARM virt machine
                # without Windows ever receiving or completing a shutdown.
                method = 'windows-shutdown'
                request_windows_shutdown(d,s,min(20,max(.1,timeout)))
            else:
                method = 'installer-power-button'
                q.send('system_powerdown')
        while q.send('query-status')['status'] != 'shutdown':
            if time.monotonic() >= end:
                raise ValueError('Guest shutdown unconfirmed; disk and process retained')
            time.sleep(.5)
        if method == 'windows-shutdown':
            event = q.shutdown_event or {}
            if event.get('guest') is not True or event.get('reason') != 'guest-shutdown':
                raise ValueError('QEMU did not observe a guest-originated shutdown; process retained')
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
    info = {'state': 'off', 'uuid': s['uuid'], 'method':method,
            'shutdown':'guest-confirmed' if method == 'windows-shutdown' else 'monitor-confirmed',
            'timeUnix': time.time()}
    save(d / 'shutdown.json', info)
    return info


def arguments(parser):
    commands = parser.add_subparsers(dest='desktop_op', required=True)
    commands.add_parser('doctor')
    identity = commands.add_parser('identity', help='Fingerprint this desktop CLI and its bundled Windows files')
    identity.add_argument('--expect-sha256', metavar='SHA256', help='Require these exact controller bytes')
    archive = commands.add_parser('inspect-archive', help='Verify a saved Windows ZIP against its receipt without starting a guest')
    archive.add_argument('file', type=Path)
    archive.add_argument('--receipt', type=Path, required=True, help='Saved desktop archive or collect JSON receipt')
    archive.add_argument('--expect-owner', metavar='UUID', help='Require this desktop UUID')
    archive.add_argument('--find', metavar='FILENAME', help='List entries with this exact filename')
    commands.add_parser('sources', help='Fetch or verify the pinned Windows media, OpenSSH and network driver').add_argument('cache',type=Path)
    p = commands.add_parser('media')
    p.add_argument('--esd', type=Path, required=True)
    p.add_argument('--sha256', required=True)
    p.add_argument('--output', type=Path, required=True)
    for op in ('create','forwards','up','wait','status','inspect','foreground','focus-window','screenshot','exec','put','archive','get','collect','down'):
        p = commands.add_parser(op)
        p.add_argument('directory', type=Path, help='Private retained VM directory')
        if op == 'create':
            for flag in ('setup','openssh','netkvm'):
                p.add_argument('--'+flag, type=Path, required=True)
            p.add_argument('--account', default='WinHooTest')
            p.add_argument('--allow-idle-sleep',action='store_true',help='Do not request a macOS idle-sleep assertion for the VM')
            p.add_argument('--ssh-port', type=int, default=2226)
            p.add_argument('--forward', action='append', default=[], metavar='HOST:GUEST')
        if op == 'forwards':
            flags = p.add_mutually_exclusive_group(required=True)
            flags.add_argument('--only-ssh',action='store_true',help='Remove all optional host forwards')
            flags.add_argument('--forward',action='append',metavar='HOST:GUEST',help='Replace the optional forward list')
        if op == 'up':
            p.add_argument('--wait',nargs='?',const='console',choices=('console','transport'),
                           help='Wait for the unlocked test console (default) or only authenticated guest transport')
            p.add_argument('--timeout',type=int,help='Readiness timeout in seconds when using --wait (default: 900)')
        if op in ('exec','put','archive','get','collect','wait','inspect','down'):
            p.add_argument('--timeout', type=int, default=900 if op=='wait' else 120)
        if op == 'inspect':
            p.add_argument('--require-loopback',type=int,metavar='GUEST_PORT',action='append',default=[],
                           help='Require this guest service to listen only on loopback; repeat for other ports')
        if op == 'status':
            p.add_argument('--locks-only', action='store_true',
                           help='Read host controller locks without contacting QEMU or Windows')
        if op in ('foreground', 'focus-window'):
            p.add_argument('--timeout', type=int, default=90, help='Total timeout including cold task startup and cleanup (1–300 seconds)')
        if op == 'foreground':
            p.add_argument('--expect-pid', type=int, help='Fail if the foreground window belongs to a different Windows process')
        if op == 'focus-window':
            p.add_argument('--pid', required=True, type=int, help='Windows process to activate on the dedicated test console')
            p.add_argument('--started-ticks', type=int, help='Also require the previously observed UTC process start time in .NET ticks')
        if op == 'exec':
            src = p.add_mutually_exclusive_group(required=True)
            src.add_argument('--file', type=Path)
            src.add_argument('--stdin', action='store_true')
        if op == 'put':
            p.add_argument('source', type=Path)
            p.add_argument('basename', help='Filename inside the dedicated user profile')
        if op == 'archive':
            p.add_argument('result_directory', help='Absolute local Windows directory containing completed test results')
            p.add_argument('basename', help='New ZIP basename in the dedicated Windows profile')
            p.add_argument('--output', type=Path, help='Download and verify the ZIP before publishing this new local file')
        if op == 'collect':
            p.add_argument('--from-directory', help='Archive this completed Windows result directory after boot; use a fresh ZIP basename')
            p.add_argument('--finish-running-owner', metavar='UUID',
                           help='Recover a completed result directory on this already running desktop, then shut it down')
        if op in ('get','collect'):
            p.add_argument('basename', help='Regular file inside the dedicated user profile')
            p.add_argument('destination', type=Path, help='New local file; never overwrites evidence')


def dispatch(a):
    os.umask(0o077)
    op = a.desktop_op
    if op == 'identity':
        return controller_identity(a.desktop_launcher, Path(__file__).parent, a.expect_sha256)
    if op == 'inspect-archive':
        from .archive_info import inspect
        return inspect(a.file, a.receipt, owner=a.expect_owner, find=a.find)
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
        if a.locks_only:
            return {'uuid': s['uuid'], 'controllerLocks': controller_locks(d)}
        state = current(d,s)
        try:
            stage = phase(d,s)
        except ReceiptUnavailable:
            if state != 'running':
                raise
            stage = 'receipt-unavailable'
        return {'uuid': s['uuid'], 'state': state, 'installed': owned_receipt(d,'installed.json','owner',s),
                'deployment': stage, 'forwards': s['ports'],
                'controllerLocks': controller_locks(d)}
    if op == 'screenshot':
        s = read_state(d)
        with Monitor(d, s) as q:
            q.send('screendump', filename=str(d / 'screen.ppm'))
        return {'path': str(d / 'screen.ppm')}
    with operation_lock(d,op):
        s = read_state(d)
        if op == 'forwards':
            return set_forwards(d,s,a.forward or [])
        if op == 'up':
            if a.timeout is not None and (not a.wait or a.timeout < 1):
                raise ValueError('--timeout requires --wait and a positive number of seconds')
            started = up(d,s)
            if a.wait:
                ready = wait(d,s,a.timeout or 900,interactive=a.wait == 'console')
                return {**started,'state':'running','readiness':ready,'waitedFor':a.wait}
            return started
        if op == 'down':
            return down(d,s,a.timeout)
        if op == 'wait':
            return wait(d,s,a.timeout)
        if op == 'collect':
            return collect(d,s,a.basename,a.destination,a.timeout,source=a.from_directory,
                           running_owner=a.finish_running_owner)
        if current(d,s) != 'running' or not owned_receipt(d,'installed.json','owner',s):
            raise ValueError('Run desktop wait to attest this instance first')
        if op == 'inspect':
            return inspect_guest(d,s,a.timeout,a.require_loopback)
        if op == 'foreground':
            return foreground(d,s,a.expect_pid,timeout=a.timeout)
        if op == 'focus-window':
            return foreground(d,s,a.pid,activate=True,started_ticks=a.started_ticks,timeout=a.timeout)
        if op == 'exec':
            return guest(d,s,sys.stdin.read() if a.stdin else a.file.read_text(),timeout=a.timeout,capture=False).returncode
        if op == 'archive':
            if a.output is not None:
                check_destination(a.output)
            receipt = archive_results(d,s,a.result_directory,a.basename,a.timeout)
            if a.output is not None:
                get_file(d,s,a.basename,a.output,a.timeout,archive=receipt)
                receipt['path'] = str(a.output.resolve())
            return receipt
        plain_filename(a.basename)
        if op == 'get':
            get_file(d,s,a.basename,a.destination,a.timeout)
            return {'path':str(a.destination.resolve())}
        return subprocess.run(['scp', *ssh_args(d,s), str(a.source.resolve()), f"{s['account']}@127.0.0.1:{a.basename}"],
                              timeout=a.timeout, stdin=subprocess.DEVNULL).returncode


def plain_filename(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', name):
        raise ValueError('The guest filename must be a plain basename')


def archive_results(d, state, source, name, timeout):
    plain_filename(name)
    if not name.lower().endswith('.zip'):
        raise ValueError('Expected a ZIP basename')
    request = json.dumps({'owner':state['uuid'],'source':source,'name':name}).encode('utf-8')
    payload = base64.b64encode(request).decode('ascii')
    script = ("$request=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+payload+"')) | ConvertFrom-Json\n"
              + Path(__file__).with_name('archive-results.ps1').read_text(encoding='utf-8'))
    proc = guest(d,state,script,timeout=timeout,capture=True)
    if proc.returncode:
        lines = [line for line in proc.stderr.splitlines() if line.strip() and not line.startswith(('#< CLIXML','<Objs '))]
        raise ValueError('Guest archive failed: ' + next(iter(lines),'no text diagnostic')[:300])
    receipt = json.loads(proc.stdout)
    if (receipt.get('owner') != state['uuid'] or receipt.get('name') != name
            or type(receipt.get('bytes')) is not int or receipt['bytes'] < 1
            or not re.fullmatch('[0-9a-f]{64}', str(receipt.get('sha256','')))):
        raise ValueError('Incomplete or mismatched guest archive receipt')
    return receipt


def check_destination(destination):
    if destination.exists() or destination.is_symlink():
        raise ValueError('Refusing to replace an existing local file')
    if not destination.parent.is_dir():
        raise ValueError('The local output directory must already exist')


def get_file(d, state, name, destination, timeout, *, archive=None):
    check_destination(destination)
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        result = Path(staging)/'download'
        subprocess.run(['scp', *ssh_args(d,state), f"{state['account']}@127.0.0.1:{name}", str(result)],
                       stdin=subprocess.DEVNULL, check=True, timeout=timeout)
        if not stat.S_ISREG(result.lstat().st_mode):
            raise ValueError('Expected a regular file from the guest')
        if archive is not None and (result.stat().st_size != archive['bytes'] or media.digest(result) != archive['sha256']):
            raise ValueError('Downloaded file does not match the guest archive receipt')
        result.chmod(0o600)
        os.link(result,destination)  # Atomic publication, including a concurrent writer.


def collect(d, state, name, destination, timeout, *, source=None, running_owner=None):
    """Collect closed result files and confirm shutdown under the same lifecycle lock."""
    plain_filename(name)
    if source is not None and not name.lower().endswith('.zip'):
        raise ValueError('Expected a ZIP basename')
    check_destination(destination)
    if running_owner is not None:
        if running_owner != state['uuid'] or source is None:
            raise ValueError('Recovery requires the original desktop owner UUID and a completed result directory')
        if current(d,state) != 'running' or not owned_receipt(d,'installed.json','owner',state):
            raise ValueError('The named desktop must already be running and previously attested')
    else:
        if current(d,state) != 'off' or not owned_receipt(d,'installed.json','owner',state):
            raise ValueError('Collection requires a stopped, previously attested Windows desktop')
        if not owned_receipt(d,'shutdown.json','uuid',state) or json.loads((d/'shutdown.json').read_text()).get('shutdown') != 'guest-confirmed':
            raise ValueError('A confirmed shutdown of this desktop is required for collection')
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        staged = Path(temp)/'complete'
        if running_owner is None:
            active = up(d,state)
            if active.get('state') != 'running' or active.get('uuid') != state['uuid']:
                raise ValueError('Could not acquire the stopped desktop for collection')
        archive = None
        downloaded = False
        try:
            wait(d,state,timeout,interactive=False)
            if source is None:
                get_file(d,state,name,staged,timeout)
            else:
                archive = archive_results(d,state,source,name,timeout)
                get_file(d,state,name,staged,timeout,archive=archive)
            downloaded = True
        finally:
            # Leave an adopted guest running if attestation or transfer failed.
            # A guest booted here is always ours to stop.
            if running_owner is None or downloaded:
                receipt = down(d,state,timeout)
                if receipt.get('uuid') != state['uuid'] or receipt.get('shutdown') != 'guest-confirmed' or receipt.get('state') != 'off':
                    raise ValueError('Guest shutdown after collection was not confirmed')
        os.link(staged,destination)
    return {'uuid':state['uuid'],'path':str(destination.resolve()),'shutdown':'guest-confirmed',
            **({'archive':archive} if archive is not None else {})}
