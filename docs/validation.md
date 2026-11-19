# Validation record

Date: **November 4, 2026**. This record separates controller tests, checks on a real Windows host, and tests requiring a real Windows container engine.

## Completed

| Check | Environment | Result | Evidence |
| --- | --- | --- | --- |
| Controller safety, trust, transport, encoding, lifecycle and cleanup | Local Python standard-library unittest runner with a stateful engine/SSH double, plus an actual Docker-to-SSH adapter subprocess using inert executables | All 34 controller tests passed | `tests/test_controller.py` |
| Local host launcher | Stateful Linux engine/SSH double | All 10 tests passed: storage/RAM gates, endpoint/ownership checks, receipt/key pinning, authenticated engine readiness, timeout/low-disk stop, retained identity and retry | `tests/test_local_host.py` |
| PowerShell syntax | Real Windows PowerShell `5.1.28000.3231` | All eight scripts parsed with zero errors | `artifacts/validation/windows-powershell-parser.json` |
| Local Windows Server host bootstrap | Real Windows PowerShell 5.1 parser and isolated child processes with Windows feature, reboot, and filesystem actions stubbed | Final script parsed with zero errors; seven reboot continuation cases passed | `artifacts/validation/windows-local-host-bootstrap-parser.json` |
| OEM batch entry point | Real Windows `cmd.exe` with a temporary inert PowerShell substitute | Exact bootstrap arguments and log redirection checked; exit codes `0` and `23` preserved | `artifacts/validation/windows-local-host-batch-fixtures.json` |
| SSH command exit handling | Trusted SSH session to the real Windows worker | Success `0`, native failure `23`, explicit PowerShell exit `37`, thrown error `1` preserved | `artifacts/validation/windows-exit-probes.json` |
| Exact streamed SSH transport | Same Windows host, using the controller's UTF-8 bootstrap | Unicode `Grüße, €, 日本語`, a 60 KB script, and three failure cases all passed | `artifacts/validation/windows-streamed-transport.json` |
| Final cmd login-shell transport | Real Windows `cmd.exe /c` running the controller-generated PowerShell command; only its working directory changed to existing `C:/Windows` | Unicode succeeded; native exit `23`, explicit exit `37`, and exception `1` preserved; final OpenSSH install script parsed with zero errors | `artifacts/validation/windows-cmd-shell-transport.json` |
| Installer result handling | Real Windows PowerShell 5.1 with a temporary executable that only returns requested exit codes | Six cases passed: wrong checksum, HTTP rejection, success `0`, failure `47`, reboot codes `3010`/`1641` rejected even when accepted explicitly | `artifacts/validation/windows-installer-fixtures.json` |
| Host preflight | Real current Windows worker | Correctly returned not ready and exit `1`; missing features/runtime listed below | `artifacts/validation/windows-host-report.json` |
| Dependency identity | Official Microsoft registry and OpenSSH release archive | Exact base digest resolved; official OpenSSH archive downloaded and SHA256 verified | `dependencies.json` |
| Standalone Windows Docker Engine | Official Docker binary archive | Version `29.9.3` downloaded and SHA256 verified; no installation performed | `dependencies.json` |
| Local Mac preflight | Actual local OrbStack Linux ARM64 engine | Correctly refused a new local Windows host with 24.7 GiB free; starting threshold is 45 GiB | `host/local-vm/local_host.py doctor` |

The installer harness substitutes only its log/download directories into a fresh temporary directory. It executes no application installer and changes no host service, Windows feature, or persistent execution policy. Its temporary files are removed. Raw evidence is local and ignored by Git.

Review also fixed a Windows-specific SSH identity issue: OpenSSH's service cannot treat the special `ContainerAdministrator` identity as a normal local SAM user. Startup creates the real `winhoo` local administrator instead. This correction is supported by upstream source and the original container report; runtime acceptance remains pending.

The final SSH login shell is `cmd.exe /c`, and the controller explicitly starts PowerShell. A real Windows reproduction showed that an outer PowerShell login shell replaced a failing native child's exit `23` with `1`. The corrected transport preserved all tested exit codes. These host-side probes still do not establish container boot or authentication.

## Current host state

