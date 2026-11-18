# Local Windows desktop on Apple Silicon

Use this backend for tests that need a real, signed-in Windows desktop. QEMU uses Hypervisor.framework. Windows runs on a local qcow2 disk; stopping a container engine or disconnecting from a remote host has no effect on it.

## First boot

Use a licensed Windows 11 Pro installation. The controller downloads the pinned en-US ARM64 Microsoft media, Win32-OpenSSH and the NetKVM driver to the Mac. No Windows binaries, disk images or account credentials ship with WinHoo.

Install Python 3.10 or later, OpenSSH and the host tools (Homebrew: `brew install qemu swtpm xorriso wimlib mtools`). Install WinHoo into a virtual environment with `python -m pip install .`, then run:

```sh
winhoo desktop doctor
root="$HOME/VMs/winhoo"
mkdir -p "$root"
winhoo desktop sources "$root/media"
winhoo desktop media --esd "$root/media/windows-pro-arm64.esd" \
  --sha256 c01b6f474c772093159b63fb3a168d2666556b223d288f67efc37c4fbaeddd25 \
  --output "$root/setup"
winhoo desktop create "$root/test" --setup "$root/setup" \
  --openssh "$root/media/OpenSSH-ARM64.zip" --netkvm "$root/media/NetKVM" \
  --account WinHooTest --ssh-port 2226
winhoo desktop up "$root/test" --wait
```

Use a new instance directory on a local volume, with at least 40 GiB free before creation. Prepare the setup tree once; each instance gets its own installer, UEFI variables, TPM state, host key and test login. The licensed Windows installation signs in its dedicated local test user at boot. The private directory includes the installer with its unattended credentials: do not share it or publish screenshots without inspecting them. The SSH client checks the per-instance host key from the first connection. Optional `--forward HOSTPORT:GUESTPORT` arguments are accepted at creation; all host listeners bind to IPv4 loopback. For a test that runs entirely inside a retained guest, use `winhoo desktop forwards "$root/test" --only-ssh` while stopped. This releases old host ports without replacing the Windows disk.

## Identifying the installed controller

Two locally built wheels can have the same package version and different
PowerShell scripts. Before a long run, compare the installed desktop
controller with the source revision selected for that run:

```sh
# From the clean checkout at the intended revision:
expected=$(python3 winhoo.py desktop identity | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha256"])')
winhoo desktop identity --expect-sha256 "$expected"
```

The command compares the launcher, desktop Python modules and bundled Windows
assets by actual bytes. It works without Git, QEMU or a running desktop; the
per-file hashes identify which component differs. Package version, paths,
build timestamps and bytecode caches do not affect the result. Use a separate
virtual environment for an update, and keep the absolute path to the selected
CLI for the entire run, including result collection and shutdown. This check
does not verify a signature, Windows installation or consumer payload.

## Daily use

Use `up --wait` when the next step needs a signed-in Windows desktop. The
controller holds the same lifecycle lock until it has observed the dedicated
user on the active, unlocked console. For service work on an installed desktop,
`up --wait transport --timeout 180` waits for pinned SSH and the guest identity
without requiring an unlocked console. A successful response includes the live
observation in `readiness`. A timeout leaves the guest available for inspection.
Plain `up` still returns as soon as QEMU starts: an immediate guest command
can fail during SSH startup, and an ACPI power-down request sent that early
can go unanswered.

```sh
winhoo desktop status "$root/test"
winhoo desktop up "$root/test" --wait --timeout 180
winhoo desktop inspect "$root/test" --timeout 15
winhoo desktop put "$root/test" ./sample.txt sample.txt
winhoo desktop exec "$root/test" --stdin <<'PS'
Get-Content "$env:USERPROFILE\sample.txt"
PS
winhoo desktop get "$root/test" sample.txt ./sample-from-guest.txt
winhoo desktop screenshot "$root/test"
winhoo desktop down "$root/test"
```

`status` returns the UUID, execution state, host/guest port pairs and installation phase. While a running WinPE owns the FAT receipt disk it may briefly return `receipt-unavailable`; a different owner is always an error. `wait` returns only after keyed SSH, the guest owner UUID and ARM64 architecture agree and the dedicated local account owns the active, unlocked physical console. A connected SSH service or a cached username is insufficient. During logon it waits for Windows to identify that console. If the same active console stays locked for 45 seconds of observations, it stops early with `console-locked`. Sign in normally on the test desktop before retrying. `inspect` reads Windows build, console user, available memory, playback endpoints and the PID listening on each declared guest port. Its `consoleSession` reports the live session ID, account, connection and lock state. It also reports active network profiles and matching inbound program block rules. A Windows service listening in the guest can still be unreachable through a host forward; inspect both sides before changing an application. Guest-local tests can return their results with `get`, which publishes a complete regular file and refuses to overwrite existing evidence. A GUI launcher can exit after handing off to a different process (observed with x64 Chrome on ARM64); have the consumer track the live owning process before relaunching. `inspect` includes the effective Windows PowerShell script policy and each policy scope. On the stock Windows client all scopes can be undefined while local script files are restricted. Report a dispatcher policy error immediately and leave user, machine and Group Policy settings intact. SSH commands run in a service session. Desktop applications must be started in the signed-in user's interactive session. During a long `exec`, you can still use `inspect` or retrieve a finished report with `get`. Another command, upload, or power transition is refused while the first command owns the guest. Keep GUI tests serialized; one guest has one foreground window. Inspect an ambiguous command timeout before submitting it again.

