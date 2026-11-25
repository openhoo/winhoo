# Windows desktop VM runtime 0.4.0

This backend runs a full Windows VM in a Linux container. The original WinHoo
native Windows container controller remains available through its existing CLI.

The consumer supplies its own OEM provisioning, shared directory, private
environment file and authenticated application readiness. HooSaidThat writes
this configuration automatically and retains its existing Windows disk and SSH
identity when switching to WinHoo.

## Controller

```bash
python3 /path/to/winhoo/winhoo_desktop.py --config /private/runtime/desktop.json build
python3 /path/to/winhoo/winhoo_desktop.py --config /private/runtime/desktop.json config
python3 /path/to/winhoo/winhoo_desktop.py --config /private/runtime/desktop.json up
python3 /path/to/winhoo/winhoo_desktop.py --config /private/runtime/desktop.json status
python3 /path/to/winhoo/winhoo_desktop.py --config /private/runtime/desktop.json stop
python3 /path/to/winhoo/winhoo_desktop.py --config /private/runtime/desktop.json down
```

The desktop image contains the standalone Python controller, runtime manifest
and owner watchdog under `/usr/share/winhoo/controller`. HooSaidThat extracts
and caches that bundle automatically from the installed image. Its normal use
requires no WinHoo source checkout, package installation or manual JSON file.
The desktop controller runs on Python 3.9+ with the standard library only.
The native Windows container controller retains its Python 3.10+ requirement.

For image development, build from the repository root:

```bash
docker build --platform linux/arm64 -t winhoo-desktop:0.4.0-arm64 -f desktop/Dockerfile.arm64 .
# On a Linux x64 engine, use Dockerfile.amd64, linux/amd64 and :0.4.0-amd64.
```

The image contains general Windows VM host tools and the controller bundle.
Applications are supplied by consumers: no NVDA, Chrome or HooSaidThat payload
is included. Windows is installed automatically into a retained volume on first
use; the distributable Linux image contains no installed Windows disk, user
credentials or license key. Later starts reuse that disk.

Docker/Podman must select the local Linux engine. The controller does not alter
the active engine context.

Example configuration (`environmentFile` contains `WINDOWS_PASSWORD=...` and
must remain private):

```json
{
  "schema": "winhoo.desktop/1",
  "name": "winhoo-desktop",
  "project": "winhoo-desktop",
  "hostname": "WINHOO-DESKTOP",
  "engine": "docker",
  "architecture": "arm64",
  "acceleration": "tcg",
  "image": "winhoo-desktop:0.4.0-arm64",
  "buildImage": false,
  "volume": "winhoo-desktop-state",
  "storage": "/private/runtime/state",
  "shared": "/private/runtime/shared",
  "oem": "/private/runtime/oem",
  "environmentFile": "/private/runtime/.env",
  "legacyBind": false,
  "adoptLegacy": false,
  "ownerGeneration": null,
  "ports": [{"host": 8008, "guest": 8006, "protocol": "tcp"}],
  "environment": {
    "VERSION": "11",
    "USERNAME": "DesktopUser",
    "RAM_SIZE": "8G",
    "CPU_CORES": "2",
    "DISK_SIZE": "64G",
    "VERIFY": "Y"
  }
}
```

On WSL2 ext4, a cloned Windows disk cannot normally be backed up with a reflink. To prepare a stopped disk on that filesystem, explicitly set `"diskBackup": "sparse"`. Before changing the disk, WinHoo saves a separate sparse copy. It requires space for the entire logical disk plus 8 GiB and refuses disks over 128 GiB logical or 64 GiB allocated. The default remains `"reflink"`.

All four absolute paths must exist. New Windows disks use SATA and sparse raw
storage in the named engine volume. `legacyBind: true` preserves an existing
SCSI disk under `storage`. `down` removes containers and networks and retains
the disk. No operation uses `--volumes` or forcibly removes a mounted disk.
With `buildImage: false`, the extracted controller needs no Dockerfiles or
source checkout. Consumers should resolve the image to its immutable local
image ID before starting it. Omitting `image` enables source builds with the
matching architecture's Dockerfile.

