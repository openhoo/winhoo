#!/usr/bin/env bash
set -Eeuo pipefail
arch=$(dpkg --print-architecture)
apt-get update
apt-get install -y --allow-downgrades --no-install-recommends \
  libfuse3-4=3.17.2-3 libkmod2=34.2-2 \
  libsystemd-shared=257.13-1~deb13u1 libudev1=257.13-1~deb13u1 \
  python3-guestfs=1:1.54.1-2+deb13u1 \
  supermin=5.2.2-6+b3 \
  "linux-image-${arch}=6.12.118-1" \
  ntfs-3g=1:2022.10.3-5+deb13u3
if [ "$arch" = arm64 ]; then
  apt-get install -y --allow-downgrades --no-install-recommends qemu-system-arm=1:10.0.13+ds-0+deb13u1
fi
mkdir -p /usr/share/winhoo/guest-tools/vioserial
curl --fail --location --retry 3 --output /usr/share/winhoo/guest-tools/qemu-ga-x86_64.msi \
  https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-qemu-ga/qemu-ga-win-110.2.3-2.el10/qemu-ga-x86_64.msi
echo '19dcf8abc30f70c2eb6e87282601fd0476e4e0b19936f63d9c80be30db0796ed  /usr/share/winhoo/guest-tools/qemu-ga-x86_64.msi' | sha256sum --check --strict
test "$(stat -c %s /usr/share/winhoo/guest-tools/qemu-ga-x86_64.msi)" = 11909632
# Resolve archive-internal hard links while writing only the three allowlisted
# files. Windows-version directories share links across multiple driver trees.
python3 - <<'PY'
from pathlib import Path
import tarfile
with tarfile.open('/var/drivers.txz') as archive:
    for name in ('vioser.inf', 'vioser.sys', 'vioser.cat'):
        data = archive.extractfile('./vioserial/w11/amd64/' + name).read()
        Path('/usr/share/winhoo/guest-tools/vioserial', name).write_bytes(data)
PY
rm -rf /var/lib/apt/lists/*
# A single exact replacement fails the build if the pinned upstream changes.
python3 - <<'PY'
from pathlib import Path
p = Path('/run/install.sh')
s = p.read_text()
old = 'local folder="/oem"'
assert s.count(old) == 1, 'upstream OEM layout changed'
p.write_text(s.replace(old, 'local folder="/run/winhoo-oem"'))
PY
