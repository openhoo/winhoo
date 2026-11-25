# winhoo

A reusable **native Windows container** for automated software installation and testing. Start a disposable instance, copy an application in, install it silently, and operate it over real SSH and PowerShell.

Use the container for command-line applications, silent installers, and services. For software that needs an interactive console, `winhoo desktop` runs a separate [Windows 11 ARM64 desktop directly on an Apple Silicon Mac](docs/macos-desktop.md). Its disk stays on the Mac and does not require a container engine or another host.

### Windows desktop in Docker on x64 Linux or WSL2

For a workload that needs real Windows speech and keyboard focus, run a full Windows desktop in Docker with KVM. Build the general WinHoo image from this checkout:

```sh
docker build --platform linux/amd64 -t winhoo-desktop:0.4.0-amd64 -f desktop/Dockerfile.amd64 .
```

The image contains the VM host and its controller. The consumer supplies the Windows application and checks its output. The [Docker desktop guide](desktop/README.md) describes retained disks, private ports and guest management. The existing `winhoo desktop` command remains the native macOS backend.

## What's included

- Windows Server Core LTSC 2022, pinned to an exact Microsoft image digest.
- Microsoft OpenSSH for Windows, pinned to a verified archive checksum.
- A real local `winhoo` administrator account with public-key-only SSH. Its random password is generated inside the container and is never exported.
- A Python controller with no third-party runtime dependencies.
- File upload/download, PowerShell execution, logs, disposable runs, and repeatable derived images.
- Checksum-verified MSI/EXE installation with machine-readable results.

Private client keys stay on the controller. Each instance has a separate host-key volume. SSH host identity is obtained through the authenticated Docker connection and then pinned. Container ports are not published: remote SSH uses the Windows host as a jump host to the container's private NAT address.

## Requirements

The **container host** needs x64 Windows with a Windows Docker engine. Windows 11 Pro/Enterprise with Containers and Hyper-V enabled is supported by the supplied host helper. A compatible Windows Server engine can also be used. The controller needs Python 3.10+, Docker CLI, and OpenSSH; it can run on macOS, Linux, or Windows.

The default isolation mode is `hyperv`, including during image builds. `--isolation process` is available when the host/image compatibility matrix supports it. A Linux Docker engine, including local OrbStack on macOS, cannot execute this native Windows image.

## Run locally on an Apple Silicon Mac

`host/local-vm/` provisions a separate x64 Windows Server 2022 Core VM inside the local ARM64 Linux Docker engine. The VM provides the Windows kernel and runs the genuine `winhoo` Windows containers with **process isolation**. It uses software emulation, so installation and tests can be slow.

The supplied launcher requires at least **45 GiB free disk space** before creating the VM and **12 GiB assigned to the Linux Docker engine**. The VM uses 4 GiB RAM and two virtual CPUs, with an independent sparse 64 GiB virtual disk. `wait` monitors free disk space and stops its owned VM below 8 GiB. Keep `wait` running during the initial installation.

```bash
python3 host/local-vm/local_host.py --context orbstack doctor
python3 host/local-vm/local_host.py --context orbstack up
python3 host/local-vm/local_host.py wait --timeout 7200
```

The launcher downloads Windows Server and provisions the Containers feature, a pinned standalone Windows Docker Engine, and public-key-only SSH. A required reboot occurs **inside the guest VM**. The Mac is not restarted. Its storage and credentials are separate from other Windows guests.

The recovery viewer is bound to `127.0.0.1:8016`; host SSH is bound to `127.0.0.1:2228`. `wait` pins the host key from the instance-specific shared receipt, then checks the Windows engine over authenticated SSH. Create the context after this succeeds:

```bash
docker context create winhoo-local-host --docker host=ssh://winhoo-local-host

python3 winhoo.py --context winhoo-local-host \
  --host-ssh-config "$PWD/.local/local-host/ssh_config" doctor
python3 winhoo.py --context winhoo-local-host \
  --host-ssh-config "$PWD/.local/local-host/ssh_config" build --isolation process
python3 winhoo.py --context winhoo-local-host \
  --host-ssh-config "$PWD/.local/local-host/ssh_config" up dev --isolation process --timeout 900
python3 winhoo.py exec dev 'Get-ComputerInfo | Select-Object WindowsProductName, OsVersion'
```