Host-side checks used a private Windows 11 worker over an existing trusted SSH connection. Its host-key pin was verified without replacement. The worker was used only for parser checks, command transport, and temporary inert fixtures; no Windows feature, account, service, registry, or SSH configuration changes were applied.

- Windows 11 Pro, x64, build `28000`.
- `Containers`: disabled.
- `Microsoft-Hyper-V-All`: disabled.
- Hypervisor reported present, but the Windows optional features needed here are absent.
- Docker CLI and Windows Docker engine: not installed.

The local Mac uses an ARM64 Linux OrbStack engine, which cannot execute the native Windows image. An existing separate Windows guest belongs to another active workflow.

## Local Windows container host on the Mac

`host/local-vm/` now contains a separate Windows Server 2022 Core host launcher and unattended provisioning scripts. It uses an x64 guest under software emulation, then the native Windows Docker Engine with process isolation. No nested Hyper-V is required by this design. The `winhoo` workload remains a genuine Windows container.

The local engine reports Linux/aarch64, 16 CPUs, and 16,818,020,352 bytes of assigned RAM. Its RAM gate passes. Filesystem free space fell during preparation; the actual launcher preflight returned **24.7 GiB free**, below the conservative **45 GiB** initial threshold. No local host state, disk volume, VM, or Windows Docker context was created.

A read-only cache audit found approximately **16.5 GiB** of old, unused, immutable, unshared build cache. Recovering that amount would still leave the latest free-space snapshot below the starting threshold. No build cache, images, containers, or volumes were pruned. The unrelated existing Windows guest was not changed.

The launcher and host bootstrap remain **runtime-unqualified** until the separate Windows Server guest actually installs and its engine passes authenticated readiness. Controller tests and PowerShell parsing do not prove that path.

## Still required

No native `winhoo` image has been built or started yet. The following claims require actual Windows engine evidence:

1. Provide enough local storage, then run `host/local-vm/local_host.py up` and `wait` to install the separate Windows Server container host and pass its authenticated Windows engine check.
2. Create the `winhoo-local-host` Docker context and build `winhoo:0.1.0` with the private host SSH config and `--isolation process`, as documented in README.md.
3. Run `python3 tests/runtime_smoke.py --context winhoo-local-host --host-ssh-config .local/local-host/ssh_config --isolation process` to verify native container boot, authenticated guest SSH, UTF-8 scripts, file transfer, exit codes, recreation with retained host identity, and resource cleanup.
4. Qualify a real application's silent installation and behavior in a derived image.

The Windows 11 fallback setup remains prepared in `host/Prepare-WinhooHost.ps1`. It has not been applied: no optional host feature changes, Docker Desktop installation/license acceptance, or worker reboot have been performed.

## Apple Silicon Windows desktop (2026-11-18)

On a local M4 Max (macOS 26.6.2, QEMU 11.1.3, HVF), the controller deployed Windows 11 Pro ARM64 from the pinned 26100.4349 media, observed the WinPE handover and authenticated the dedicated console user after a cold boot. A consumer uploaded its pinned packages over the installed CLI, completed provisioning and confirmed guest shutdown. A later boot on the retained disk exposed a Windows playback endpoint (`Speakers (Audio Device)`, PnP status `OK`). The host received successful HTTP responses from the consumer's two loopback forwards. These are desktop lifecycle and transport observations; the consumer records its own application test results.

`python -m unittest discover -s tests -v` includes host regressions for the per-instance monitor, ownership changes, transient FAT reads, cached media corruption and early QMP disconnect. Host tests do not exercise the Windows installer. Do not publish the private instance directory or the unattended installer as validation artifacts.

The guest-local consumer subsequently passed its core block (four selected cases including NVDA speech and braille) and the previously failing actions-03 block (two selected cases). These were separate attempts. WinHoo retrieved each guest's reports over pinned SSH and confirmed Windows shutdown. A live read-only inspection during the second attempt reported the active Public profile, Windows script policy and the listening processes without interrupting the test. Removing the two old application forwards on the stopped guest preserved its disk, UUID and SSH port. The host suite now contains 56 tests.

A later inspection over a second authenticated SSH connection ran during a
native GUI matrix. On the local Windows 11 ARM64 guest it reported physical
console session 1, the dedicated local account, connection `active` and lock
state `unlocked`. The two consumer application ports were bound to loopback.
The host regressions replay a locked console with the correct username, a
changed domain, missing state and a subsequent unlock. They also check the
45-second cutoff and transport-only recovery. The actual VM remained unlocked
during the observation.

