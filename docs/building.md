# Building and testing

Phonelet uses pinned Asterisk and chan_dongle sources. Build the driver together with
the matching Asterisk headers; an arbitrary distribution module is not a substitute.
Installation, configuration and upgrades are covered in [install.md](install.md) and
[operations.md](operations.md). The runtime design is in [architecture.md](architecture.md).

## Source inputs

[asterisk/sources.lock](../asterisk/sources.lock) fixes:

| Component | Version / commit |
|---|---|
| Asterisk | 20.22.0 / `7c225dc47bb43bf9862b2b147ed565b7b6113fed` |
| chan_dongle | `7db8e75bc2b7c81b173e1bb465e376e64a70319a` |

From the repository root:

```sh
bash scripts/prepare-sources.sh /tmp/phonelet-sources
```

The destination must not exist. The script fetches and verifies both Git revisions,
applies the six patches in [series](../chan_dongle_patch/series) order and writes
`applied-sources.sha256` with hashes of the lock, series and patch files. It prepares
sources only: it does not build, install or restart services. It stops when a patch applies
only at an offset (`git apply` never fuzzes); after changing a patch, use a fresh tree.
The [patch README](../chan_dongle_patch/README.md) explains each patch.

## Full build in a disposable Debian 13 container

The build entry point is `scripts/build-asterisk.sh`, run inside a disposable Debian
13 container with the checkout at `/repo` and an empty writable build directory at
`/work`. It prepares its own sources; do not copy the tree from the preceding example
into `/work/src`. Both `/work/src` and `/work/stage` must be absent at the start.

Use rootless Podman on the development host. On an amd64 host, building arm64 requires
qemu-user emulation and working binfmt registration for arm64 containers. Arrange that
host prerequisite before running the build. `--platform` selects the container
architecture; this is a native build inside that environment, not a compiler target
flag. Do not build on the running gateway.

Run from a checkout with no secrets or private runtime data: a read-only mount is
still readable by the container. The build needs network access for the Debian image,
packages, pinned source fetches and bundled dependencies. Pass no modem devices,
service sockets or host networking. Package installation affects only the disposable
container; staged installation goes into `/work/stage`.

arm64:

```sh
mkdir -p "$PWD/.test-build/arm64"
podman run --rm --platform linux/arm64 --security-opt label=disable --memory 3g \
  -v "$PWD:/repo:ro" \
  -v "$PWD/.test-build/arm64:/work" \
  docker.io/library/debian:13 bash /repo/scripts/build-asterisk.sh
```

amd64, using a separate empty output directory:

```sh
mkdir -p "$PWD/.test-build/amd64"
podman run --rm --platform linux/amd64 --security-opt label=disable --memory 3g \
  -v "$PWD:/repo:ro" \
  -v "$PWD/.test-build/amd64:/work" \
  docker.io/library/debian:13 bash /repo/scripts/build-asterisk.sh
```

The bind source paths must be absolute paths visible to the Podman host. The SELinux
option avoids relabeling the checkout. A failed or completed build directory is not
fresh input for another run; use a new directory.