`--host-ssh-config` applies the private SSH settings to both the Docker connection and the guest jump host. The instance remembers it. It does not edit global SSH configuration or change the active Docker context.

```bash
python3 tests/runtime_smoke.py --context winhoo-local-host \
  --host-ssh-config "$PWD/.local/local-host/ssh_config" --isolation process

python3 host/local-vm/local_host.py status
python3 host/local-vm/local_host.py stop
```

`stop` retains the VM disk and credentials; `up` resumes that owned VM. This local-host path is implemented but has not yet passed a Windows guest boot or native container runtime test. See the validation record for the current storage gate and exact checks completed.

## Prepare a Windows 11 host

Run these from an elevated Windows PowerShell in the checkout:

```powershell
# Applies only to this PowerShell process; no persistent policy change.
Set-ExecutionPolicy -Scope Process Bypass

# Read-only report; exit 1 means the host is not ready.
.\host\Test-WinhooHost.ps1

# Preview, then enable the required Windows features without restarting.
.\host\Prepare-WinhooHost.ps1 -EnableFeatures -WhatIf
.\host\Prepare-WinhooHost.ps1 -EnableFeatures
```

Exit `3010` means a Windows reboot is required. The helper does not restart the machine. After restarting, install the pinned all-users Docker Desktop with the Windows backend and start it:

```powershell
# Review the Docker Subscription Service Agreement before accepting it.
.\host\Prepare-WinhooHost.ps1 -InstallDocker -AcceptDockerLicense -StartDocker
.\host\Test-WinhooHost.ps1
```

The installer is checked against the official SHA256 and its Authenticode signature. The helper refuses to replace an existing Docker Desktop installation. Its final gate checks the local Windows engine rather than inheriting a remote Docker context.

## Connect from a Mac or Linux controller

Configure an existing, trusted SSH identity for the Windows host. For example, in your SSH config:

```sshconfig
Host winhoo-windows
    HostName YOUR_WINDOWS_HOST
    User YOUR_WINDOWS_USER
    IdentityFile ~/.ssh/YOUR_WINDOWS_HOST_KEY
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    BatchMode yes
```

Verify that `ssh winhoo-windows docker version` works with the host's existing verified host key, then create a Docker context:

```bash
docker context create winhoo-windows --docker host=ssh://winhoo-windows
python3 winhoo.py --context winhoo-windows doctor
```

On the Windows host itself, use its local named-pipe Docker context and omit `--context winhoo-windows`. No Docker API TCP listener is needed.

## Build, start, and operate

From the checkout:

```bash
python3 winhoo.py --context winhoo-windows build
python3 winhoo.py --context winhoo-windows up dev

python3 winhoo.py exec dev 'Get-ComputerInfo | Select-Object WindowsProductName, OsVersion'
python3 winhoo.py script dev examples/smoke.ps1
python3 winhoo.py ssh dev
python3 winhoo.py ssh dev --print-command

python3 winhoo.py put dev ./application C:/work/application
python3 winhoo.py get dev C:/winhoo/logs ./artifacts/installation-logs
python3 winhoo.py status dev
python3 winhoo.py logs dev
```

An instance remembers its Docker context, so later commands do not require `--context`. Supplying a conflicting context fails. Commands preserve stdout, stderr, and the PowerShell/native process exit code. Scripts are transported without depending on Windows' command-line length limit. `script` stages a standalone file under `C:\work`; upload a complete application directory with `put` when scripts depend on adjacent files.

`exec` and `script` run Windows PowerShell 5.1. Interactive `ssh` opens `cmd.exe`; enter `powershell.exe` for a PowerShell session.

`up` succeeds only after pinned, authenticated SSH executes a Windows PowerShell identity probe. Docker's own health check establishes the SSH service and protocol endpoint; it is a separate, weaker signal.

## Install an application silently

Upload the installer, obtain its expected SHA256 from a trusted source, and call the helper:

