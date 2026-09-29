#!/bin/bash
# Build the live image in a Debian 12 chroot, the way CI does.
#
# CI builds inside a privileged debian:12 container. Building anywhere else
# validates something CI never runs: live-build's options change between
# Debian releases, and --uefi-secure-boot is one that has already caused a
# failure once when Ubuntu's fork rejected it. A host running trixie would
# parse the same build-iso.sh with a different generation of live-build.
#
# So this bootstraps bookworm, installs exactly the package list the workflow
# installs, and runs the same command. A green run here means CI will be green
# for the same reasons, not by coincidence.
#
# Intended for WSL or any Debian-ish host with debootstrap. The chroot is kept
# between runs, so the first build pays for it and later ones do not.
set -euo pipefail

CHROOT="${CHROOT:-/srv/zeroize-bookworm}"
SUITE=bookworm
MIRROR="${MIRROR:-http://deb.debian.org/debian}"

# Where the repo is, and where it gets built.
#
# Not built in place when the source is on /mnt/*: DrvFs does not carry Unix
# ownership, symlinks or xattrs, and mksquashfs needs all three. The build also
# writes tens of gigabytes, which is painfully slow across the 9p boundary.
SOURCE="${SOURCE:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK="$CHROOT/build/zeroize"

if [ "$(id -u)" -ne 0 ]; then
    echo "This needs root: debootstrap, mount and chroot all do." >&2
    echo "Try:  sudo bash $0" >&2
    exit 1
fi

for TOOL in debootstrap rsync; do
    command -v "$TOOL" >/dev/null 2>&1 || {
        echo "$TOOL is not installed. apt-get install -y debootstrap rsync" >&2
        exit 1
    }
done

