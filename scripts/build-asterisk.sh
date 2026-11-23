#!/bin/bash
# Full Asterisk + patched chan_dongle build in a disposable Debian container only:
# docs/building.md, "Full build in a disposable Debian 13 container" (arm64 or amd64).
set -euo pipefail
cd /work
test ! -e /work/src
test ! -e /work/stage
mkdir -p /work/tmp
export TMPDIR=/work/tmp
exec > >(tee /work/build.log) 2>&1
set -x

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ca-certificates git build-essential autoconf automake autotools-dev pkg-config \
  libedit-dev libjansson-dev libsqlite3-dev uuid-dev libxml2-dev libncurses-dev \
  libssl-dev bzip2 wget patch python3 file
cat /etc/os-release
uname -m
dpkg-query -W > /work/packages.txt
cc --version

bash /repo/scripts/prepare-sources.sh /work/src
# shellcheck disable=SC1091
source /repo/asterisk/sources.lock
git --no-pager -C /work/src/asterisk rev-parse HEAD
git --no-pager -C /work/src/chan-dongle rev-parse HEAD
cat /work/src/applied-sources.sha256

cd /work/src/asterisk
./configure --libdir=/usr/lib --with-pjproject-bundled --with-jansson-bundled=no
make menuselect.makeopts
# Keep binaries portable across target CPUs, including builds under emulation.
menuselect/menuselect --disable BUILD_NATIVE menuselect.makeopts
make -j4
make install DESTDIR=/work/stage
make samples DESTDIR=/work/stage
# A native unit instead of make config's SysV script (/etc/init.d, /etc/default): Asterisk has no make
# target for its systemd unit, so the upstream contrib/systemd/asterisk.service is copied unchanged and
# Phonelet's differences, each with its reason, are in one drop-in.
install -D -m 0644 contrib/systemd/asterisk.service /work/stage/usr/lib/systemd/system/asterisk.service
install -D -m 0644 /repo/asterisk/systemd/asterisk.service.d/10-phonelet.conf \
  /work/stage/usr/lib/systemd/system/asterisk.service.d/10-phonelet.conf
test ! -e /work/stage/etc/init.d
test ! -e /work/stage/etc/default
# Type=simple in the drop-in assumes no sd_notify support.
if grep -qx '#define HAVE_SYSTEMD 1' include/asterisk/autoconfig.h; then
  echo 'Asterisk was configured with libsystemd: the drop-in Type=simple no longer fits' >&2
  exit 1
fi

cd /work/src/chan-dongle
./bootstrap
test -f config.guess
test -f config.sub
./configure --with-astversion="$ASTERISK_VERSION" --with-asterisk=/work/src/asterisk/include \
  DESTDIR=/usr/lib/asterisk/modules
make -j4
make check
test -s chan_dongle.so
install -m 0755 chan_dongle.so /work/stage/usr/lib/asterisk/modules/chan_dongle.so
file /work/stage/usr/sbin/asterisk /work/stage/usr/lib/asterisk/modules/chan_dongle.so

cd /work
ls /work/stage/usr/lib/asterisk/modules > /work/modules.txt
# What the archive installs (regular files and symlinks, itself included), installed with it:
# docs/operations.md, "Removing Phonelet". An upgrade replaces it with the new build's list.
MANIFEST=usr/share/phonelet/asterisk.files
{ find /work/stage \( -type f -o -type l \) -printf '/%P\n'; echo "/$MANIFEST"; } | LC_ALL=C sort > /work/asterisk.files
install -D -m 0644 /work/asterisk.files "/work/stage/$MANIFEST"
ARCHIVE="asterisk-$ASTERISK_VERSION-$(dpkg --print-architecture).tar.gz"
tar -C /work/stage -czf "/work/$ARCHIVE" .
sha256sum "$ARCHIVE" stage/usr/sbin/asterisk \
  stage/usr/lib/asterisk/modules/chan_dongle.so > /work/artifacts.sha256
cat /work/artifacts.sha256