```bash
python3 winhoo.py put dev ./setup.msi C:/work/setup.msi
python3 winhoo.py exec dev '& C:/winhoo/container/Install-WinhooPackage.ps1 -Path C:/work/setup.msi -Type msi -Name my-app -Sha256 REPLACE_WITH_EXPECTED_SHA256'
```

For HTTPS downloads, replace `-Path` with `-Uri https://your-vendor.example/setup.exe` and use `-Type exe -Arguments '/your-vendors-silent-flag'`. EXE silent switches depend on the vendor; the helper does not guess them. MSI installation uses `/qn /norestart` and produces a verbose installer log.

Results and logs are written to `C:\winhoo\logs`. A nonaccepted exit code fails the command. Codes `3010` and `1641` are recorded as `reboot-required` and fail even if listed in `-AcceptExitCodes`: the installation has not been established as complete inside a container. Applications needing a desktop, a driver, or a machine reboot need a different test environment.

## Disposable tests and repeatable application images

```bash
# Fresh instance, execute the test, then remove its container, keys, and state.
python3 winhoo.py --context winhoo-windows run --file examples/smoke.ps1

# Remove a development container; retain its identity and SSH host-key volume.
python3 winhoo.py down dev

# Remove the development container, its owned key volume, and local state.
python3 winhoo.py down dev --purge
```

The filesystem inside a removed container is discarded. Persist application setup in a derived image for repeatable tests. `examples/Dockerfile` shows the derivation pattern:

```bash
docker --context winhoo-windows build --isolation hyperv -f examples/Dockerfile -t winhoo-example:0.1.0 .
python3 winhoo.py --context winhoo-windows run --image winhoo-example:0.1.0 '& C:/winhoo/examples/smoke.ps1'
```

Use the installer helper in a derived image's `RUN` instruction to bake a verified application install into that image. Keep the base entrypoint so every instance receives its own runtime account and keys. Never commit a running SSH-enabled container into a reusable image; that would capture its account and private keys.

## State and safeguards

The checkout's ignored `.local/` directory contains per-instance client keys, pinned host identities, and resource metadata. Change its location with `--state-dir`. Resources are matched by immutable container ID plus ownership labels and an instance UUID. A reused container name, changed endpoint, or changed SSH key is rejected.

The repository command is `python3 winhoo.py`. Install the wheel from [GitHub Releases](https://github.com/openhoo/winhoo/releases), or use an editable install (`python3 -m pip install -e .`) for the `winhoo` command. A standalone wheel does not include the container build context; use `build --source-dir /path/to/winhoo` with a checkout.

## Verification

GitHub Actions runs the controller and launcher unit tests on Linux with Python 3.10 and 3.14. This checks the automation code; native Windows container qualification uses the separate runtime smoke test below.

```bash
python3 -m unittest discover -s tests -v

# Real Windows host, no application installation: a temporary executable only returns exit codes.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/Test-WinhooInstaller.ps1

# Actual native container lifecycle, SSH, Unicode, file exchange and exit codes.
python3 tests/runtime_smoke.py --context winhoo-windows
```

See [the validation record](docs/validation.md) for what has actually run. Controller unit tests and host-side PowerShell checks do not establish that the Windows container boots or accepts SSH.

## Dependency updates and primary references

Exact versions, URLs, and hashes are recorded in `dependencies.json`. To update the base, verify a new official Microsoft digest and OpenSSH checksum, update the Dockerfile and lock together, then run the real container smoke test. The pinned OpenSSH release carries Microsoft's `Preview` suffix; this is a test base rather than a production server distribution.

- [Microsoft: application compatibility and unsupported GUI/RDP workloads](https://learn.microsoft.com/en-us/virtualization/windowscontainers/quick-start/lift-shift-to-containers)
- [Microsoft: host/image compatibility and isolation modes](https://learn.microsoft.com/en-us/virtualization/windowscontainers/deploy-containers/version-compatibility)
- [Docker: Windows host requirements and installer flags](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Microsoft OpenSSH for Windows](https://github.com/PowerShell/Win32-OpenSSH)
- [OpenSSH: ContainerAdministrator service-login issue](https://github.com/PowerShell/Win32-OpenSSH/issues/1725)

## License

MIT; see [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md). Downloaded operating systems and tools retain their own licenses.
