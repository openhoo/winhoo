# Working on winhoo

The container commands target native Windows containers, silent installers, CLI applications and services. A Windows Server host VM can supply a container engine. The independent `desktop` command operates a Windows 11 desktop directly on Apple Silicon for interactive applications. Keep its lifecycle in WinHoo and application provisioning and assertions in the consumer. See docs/macos-desktop.md. For a Windows desktop on Linux Docker or WSL2, use the separate image and controller in desktop/README.md. Validate actual reader output in the consuming application.

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
