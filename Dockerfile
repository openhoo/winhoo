# escape=`
ARG WINDOWS_BASE_IMAGE=mcr.microsoft.com/windows/servercore:ltsc2022@sha256:b52799a67fa596d4bddcd82ae1dab0077a84767fbc33c7342b72b53ecc71ad09
FROM ${WINDOWS_BASE_IMAGE}

SHELL ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]

ARG OPENSSH_VERSION=10.4.0.0p1-Preview
ARG OPENSSH_SHA256=b67ff8becd203502190ec42ac7787c24c2cd3325ed7700485fe7a1c83d2a7957

LABEL org.opencontainers.image.title="winhoo" `
      org.opencontainers.image.description="Native Windows container base for unattended installation, SSH and PowerShell test execution"

USER ContainerAdministrator
COPY container/ C:/winhoo/container/
RUN $ErrorActionPreference = 'Stop'; `
    & C:/winhoo/container/Install-WinhooOpenSsh.ps1 -Version $env:OPENSSH_VERSION -Sha256 $env:OPENSSH_SHA256

WORKDIR C:/work
EXPOSE 22
HEALTHCHECK --interval=15s --timeout=8s --start-period=30s --retries=3 CMD ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "C:\\winhoo\\container\\Test-WinhooHealth.ps1"]
ENTRYPOINT ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "C:\\winhoo\\container\\Start-Winhoo.ps1"]