The Windows PowerShell .NET ZIP helper failed on a real Playwright result
directory with a generated attachment path longer than 260 characters. On
the same retained Windows 11 ARM64 disk, `desktop archive` retrieved the
original result, including its trace and video: 16 ZIP entries, a longest
source path of 267 characters and 20,027,477 archived bytes. Every entry
matched an independent extraction, and the downloaded ZIP matched the
SHA-256 reported by Windows. The test was not repeated to recover these files.

A subsequent failed GUI block left three tests' traces on the stopped disk.
An immediate archive after QEMU startup failed during the SSH banner exchange.
`collect --from-directory` waited for the authenticated transport, downloaded
2,927,422 bytes and recorded guest-confirmed shutdown. All 50 entries matched
an independent `archive --output` of the same source. Both downloads matched
their own guest SHA-256. The host suite includes rejection of altered or
truncated downloads without publishing a partial file (66 tests passed).

The combined `desktop up --wait` command was added after a cold boot
returned before Windows accepted SSH. The host suite (69 tests) checks that
the boot and readiness probe share the same exclusive lock, distinguishes an
unlocked console from authenticated transport, and rejects invalid timeouts
before boot. The locked-console fixture also verifies that a failed wait leaves
the running guest available for inspection.

On the retained Windows 11 ARM64 desktop, `up --wait` reported the
dedicated user on the active, unlocked physical console; `up --wait transport`
also returned the authenticated guest identity. The first foreground sample
identified Explorer in session 1, and a later sample identified Chrome after
it started. Requiring Chrome's PID passed. Requiring Explorer's old PID failed
and named Chrome. An independent process query confirmed Chrome's PID and
session; no probe task or temporary directory remained. One separate SSH probe
timed out during startup and produced no usable sample.

The Windows CI job compiles and executes the shipped probe and verifies the
GUI PE subsystem. The host suite checks stale and locked sessions, wrong PIDs,
and ambiguous SSH timeouts (72 tests). An installed wheel contains both the
dispatch script and the C# source.

During a real NVDA matrix on the retained Windows 11 ARM64 guest, a second
host process ran the new `desktop status` against the existing controller.
It reported a running VM, a shared lifecycle lock and an exclusive guest
operation lock. No guest command or controller upgrade was needed. The
macOS suite (73 tests) also checks idle, observation-only and power-transition
states against real kernel locks and that status creates no lock files.

The optional `desktop status --locks-only` returned the same shared lifecycle
and exclusive guest operation locks during a later NVDA matrix on the Mac.
The running controller was unchanged. A separate macOS test holds the
exclusive shutdown lock and verifies the query returns without consulting
QMP, the installation media or a guest receipt (74 host tests passed).

With a host-side fixture holding the shutdown lock and accepting a monitor
connection without sending the QMP greeting, full status timed out after
4.07 s. The installed `--locks-only` controller reported the exclusive lock
in 0.057 s and did not queue a second connection. This measures the host
observation, not Windows shutdown time.

The source checkout and a freshly installed wheel returned the same
`desktop identity` for all 11 shipped desktop and launcher files. Changing
only the installed foreground PowerShell script in a disposable copy made
`--expect-sha256` exit 1. The file list identified that one difference.
No VM was started for the package comparison; 75 macOS host tests passed.

On November 19, 2026, `collect --finish-running-owner` recovered a completed
Playwright result directory from a running, dedicated Windows 11 ARM64 desktop
on Apple Silicon. The archive contained 14 files (815,412 bytes); its downloaded
SHA-256 matched the guest receipt. Offline inspection found the original reader
flow, and WinHoo confirmed guest shutdown. The underlying test had failed.
The macOS host suite (79 tests) also checked refusal of other owners and that
a failed download leaves an adopted desktop running.

## Local Windows 11 ARM64: acquiring the test window (November 19, 2026)

On build 26100, the dedicated user's taskbar was activated to put Explorer
in front of a running Chrome for Testing window. `desktop foreground` identified
Explorer. `focus-window` given the live browser PID and start time returned
`caption-click`. An independent foreground probe then identified the same Chrome
PID. A second activation returned `already-active`. A start time off by one
tick and a process in the service session were both refused before activation.

