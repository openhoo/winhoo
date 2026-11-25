#!/usr/bin/env bash
set -Eeuo pipefail
upstream_umask=$(umask)
umask 077
mkdir -p /run/winhoo-control /run/winhoo-oem
chmod 700 /run/winhoo-control
# Build a generic OEM bootstrap without changing the consumer's mounted files.
if [ -d /oem ]; then cp -a /oem/. /run/winhoo-oem/; fi
if [ -f /run/winhoo-oem/install.bat ]; then
  mv /run/winhoo-oem/install.bat /run/winhoo-oem/consumer-install.bat
fi
cp -a /usr/share/winhoo/guest-tools /run/winhoo-oem/winhoo-tools
cp /usr/share/winhoo/guest-tools/oem-install.bat /run/winhoo-oem/install.bat
export QMP='unix:/run/winhoo-control/qmp.sock,server=on,wait=off'
export ARGUMENTS='-chardev socket,path=/run/winhoo-control/qga.sock,server=on,wait=off,id=winhoo-qga -device virtio-serial-pci,id=winhoo-serial -device virtserialport,chardev=winhoo-qga,name=org.qemu.guest_agent.0'
# The upstream DNS daemon drops privileges and must read its resolver backup.
# Restrict our control directory, then restore the caller's mask for upstream.
umask "$upstream_umask"
exec /run/entry.sh "$@"