Each container has an owner label derived from the configuration's absolute
path. Existing storage and shared mounts must match exactly before any action.
`adoptLegacy` permits an unlabeled predecessor with those exact mounts; it never
permits a different owner label. A kernel file lock serializes lifecycle
operations against the configuration. The Linux daemon identity is pinned on
first use; switching engines fails before lifecycle changes. Published ports
always bind to loopback.

Repeating `up` preserves an already running desktop session. Lifecycle commands
reject other containers sharing the Compose project label, including renamed
guests. For bind disks the offline preparation checks active container mounts
for parent directories and directly mounted disk files as well as the configured path.

For managed runs, supply a random 32-character lowercase hex `ownerGeneration`.
The owner atomically renews `shared/bootstrap/winhoo-owner-heartbeat` every five
seconds with `GENERATION MONOTONICALLY_CHANGING_TICK\n`. WinHoo starts an
independent watchdog inside the container. Ninety seconds without a changed
tick requests TERM/ACPI shutdown. A new generation retires the old watchdog.
The container has no restart policy that could resurrect a stopped owner.
Normal cleanup requests ACPI shutdown with ten minutes of grace for software
CPU emulation (TCG), or two minutes with KVM. The QEMU termination deadline,
container grace and controller timeout are ordered so an outer wrapper does
not cut off that grace period. The disk remains even if the guest ultimately
fails to shut down within the deadline.
Cleanup can wait up to three minutes for an in-flight guest command to release
the controller lock before this shutdown grace starts. Concurrent startup and
other lifecycle mutations still fail immediately. Consumers must budget both
the lock wait and the shutdown grace in their outer process timeouts.

Startup, SSH availability, a logged-in desktop, and actual NVDA presentation
are separate checks. The controller intentionally does not equate these.

## Guest tooling

The general image includes a hash-pinned official QEMU Windows guest agent and
the matching x64 VirtIO serial driver. A generic OEM wrapper installs these
before calling the consumer's original `install.bat`. The consumer directory is
mounted read-only and remains unchanged. No consumer application is in this image.

For a retained Windows disk, `up` prepares the same generic bootstrap while
every container using the disk is stopped. It identifies the Windows root
read-only. If the OEM already installed the matching guest tools, it verifies the
installation receipt and hashes the installed files, then skips the repair.
Otherwise, it completes a backup and places the one-time installer in
the existing Startup directory. It refuses hibernated volumes and never forces
an NTFS mount. A missing reflink fails before any guest write unless the operator
selected the bounded sparse backup described above. The backup is retained alongside the
disk. `tools-repair` explicitly repeats this preparation after stopping Windows.
The preparation marker includes the installed payload's SHA-256, so a corrected
installer invalidates the cache even when the development version is unchanged.
An unrelated Windows Startup file with the same name is preserved: preparation
refuses to overwrite it before making any guest changes.

The installer checks the actual VirtIO serial device before touching drivers.
A healthy existing driver is reused; an absent device receives the pinned
Windows 11 x64 driver. An installer exit code alone cannot establish readiness:
the expected PCI device must report no Windows configuration error.

Bootstrap phases and failures are written to
`C:\ProgramData\WinHoo\bootstrap-status.json` and, when the private share is
available, `shared/bootstrap/winhoo-tools-status.json`. Local detailed output
is retained in `C:\ProgramData\WinHoo\guest-tools.log`. These diagnostics work
before QGA starts; HooSaidThat reports current-boot failures immediately.
Managed receipts carry the existing owner generation, independently of Windows
clock accuracy. `guest-status` returns the guest clock as integer `timeUnixMs`;
the helper performs the QGA nanosecond conversion before emitting JSON.

QGA and QMP use Unix sockets inside a mode-0700 container directory. They have
no published TCP port. Access uses the owning Docker/Podman engine, with the
same engine identity, container owner, storage checks and lifecycle lock.