### Checking whether a long command still owns the guest

Run `winhoo desktop status "$root/test" --locks-only` from a second terminal.
This reads the instance UUID and two host kernel locks. It works while another
controller holds the QEMU monitor during shutdown, and opens neither a monitor
connection nor an SSH session. Its output contains only `uuid` and
`controllerLocks`. Omit the flag for the full status, including QEMU state and
the installation phase. An `exclusive` guest operation
with a `shared` lifecycle lock means that an `exec`, upload or archive
holds the guest; another writer or power transition would be refused.
A shared lifecycle lock by itself can be an observer. An exclusive
lifecycle lock can indicate startup, shutdown or recovery. Missing or
unlocked lock files report `free`; an unreadable lock reports `unavailable`.

These are observations at the instant each lock was read. They do not
establish guest progress, successful completion, or permission to retry a
timed-out command. For progress, read the original run log; for completion,
check its result and shutdown receipt.

## Installation and recovery

- WinPE applies Windows with DISM and installs the UEFI boot files with BCDBoot. Before partitioning, the installer compares nonempty markers on two separate volumes and requires one empty QEMU NVMe. It does not clear or repartition a retained Windows disk. The pinned WinPE has `find.exe`, but lacks `findstr.exe`.
- The FAT receipt records `identify-disk`, `partition-new-nvme`, `apply-windows`, `stage-guest-tools`, `install-uefi-boot` and `image-applied`. On failure inspect `failed.txt` and the corresponding logs with `mtype -i "$root/test/phases.img" ::failed.txt`. A successful WinPE shutdown hands the installed disk to the next boot.
- The emulated TPM stays enabled. Its optional PPI is disabled because it raised `HV_BAD_ARGUMENT` with HVF on the tested Mac. Windows gets a USB playback endpoint backed by silence; no host speaker or microphone access is needed.
- `down` asks Windows to shut down, waits for QMP state `shutdown`, then asks QEMU to exit. QMP can disconnect before acknowledging `quit`. Success requires the original process to be gone as well. An unavailable monitor with a live PID blocks another start. A timeout leaves the disk in place. Reuse the instance on the next run.
- WinHoo requests a macOS idle-sleep assertion tied to the actual QEMU PID. It releases when that process exits and does not keep the display on or change global power settings. To opt out for a new desktop, pass `--allow-idle-sleep` to `create`. `doctor` reports an available laptop lid sensor as open or closed. Keep the lid open during long runs; the idle-sleep assertion does not override closing it. Check `status` and the consumer's result after a suspend. An already-running process, a port that answers, and an old receipt do not by themselves prove that the interactive test passed.

### Guest-local applications

Check the actual Windows sockets before accepting a guest-local test service.
For example, `winhoo desktop inspect "$root/test" --require-loopback 8700`
fails if TCP 8700 is missing or if **any** of its listeners binds a non-loopback
address. The service port need not be forwarded to the host. An application
that relaunches through Windows UIAccess can lose its launcher's environment;
use installation state owned by the test instance to scope its listener, then
check the live IPv4 and IPv6 binds.

When dispatching a native program from Windows PowerShell, pass a single fixed
subcommand directly (for example `& $program status`). If building an argument
list, keep it an array across conditional pipelines: a single result can become
a scalar. Verify the program's actual argument count and exit code. In a desktop
test this distinction affected the single-selector full suite even though the
multi-argument diagnostic selector worked.

### Recovering a finished report

If Windows stops accepting new SSH sessions while a long test is still
writing progress, retain its attempt ID and let the original run finish. If
the final download failed, after a guest-confirmed shutdown run:

```sh
winhoo desktop collect "$root/test" finished-report.zip ./recovered-report.zip --timeout 180
```

This takes an exclusive lifecycle lock, boots the same installed disk,
attests the guest UUID, login and architecture through the pinned key, downloads the existing file and
confirms a fresh shutdown before publishing the local copy. An active guest,
a changed owner or an existing output file stops the operation. Collection
also works when the console is locked. It does not launch the test again or establish that it passed; verify its report
and attempt identity.