The full build configures Asterisk with `/usr/lib` as its library directory, bundled
PJPROJECT and system Jansson, disables `BUILD_NATIVE`, builds and stages Asterisk and
sample configuration, then builds the patched chan_dongle against those configured headers
and runs its upstream `make check`. Disabling `BUILD_NATIVE` avoids selecting CPU-specific
instructions from the build/emulation host. Instead of `make config`'s SysV init script, the
stage gets the upstream `contrib/systemd/asterisk.service` unchanged as
`usr/lib/systemd/system/asterisk.service` (Asterisk has no make target for it) and
Phonelet's differences in the drop-in
[10-phonelet.conf](../asterisk/systemd/asterisk.service.d/10-phonelet.conf); see
[Asterisk service](operations.md#asterisk-service).

The output under `/work` (the host's chosen output directory) includes:

- `asterisk-VERSION-ARCH.tar.gz`, containing the staged installation, including
  `usr/sbin/asterisk`, `usr/lib/asterisk/modules/chan_dongle.so`, the systemd unit and
  `usr/share/phonelet/asterisk.files`, the sorted list of every regular file and symlink
  in the archive (itself included; used by [removal](operations.md#removing-phonelet));
  `ARCH` is `arm64` or `amd64`. The same list is kept as `asterisk.files`.
- `artifacts.sha256`, with checksums of the archive, staged Asterisk executable and
  driver module.
- Build evidence: `build.log`, `packages.txt`, `modules.txt` and
  `src/applied-sources.sha256`.

Keep the exact repository revision and any uncommitted diff with the build evidence.
Record the image digest as well:

```sh
podman image inspect --format '{{.Digest}}' docker.io/library/debian:13
```

Pinned source commits do not promise bit-for-bit reproducible binaries: the image tag,
package index and dependency downloads can change. A successful build requires the
whole preparation/configuration/compilation/check sequence to exit zero; an old `.so`
in the output directory does not establish success.

The pinned build 4 (`asterisk-20.22.0-4`) runs on the arm64 reference board; calls and SMS were tested
on hardware with build 3. Build 4 uses the same Asterisk and chan_dongle sources as build 3; only the systemd drop-in (comments, ordering after
`time-set.target`, `UMask=0077`) and patch-header license lines changed. Providing a build recipe does not claim runtime validation.

## Release archives

Release order for maintainers:

1. Build both architectures from the same revision, as in the next paragraphs.
2. Commit their sums and the release URLs into [artifacts.lock](../asterisk/artifacts.lock).
3. Publish the release `asterisk-VERSION-N` with both archives, their sums and build records, and
   the source tarball; check that `scripts/fetch-asterisk.sh` downloads and verifies each archive.
4. Tag the application release `vX.Y.Z` on a revision that includes that lock.
5. If a build was committed before its release, drop the "not published yet" sentences written
   for it: the comment in `asterisk/artifacts.lock`, [Get the Asterisk archive](install.md#get-the-asterisk-archive)
   and the target guides.

Maintainers: after each build and a clean Debian 13 container check that the archive starts
Asterisk and loads chan_dongle, attach `asterisk-VERSION-ARCH.tar.gz` to the release
`asterisk-VERSION-N` (`N` counts builds of that Asterisk version). For each architecture also
attach `artifacts.sha256`, `build.log`, `packages.txt`, `modules.txt` and
`src/applied-sources.sha256`, renamed with an `-ARCH` suffix before the extension
(for example `artifacts-arm64.sha256` and `build-arm64.log`). Update
[artifacts.lock](../asterisk/artifacts.lock) with the URL and all three build checksums in
a commit right after the build, based on the revision whose sources were built.

Attach one `asterisk-VERSION-N-source.tar.gz` per release. It is the corresponding source
for the binaries in the same release (GPL-2.0). From a clean checkout of the exact Phonelet
revision used for the build, save these commands as a script and run it with `bash`
(for example `bash release-source.sh`), adjusting `BUILD_DIR` first; the release name and its
build number come from the URLs in `artifacts.lock`.
The `git archive` command uses that build revision.

```sh
set -euo pipefail
BUILD_DIR="$PWD/.test-build/arm64"
REVISION=$(git rev-parse HEAD)
VERSION=$(awk -F= '$1 == "ASTERISK_VERSION" {print $2}' asterisk/sources.lock)
RELEASE=$(awk '$1 == "arm64" {n = split($5, part, "/"); print part[n - 1]}' asterisk/artifacts.lock)
[[ "$RELEASE" =~ ^asterisk-$VERSION-[0-9]+$ ]]
SOURCE="$PWD/.test-build/$RELEASE-source"
bash scripts/prepare-sources.sh "$SOURCE"
# prepare-sources.sh records absolute checkout paths: keep the build's record (/repo paths, same sums).
diff <(cut -d' ' -f1 "$BUILD_DIR/src/applied-sources.sha256") <(cut -d' ' -f1 "$SOURCE/applied-sources.sha256")
cp "$BUILD_DIR/src/applied-sources.sha256" "$SOURCE/applied-sources.sha256"
shopt -s nullglob
PJPROJECT_MD5=("$BUILD_DIR"/src/asterisk/third-party/pjproject/pjproject-*.tar.bz2.md5)
test "${#PJPROJECT_MD5[@]}" -eq 1
PJPROJECT=$(basename "${PJPROJECT_MD5[0]}" .md5)
cp "$BUILD_DIR/tmp/$PJPROJECT" "${PJPROJECT_MD5[0]}" "$SOURCE/asterisk/third-party/pjproject/"
mkdir "$SOURCE/phonelet"
git archive "$REVISION" | tar -x -C "$SOURCE/phonelet"
tar --sort=name --owner=0 --group=0 --numeric-owner --exclude=.git \
  -C "$(dirname "$SOURCE")" -czf "$SOURCE.tar.gz" "$(basename "$SOURCE")"
```

`SOURCE` must not exist. Preparation provides the patched Asterisk and chan_dongle trees;
the commands add the bundled PJPROJECT tarball from the build's `/work/tmp` and its shipped
`.md5` file, plus the Phonelet build scripts via `git archive`, excluding `.git` directories.

## Test levels and their limits

Create the Python environment from the repository root when dependencies are available:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps --require-hashes -r requirements-dev.lock
```

`requirements.lock` (runtime) and `requirements-dev.lock` (runtime plus test tools) pin exact versions with
the sha256 of every file PyPI publishes for them (all wheels and the sdist), so one lock installs on arm64
and amd64 and on every supported Python. After changing a pinned version, regenerate the hashes from PyPI
(the Python standard library only; versions are kept):

```sh
python3 scripts/hash-locks.py
```

Run unit and service tests separately from tests that compile driver code:

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q -m "not driver"
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q -m driver
```

| Level | What it checks | What it does not prove |
|---|---|---|
| Unit and service tests (`not driver`) | Python parsing, assembly, temporary SQLite transactions/outbox, API, lifecycle, privacy, commands and fake notification transports | Real modem, Asterisk lifecycle or provider delivery |
| Driver tests (`driver`), including PTY integration | Patched C receive/send paths, framing, deadlines, memory selection, journal durability error paths and recovery through Python replay into temporary SQLite/outbox | A loaded Asterisk module, real CDR production, voice, USB/radio behaviour or physical power-loss safety |
| Full container build | All driver translation units and linking against real configured pinned Asterisk headers, full Asterisk build, upstream driver `make check` and staged packaging | Module loading, working runtime configuration, call audio or reception/delivery on hardware |

Driver tests use the hash-verified upstream subset in
[tests/fixtures/chan_dongle](../tests/fixtures/chan_dongle/README.md), apply the current
patches with zero fuzz/offsets and compile locally. They need `cc`, `patch` and Linux
PTYs, but no source downloads or installed Asterisk. One PTY represents the data port,
mixing command replies with unsolicited modem notifications. The monitor, TTY open,
framing, parsers and SMS journal writer are real patched C code; Asterisk channels,
queue storage, time and reconnect scheduling use adapters. RING is observed in C,
but the subsequent integration-test CDR is synthetic. There is no audio PTY.

The SMS command page test also needs `node`; without it that comparison is skipped.
Asyncio/database tests need permitted local sockets/socketpairs. If a sandbox blocks
worker-thread wakeups, a test may hang; a timeout is not a passing result. Keep tests
on temporary databases, synthetic traces and fake transports throughout. None of
these levels authorizes a live SMS, call, notification, modem reset or deployment.