```bash
python3 winhoo_desktop.py --config /private/runtime/desktop.json guest-status
python3 winhoo_desktop.py --config /private/runtime/desktop.json guest-time-sync
python3 winhoo_desktop.py --config /private/runtime/desktop.json guest-exec --script ./probe.ps1 --timeout 120
python3 winhoo_desktop.py --config /private/runtime/desktop.json guest-process --pid 1234 --timeout 120
python3 winhoo_desktop.py --config /private/runtime/desktop.json guest-read --path 'C:\ProgramData\WinHoo\guest-tools.log' --max-bytes 65536 --timeout 15
python3 winhoo_desktop.py --config /private/runtime/desktop.json stop
python3 winhoo_desktop.py --config /private/runtime/desktop.json tools-repair
```

`guest-exec` sends UTF-8 PowerShell source through stdin without shell
interpolation. JSON results include the source SHA-256, guest PID, stdout,
stderr and native exit code. Exit 124 leaves the process running and returns
its PID; poll it with `guest-process` instead of submitting it twice. These
commands run as the guest agent's SYSTEM account. They are administrative tools
for the dedicated owned test VM. Application readiness remains the consumer's
responsibility. Normal lifecycle shutdown tries QGA before the ACPI fallback.

`guest-read` retrieves a file directly through QGA without starting PowerShell
or requiring SSH. It opens the file read-only, closes its handle on success and
failure, and limits each operation to at most 1 MiB (64 KiB by default). Its JSON
result contains the exact bytes in `dataBase64`, the byte count and an explicit
`truncated` flag. Missing files and malformed responses fail. This allows early
installation logs to be inspected while Windows application startup is slow.
Windows sharing restrictions still apply: an actively written file can be
locked against QGA reads. The command reports that error instead of retrying
indefinitely or returning an empty log. For such files, use a bounded
`guest-exec` probe that opens a .NET `FileStream` with `FileShare.ReadWrite`.

### Background processes on the interactive desktop

The image also supplies the general Windows GUI executable
`C:\ProgramData\WinHoo\tools\WinHoo.RunHidden.exe`. Use it as a Scheduled Task's
executable when a long-running console application must share the desktop with
GUI applications without opening a terminal or taking keyboard focus:

```text
WinHoo.RunHidden.exe --stdout C:\work\stdout.log --stderr C:\work\stderr.log --status C:\work\process.json -- C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoLogo -NoProfile -NonInteractive -File C:\work\service.ps1
```

For interactive application supervisors, create task settings with
`New-ScheduledTaskSettingsSet -Priority 4`. The default Task Scheduler priority
is for background work; actual TCG diagnosis found the launcher at
`BelowNormal`, with minutes elapsing before its first receipt. The launcher
does not change process priority itself.

The executable has the Windows GUI subsystem and starts its child with no
console and redirected standard handles. A PowerShell `-WindowStyle Hidden`
argument alone did not prevent terminal windows during actual NVDA acceptance.
The launcher invokes the absolute executable directly, quotes each argument,
closes stdin and drains stdout/stderr concurrently as original bytes. Omitted
output channels are discarded. Output files are replaced for each invocation;
consumers should choose separate files for concurrent processes.

The optional atomic status receipt uses schema `winhoo.process/1`, states
`starting`, `running`, `exited` or `failed`, the child PID, native `exitcode` and
`generatedUtc`. It records no command arguments. Native child exit codes pass
through; a launcher failure returns 125. All output paths must be absolute and
distinct. Read the receipt and logs to distinguish a launcher failure from a
child returning 125. This launcher provides process execution; the consumer
still establishes application readiness.

Only the small compiled executable and its source enter the runtime image.
The exact Mono compiler package is pinned in `dependencies.json` and remains
in a separate build stage. New disks receive the launcher through OEM setup;
retained disks receive it through the normal stopped-disk preparation and
backup path. `desktop/runtime.json` advertises its installed location so
consumers can reject an older image before starting Windows.

`tests/Test-RunHidden.ps1` and the console fixture `tests/RunHiddenProbe.cs`
qualify actual Windows behavior: no child console, literal Unicode/empty/quoted
arguments, concurrent large output streams, exit 37, and invalid output-path
rejection. These checks require the compiled fixture and a real Windows guest.
