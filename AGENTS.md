# Working on winhoo

The user's selected scope is native Microsoft Windows containers, SSH, silent installers, command-line applications and services. There is no interactive desktop/RDP support. The workload must remain a native Windows container. A separate local Windows Server VM may provide its Windows container host on macOS; that host VM is not the winhoo workload.

- Read README.md for the supported host and controller transports.
- A native Windows engine is necessary for build and runtime verification. A Linux Docker engine on macOS cannot supply that evidence.
- Keep dependencies pinned in Dockerfile and dependencies.json together.
- Runtime creates the real local SAM user `winhoo`. ContainerAdministrator has special identity semantics and must not be used for service-based SSH login.
- Keep the SSH login shell `cmd.exe /c`. The controller explicitly starts PowerShell; an extra outer PowerShell shell would replace failing native exit codes with `1`.
- Keep client private keys outside images; generate host private keys only at runtime.
- Preserve strict host verification, private NAT transport, authenticated readiness and ownership checks before changing lifecycle behavior.
- Local verification: `python3 -m unittest discover -s tests -v`.
- The real runtime gate is `python3 tests/runtime_smoke.py --context YOUR_WINDOWS_CONTEXT`.
- Record parser checks, mocked controller tests, host-side behavior and actual container evidence separately in docs/validation.md.