### Collecting a test result directory

Use `archive` when a test left a directory of reports, traces and attachments
on the installed disk. After its producer has closed the files, on the running
attested desktop:

```sh
winhoo desktop archive "$root/test" 'C:\ProgramData\MyRunner\attempts\example\results' attempt-example.zip --output ./attempt-example.zip
```

With `--output` WinHoo compares the length and SHA-256 of the staged local
file with the guest receipt before publishing the download. The source must be a
local result directory under the test profile or a specific application in
ProgramData. WinHoo uses the Windows tar shipped with the OS, changes to the
result directory before walking it, and publishes the ZIP in the test profile
only after it is complete. This handles long generated filenames that Windows
PowerShell's .NET ZIP helper can reject. Archive creation will refuse an
existing name or a concurrent guest writer. It never reruns the test. Keep
archives private until you have reviewed their contents. A ZIP already staged
on a stopped, cleanly shut down desktop can also be retrieved with `collect`.

If the ZIP and its JSON receipt are already on the host, inspect those files
before starting Windows again. Save the JSON output of `archive --output` or
of `collect --from-directory` when downloading an archive. For example:

```sh
winhoo desktop inspect-archive ./attempt-example.zip --receipt ./attempt-example.json --find trace.zip
```

The command checks the complete file size and SHA-256 against the guest receipt.
It lists matching filenames with their paths and uncompressed sizes; paths in the
ZIP are never extracted. Add `--expect-owner UUID` to bind the receipt to a known
desktop. This works after the guest is stopped or the local ZIP is renamed.
An archive verifies what the original attempt recorded. A later passing replay
has its own receipt and cannot replace an earlier failure.

If the desktop has **already shut down** and only the original directory exists:

```sh
winhoo desktop collect "$root/test" recovery-example.zip ./recovered-example.zip \
  --from-directory 'C:\ProgramData\MyRunner\attempts\example\results' --timeout 180
```

This holds the lifecycle lock from boot through shutdown, waits for the guest
transport rather than the GUI, creates a new archive, verifies the downloaded
bytes and publishes the local file only after the same guest has shut down.
A running QEMU process is not evidence that Windows SSH is ready.

### Checking which application has the keyboard

An active console and a responding browser debugging port do not identify the
foreground application. In a real screen reader language switch, F6 cycled
through Explorer's Start, Search and Desktop controls while Chrome was running.
Preserve the browser window when restarting an unrelated helper.

On a running, attested desktop use:

```sh
winhoo desktop foreground "$root/test"
# browser_pid is the Windows PID of the browser that owns the expected window.
winhoo desktop foreground "$root/test" --expect-pid "$browser_pid"
```

The command samples the unlocked physical console through a temporary,
limited task running as the dedicated test user. It reports the process name,
PID and session. It opens no window, sends no input, and does not read window
titles. It can run while a test owns the guest. A different PID, a locked
desktop, a session switch or a missing foreground window fails the check.
This describes the instant sampled; another application can take focus later.

### Replacing content inside a foreground application

Windows can keep the same foreground process while the application's native
focus changes. In a Windows 11 Chrome run, replacing a page that contained a
focused editor produced a document focus event followed by a menu focus
event. NVDA completed the requested mode toggle there, but was still in
focus mode. The intended text command had never been sent.

Use `foreground --expect-pid` when diagnosing which application owns the
keyboard. For a command that targets a new control, first focus that control
and verify its identity with the application's accessibility interface. If the
command works directly on the editor, a mode change on the preceding menu
adds an unrelated failure point. Preserve the failed attempt's events before
replaying; a second toggle can reverse the first.

A document operation can also fail after the browser has regained focus.
In a Windows NVDA run, leaving an embedded frame and replacing its parent
page was followed by an “unsupported in this document” response to native
selection. An unchanged isolated replay passed. For a test of selection
support, navigate to a fresh top-level HTML document, verify a named control
through the screen reader, then check the replies to both enabling and
disabling selection. Keep the original frame-exit test separate.

### Checking a native text or braille movement

Give the command a starting position with a distinct target on either side.
On the retained Windows 11 desktop, Chromium and NVDA exposed a textarea
containing three newline-separated strings as one reading unit. Moving to
the previous line left the caret unchanged. Sometimes the cursor blink
redrew the same braille frame, so checking for any of those strings could
pass even when the command had done nothing.

In the working probe, three editable block elements held “Alpha beta”,
“Gamma delta” and “Omega last”. The selection started four characters into
the middle block. Moving backward produced only “Alpha beta”; moving forward
from the same start produced only “Omega last”. In both cases the editable
caret was at offset zero in the target block. Capture the fresh display
response and the resulting selection separately. A focused browser process
alone establishes neither observation.
