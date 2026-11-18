"""Build an unattended installer for one new, owned Windows desktop."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from xml.etree import ElementTree
import zipfile

ASSETS = Path(__file__).parent
SSH_SHA = 'd2b1928225479fec9e0c938f527fdb5ed676446cca1bbcc5af84aca1f272842e'
NETKVM = {
    'netkvm.cat': '7185ae6c8c6d31d047c77b167e740b7fe3aa676a89b853783cdc47fd889d51c5',
    'netkvm.inf': '1aaf16af51ef4697503e7c261f318a9414dc726be5250f09cc64d558f46138fc',
    'netkvm.sys': '211340b8128145bae8890a4fab27b625c23e032ca65be984b524e1b0d4b54f0f',
    'netkvmco.exe': '346c35078dc7adf0c9521243ff817da5e882af668155e7d736fa4bccdc0b04f9',
    'netkvmp.exe': '98b239dc9e4080cc21211582f05ab1d7f6ac74d867980b183c3f8f5ae9d5dc5a',
}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run(*cmd):
    subprocess.run(list(map(str, cmd)), check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


def prepare(esd, expected, destination):
    if destination.exists():
        raise ValueError('Media destination already exists; reuse the completed setup directory')
    if not re.fullmatch('[0-9a-fA-F]{64}', expected) or digest(esd) != expected.lower():
        raise ValueError('Windows ESD SHA-256 mismatch')
    xml = ElementTree.fromstring(subprocess.check_output(['wimlib-imagex', 'info', esd, '--xml']))
    def index(name, arm):
        matches = [i.get('INDEX') for i in xml.findall('IMAGE') if i.findtext('NAME') == name
                   and (not arm or i.findtext('WINDOWS/ARCH') == '12')]
        if len(matches) != 1:
            raise ValueError(f'Expected exactly one image for {name}')
        return matches[0]
    indices = [index(n, arm) for n, arm in [('Windows Setup Media', False),
        ('Microsoft Windows PE (arm64)', True), ('Microsoft Windows Setup (arm64)', True), ('Windows 11 Pro', True)]]
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        tmp = Path(temp)
        tree = tmp / 'setup'
        tree.mkdir()
        run('wimlib-imagex', 'apply', esd, indices[0], tree, '--no-acls', '--no-attributes')
        def boot():
            boot_wim = tree / 'sources/boot.wim'
            run('wimlib-imagex', 'export', esd, indices[1], boot_wim, '--compress=LZX', '--threads=4')
            run('wimlib-imagex', 'export', esd, indices[2], boot_wim, '--boot', '--threads=4')
        def install():
            wim = tmp / 'professional.wim'
            run('wimlib-imagex', 'export', esd, indices[3], wim, '--compress=XPRESS', '--threads=4')
            run('wimlib-imagex', 'split', wim, tree / 'sources/install.swm', 3600)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(task) for task in (boot, install)]
            for f in futures:
                f.result()
        tree.rename(destination)
    return {'setup': str(destination), 'esdSha256': expected.lower(), 'imageIndices': indices}


def installer(directory, setup, ssh_bundle, drivers, state):
    for file in ('efi/microsoft/boot/efisys_noprompt.bin', 'sources/boot.wim', 'sources/install.swm'):
        if not (setup / file).is_file():
            raise ValueError(f'Incomplete Windows setup tree: {file}')
    if digest(ssh_bundle) != SSH_SHA:
        raise ValueError('Wrong OpenSSH ARM64 archive')
    for name, sha in NETKVM.items():
        if digest(drivers / name) != sha:
            raise ValueError(f'Wrong Windows 11 ARM64 NetKVM driver: {name}')
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        seed = Path(tmp)
        guest = seed / 'oem' / '$1/ProgramData/WinHoo'
        guest.mkdir(parents=True)
        (guest / 'NetKVM').mkdir()
        for name in NETKVM:
            shutil.copyfile(drivers / name, guest / 'NetKVM' / name)
        with zipfile.ZipFile(ssh_bundle) as archive:
            archive.extractall(guest)  # Entire archive is content pinned above.
        shutil.copyfile(ASSETS / 'first-logon.ps1', guest / 'first-logon.ps1')
        for key in (directory / 'id_ed25519', guest / 'host_ed25519'):
            run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'WinHoo', '-f', key)
        shutil.copyfile(directory / 'id_ed25519.pub', guest / 'client.pub')
        public = (guest / 'host_ed25519.pub').read_text().split()
        (directory / 'known_hosts').write_text(f"winhoo-{state['uuid']} {' '.join(public[:2])}\n")
        (guest / 'owner.json').write_text(json.dumps({'uuid': state['uuid'], 'account': state['account']}))
        password = 'Wh9!' + secrets.token_urlsafe(30)
        answer = (ASSETS / 'unattend.xml').read_text().replace('WINHOO_PASSWORD', password).replace('WINHOO_ACCOUNT', state['account'])
        (seed / 'winhoo-answer.xml').write_text(answer)
        (seed / 'winhoo-owner.txt').write_bytes((state['uuid'] + '\r\n').encode('ascii'))
        phase = directory / 'phases.img'
        with phase.open('xb') as disk:
            disk.truncate(16 * 1024 * 1024)
        run('mformat', '-i', phase, '-v', 'WHSTATUS', '::')
        run('mcopy', '-i', phase, seed / 'winhoo-owner.txt', '::WHSTATE.TAG')
        boot = seed / 'deploy.wim'
        shutil.copyfile(setup / 'sources/boot.wim', boot)
        for name, dest in [('winpeshl.ini', 'winpeshl.ini'), ('deploy.cmd', 'winhoo-deploy.cmd')]:
            local = seed / name
            local.write_bytes((ASSETS / name).read_text().replace('\n', '\r\n').encode('ascii'))
            run('wimlib-imagex', 'update', boot, '2', '--command', f'add "{local}" "/Windows/System32/{dest}"')
        (seed / 'ei.cfg').write_bytes(b'[EditionID]\r\nProfessional\r\n[Channel]\r\nRetail\r\n[VL]\r\n0\r\n')
        # Windows Setup reads Joliet names, including the $OEM$ distribution folder.
        run('xorriso', '-as', 'mkisofs', '-quiet', '-iso-level', '3', '-J', '-joliet-long',
            '-V', 'WINHOO', '-e', 'efi/microsoft/boot/efisys_noprompt.bin', '-no-emul-boot',
            '-o', directory / 'installer.iso', '-graft-points', f'/={setup}', f'/winhoo-answer.xml={seed}/winhoo-answer.xml',
            f'/winhoo-owner.txt={seed}/winhoo-owner.txt', f'/sources/boot.wim={boot}',
            f'/sources/ei.cfg={seed}/ei.cfg', f'/sources/$OEM$={seed}/oem')

# Windows 11 Pro ARM64 en-US; the catalog was obtained via Microsoft's HTTPS
# product catalog (LinkId=2156292). Its file URL uses HTTP; pin the full bytes.
WINDOWS = ('windows-pro-arm64.esd', 4527171158, 'c01b6f474c772093159b63fb3a168d2666556b223d288f67efc37c4fbaeddd25',
    'http://dl.delivery.mp.microsoft.com/filestreamingservice/files/37acc189-7f4f-4e07-976c-1cf0a099ec79/'
    '26100.4349.250607-1500.ge_release_svc_refresh_CLIENTCONSUMER_RET_A64FRE_en-us.esd')
OPENSSH = ('OpenSSH-ARM64.zip', 5330882, SSH_SHA,
    'https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.4.0.0p1-Preview/OpenSSH-ARM64.zip')
VIRTIO = ('virtio-win-arm-0.1.285.tar.xz', 1092056, 'c6712f8d5730c09c1212be9fc3baa18b78534f3c8c136cf02b2cca46515ca310',
    'https://github.com/qemus/virtiso-arm/releases/download/v0.1.285-1/virtio-win-0.1.285.tar.xz')


def sources(directory):
    import tarfile
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    def obtain(spec):
        name, length, expected, url = spec
        final = directory / name
        if final.exists():
            if final.is_symlink() or final.stat().st_size != length or digest(final) != expected:
                raise ValueError(f'Cached {name} differs from its pin; preserving it for inspection')
            return final
        with tempfile.TemporaryDirectory(dir=directory) as temp:
            download = Path(temp) / name
            run('curl', '--fail', '--location', '--silent', '--show-error', '--proto', '=http,https',
                '--proto-redir', '=https', '--connect-timeout', '10', '--max-time', '900',
                '--retry', '2', '--max-filesize', str(length), '--output', download, url)
            if download.stat().st_size != length or digest(download) != expected:
                raise ValueError(f'{name} failed its content check')
            download.replace(final)
        return final
    with ThreadPoolExecutor(3) as pool:
        windows, ssh, virtio = pool.map(obtain, [WINDOWS, OPENSSH, VIRTIO])
    target = directory / 'NetKVM'
    if target.is_symlink():
        raise ValueError('Driver cache must not be a symlink')
    target.mkdir(exist_ok=True)
    with tarfile.open(virtio) as archive:
        for name, expected in NETKVM.items():
            entry = archive.getmember('NetKVM/w11/ARM64/' + name)
            if not entry.isfile() or entry.size > 2*1024*1024:
                raise ValueError('Unexpected NetKVM archive entry')
            data = archive.extractfile(entry).read()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f'Unexpected NetKVM bytes: {name}')
            output = target / name
            if output.is_symlink() or (output.exists() and digest(output) != expected):
                raise ValueError(f'Existing driver differs from the verified archive: {name}')
            if not output.exists():
                with output.open('xb') as f:
                    f.write(data)
    return {'esd': str(windows), 'esdSha256': WINDOWS[2], 'openssh': str(ssh), 'netkvm': str(target)}
