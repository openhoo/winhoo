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