case "$CHROOT" in
    /mnt/*)
        echo "Refusing to put the chroot on $CHROOT." >&2
        echo "DrvFs cannot represent the ownership and device nodes debootstrap" >&2
        echo "creates, and the build will fail in ways that look like bugs in" >&2
        echo "live-build. Use a path on the Linux filesystem." >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Unmount on the way out, deepest first.
# ---------------------------------------------------------------------------
# Deepest first because /dev/pts is under /dev, and unmounting /dev while
# /dev/pts is still mounted leaves a mount the kernel will not release. That is
# how an earlier build left 58 GB of trees that rm -rf could not remove: the
# bind mounts were still live, so the recursive delete walked into the host
# filesystem and then failed.
cleanup() {
    local target
    for target in "$CHROOT/dev/pts" "$CHROOT/dev" "$CHROOT/proc" "$CHROOT/sys"; do
        mountpoint -q "$target" 2>/dev/null && umount -l "$target" 2>/dev/null || true
    done
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Bootstrap, once.
# ---------------------------------------------------------------------------
if [ ! -x "$CHROOT/bin/bash" ]; then
    echo "==> bootstrapping $SUITE into $CHROOT (a few minutes)"
    mkdir -p "$CHROOT"
    debootstrap --arch=amd64 "$SUITE" "$CHROOT" "$MIRROR"
else
    echo "==> reusing the existing chroot at $CHROOT"
fi

printf '%s\n' \
    "deb $MIRROR $SUITE main contrib non-free non-free-firmware" \
    "deb $MIRROR $SUITE-updates main contrib non-free non-free-firmware" \
    "deb http://security.debian.org/debian-security $SUITE-security main contrib non-free non-free-firmware" \
    > "$CHROOT/etc/apt/sources.list"

mountpoint -q "$CHROOT/proc" || mount -t proc proc "$CHROOT/proc"
mountpoint -q "$CHROOT/sys"  || mount -t sysfs sys "$CHROOT/sys"
mountpoint -q "$CHROOT/dev"  || mount --bind /dev "$CHROOT/dev"
mountpoint -q "$CHROOT/dev/pts" || mount --bind /dev/pts "$CHROOT/dev/pts"

# ---------------------------------------------------------------------------
# Stage the source.
# ---------------------------------------------------------------------------
echo "==> staging $SOURCE into the chroot"
mkdir -p "$WORK"
# Build outputs and the virtualenv are excluded rather than cleaned afterwards:
# build/ alone has run to several gigabytes, and copying a Windows-side .venv
# into a Linux chroot produces an environment whose interpreter paths all point
# at a drive that does not exist here.
rsync -a --delete \
    --exclude '.git/' \
    --exclude 'build/' \
    --exclude 'dist/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    "$SOURCE/" "$WORK/"

# ---------------------------------------------------------------------------
# Refuse to build from a tree carrying carriage returns.
# ---------------------------------------------------------------------------
# A CR in a shebang makes the kernel look for an interpreter whose name ends in a carriage return.
# It does not exist, so sh reports "not found" - naming the SCRIPT, not the
# interpreter, which sends you looking at the wrong file. live-build then fails
# with nothing but "hook failed (exit non-zero)".
#
# This only bites a local build: git stores these as LF and CI checks them out
# that way, but core.autocrlf rewrites them in the working tree, and git calls
# the file clean because it normalises back on commit. So the corruption is
# invisible to git status and reaches only the thing that reads the working
# tree directly - which is this script.
echo "==> checking the staged tree for carriage returns"
CR_FOUND=0
while IFS= read -r CANDIDATE; do
    case "$CANDIDATE" in
        */.git/*) continue ;;
    esac
    if head -c 2 "$CANDIDATE" 2>/dev/null | grep -q '#!' && grep -qU $'\015' "$CANDIDATE" 2>/dev/null; then
        echo "    CR in ${CANDIDATE#$WORK/}"
        CR_FOUND=1
    fi
done <<CANDIDATES
$(find "$WORK" -type f -not -path '*/build/*' -not -path '*/dist/*' \( -name '*.sh' -o -name '*.hook.*' -o -name '*.script' -o -name 'zeroize' \) 2>/dev/null)
CANDIDATES

if [ "$CR_FOUND" -eq 1 ]; then
    echo
    echo "Refusing to build. Normalise the working tree first:" >&2
    echo "    git add --renormalize . && git checkout -- ." >&2
    exit 1
fi
echo "    clean"

# ---------------------------------------------------------------------------
# Install and build, with the workflow's package list verbatim.
# ---------------------------------------------------------------------------
# NOT --no-install-recommends. live-build Recommends apt-utils, bzip2, cpio,
# cryptsetup, file, rsync, systemd-container, wget and xz-utils, and its own
# stages shell out to several of them. Stripping recommends produced a bare
# exit 127 with no output at all, because the failure happened inside
# live-build before any stage had printed anything.
cat > "$CHROOT/build/run-build.sh" <<'INNER'
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C

apt-get update -qq
apt-get install -y \
    live-build debootstrap squashfs-tools xorriso isolinux \
    syslinux-common syslinux-utils dosfstools mtools parted \
    fatresize grub-common grub-pc-bin grub-efi-amd64-bin \
    librsvg2-bin fonts-inter dpkg-dev python3 ca-certificates \
    cpio file rsync xz-utils wget bzip2 \
    sbsigntool

cd /build/zeroize
python3 build.py iso

# An exit code says the build ran, not that it built the right tree. This
# mounts the result and reads the installed package back out of the squashfs,
# exactly as the workflow does.
bash tools/verify-iso-contents.sh build/dist/zeroize-live-bookworm-amd64.iso

cd build/dist && sha256sum -c ./*.sha256
INNER
chmod +x "$CHROOT/build/run-build.sh"

echo "==> building"
chroot "$CHROOT" /build/run-build.sh

# ---------------------------------------------------------------------------
# Hand the results back.
# ---------------------------------------------------------------------------
OUT="$SOURCE/build/dist"
mkdir -p "$OUT"
cp -f "$WORK"/build/dist/* "$OUT/" 2>/dev/null || true

echo
echo "==> artefacts in $OUT"
ls -lh "$OUT" 2>/dev/null | sed 's/^/    /'
