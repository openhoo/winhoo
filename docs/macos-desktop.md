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
winhoo desktop up "$root/test"
winhoo desktop wait "$root/test"
```

Use a new instance directory on a local volume, with at least 40 GiB free before creation. Prepare the setup tree once; each instance gets its own installer, UEFI variables, TPM state, host key and test login. The licensed Windows installation signs in its dedicated local test user at boot. The private directory includes the installer with its unattended credentials: do not share it or publish screenshots without inspecting them. The SSH client checks the per-instance host key from the first connection. Optional `--forward HOSTPORT:GUESTPORT` arguments are accepted at creation; all host listeners bind to IPv4 loopback. For a test that runs entirely inside a retained guest, use `winhoo desktop forwards "$root/test" --only-ssh` while stopped. This releases old host ports without replacing the Windows disk.

## Daily use

```sh
winhoo desktop status "$root/test"
winhoo desktop up "$root/test"
winhoo desktop wait "$root/test" --timeout 180
winhoo desktop inspect "$root/test" --timeout 15
winhoo desktop put "$root/test" ./sample.txt sample.txt
winhoo desktop exec "$root/test" --stdin <<'PS'
Get-Content "$env:USERPROFILE\sample.txt"
PS
winhoo desktop get "$root/test" sample.txt ./sample-from-guest.txt
winhoo desktop screenshot "$root/test"
winhoo desktop down "$root/test"
```

`status` returns the UUID, execution state, host/guest port pairs and installation phase. While a running WinPE owns the FAT receipt disk it may briefly return `receipt-unavailable`; a different owner is always an error. `wait` returns only after keyed SSH, the guest owner UUID, ARM64 architecture and console user agree. `inspect` reads Windows build, console user, available memory, playback endpoints and the PID listening on each declared guest port. It also reports active network profiles and matching inbound program block rules. A Windows service listening in the guest can still be unreachable through a host forward; inspect both sides before changing an application. Guest-local tests can return their results with `get`, which publishes a complete regular file and refuses to overwrite existing evidence. A GUI launcher can exit after handing off to a different process (observed with x64 Chrome on ARM64); have the consumer track the live owning process before relaunching. `inspect` includes the effective Windows PowerShell script policy and each policy scope. On the stock Windows client all scopes can be undefined while local script files are restricted. Report a dispatcher policy error immediately and leave user, machine and Group Policy settings intact. SSH commands run in a service session. Desktop applications must be started in the signed-in user's interactive session. During a long `exec`, you can still use `inspect` or retrieve a finished report with `get`. Another command, upload, or power transition is refused while the first command owns the guest. Keep GUI tests serialized; one guest has one foreground window. Inspect an ambiguous command timeout before submitting it again.

## Installation and recovery

- WinPE applies Windows with DISM and installs the UEFI boot files with BCDBoot. Before partitioning, the installer compares nonempty markers on two separate volumes and requires one empty QEMU NVMe. It does not clear or repartition a retained Windows disk. The pinned WinPE has `find.exe`, but lacks `findstr.exe`.
- The FAT receipt records `identify-disk`, `partition-new-nvme`, `apply-windows`, `stage-guest-tools`, `install-uefi-boot` and `image-applied`. On failure inspect `failed.txt` and the corresponding logs with `mtype -i "$root/test/phases.img" ::failed.txt`. A successful WinPE shutdown hands the installed disk to the next boot.
- The emulated TPM stays enabled. Its optional PPI is disabled because it raised `HV_BAD_ARGUMENT` with HVF on the tested Mac. Windows gets a USB playback endpoint backed by silence; no host speaker or microphone access is needed.
- `down` asks Windows to shut down, waits for QMP state `shutdown`, then asks QEMU to exit. QMP can disconnect before acknowledging `quit`. Success requires the original process to be gone as well. An unavailable monitor with a live PID blocks another start. A timeout leaves the disk in place. Reuse the instance on the next run.
- On a laptop, keep the Mac awake with its lid open during long runs. Check `status` and the consumer's result after a suspend. An already-running process, a port that answers, and an old receipt do not by themselves prove that the interactive test passed.