## Windows-confirmed shutdown on ARM64 (November 19, 2026)

After successive QMP power-button shutdowns, Windows logged Kernel-Power 41 and
EventLog 6008 at the next boot, even though the old controller had observed the
emulator stop. On the same installed VM, an authenticated `shutdown.exe /s /t 0`
returned success; QMP reported `guest=true, reason=guest-shutdown` after 8.9
seconds. At the next boot Windows recorded the shutdown request (1074), event
log stop (6006), and kernel shutdown (13), with no new 41 or 6008. The updated
`desktop down` also completed on this guest with `method=windows-shutdown`.

### November 19: Foreground samples on a cold Windows 11 ARM64 desktop

The previous controller stopped waiting after 12 seconds. In a controlled
Windows test the original delayed receipt arrived at 15.373 seconds. The
installed controller accepted that one receipt. In a separate test with real
Chrome, Windows confirmed the caption activation after 1.711 seconds; a
passive observation returned the same foreground PID after 8.509 seconds.
These timings cover the wait after dispatch, not the full command or a
screen reader test. Windows Task Scheduler history was disabled on this guest,
so the earlier timeout cannot be attributed to a specific scheduler event.

### November 19: Diagnosing a pause from the Mac

With a Windows ARM64 qualification still holding the guest, the previous
`desktop doctor` showed an open lid. The host had already resumed. Both
`desktop doctor` and full `desktop status` now reported the kernel's last
sleep at 14:04:10 UTC and wake at 14:10:11 UTC. These matched
`kern.sleeptime` and `kern.waketime` on that Mac. The VM remained running
with its guest operation held throughout the observation. This check read
host state; it did not start another guest command. The 84 controller tests
passed on macOS, including independent handling of missing events.

### November 19: Detached matrix submission and recovering a guest result

On macOS, a disposable fixture was submitted to the actual user launchd
domain. It received the intended source commit, desktop and evidence path;
a second submission under a different directory was refused before dispatch.
This host test did not boot Windows. All 88 controller tests passed on the Mac.

On a real earlier NVDA run for which the original host process had exited,
the status reader found 28 logged passes and the incomplete German desktop
actions-08 block. The recovered guest result matched the attempt, owner and
input bundle and contained exit code 1. The host receipt still said
`starting`. The command reported `guest-reported-failure` and did not turn
the partial evidence into a passing matrix.

### November 19: Power source before a local matrix

The host power log recorded `Low Power Sleep` at 1% charge. The Mac woke
213 seconds later when power was connected. The native job had completed
14 blocks; the next screen reader request timed out. Windows subsequently
confirmed shutdown. This attempt did not qualify the candidate.

The submission regression rejects battery, UPS and unrecognized power
sources before reserving a desktop. A launchd fixture on simulated AC verifies
the recorded source; all 89 controller tests passed on macOS. A separate
submission on the real Mac recorded `AC Power` and started a new job.

### November 19: Handing a verified running desktop to the matrix

After a cold foreground request timed out, Windows confirmed shutdown.
On the next boot the owner and unlocked console were verified. A passive
foreground observation took 4.71 seconds, and confirming the target Chrome
window took 3.22 seconds. The job accepted that running owner's UUID and
completed its first native block without another boot. This is a handoff
check; the remaining matrix and shutdown are separate evidence. All 90
controller tests passed on macOS, including rejection of a different owner,
busy locks and a stopped VM.

The same local matrix had reported 15 completed blocks while its host receipt
still said `starting`. Reading its existing evidence with the updated status
command returned `matrix-started`, the current block and the original receipt
state without contacting Windows. All 91 controller tests passed on macOS.

### Offline NVDA matrix verification

The installed verifier read an existing Windows 11 ARM64 NVDA run from its
original guest ZIP and host receipts. The four locale/layout combinations
contained 40 complete blocks, 92 selected passing tests and zero retries;
shutdown was guest-confirmed. This historical run predates embedded per-report
block metadata. It exercises the offline verification path, not a fresh guest
boot. Host fixtures additionally cover a wrong selected case with unchanged
statistics, a hidden retry, a substituted block or attempt, missing original
reports, an incorrect source identity and incomplete shutdown.
