#!/bin/bash
# Verify a built ISO actually contains the source we think it does.
#
# An ISO takes ten minutes, and the only thing the build reports is an exit
# code. That says the build ran, not that it built the right tree - a stale
# .deb, an interrupted stage, or two builds sharing a directory all exit 0.
# So this pulls the installed Python back out of the squashfs and checks it.
set -euo pipefail

ISO="${1:?usage: verify-iso-contents.sh <iso> [<expected-string> ...]}"
shift
EXPECTED=("$@")
[ ${#EXPECTED[@]} -eq 0 ] && EXPECTED=("def clear_freezes" "auto_unfreeze_on_scan")

# Mounting an ISO needs root. A developer runs this unprivileged and reaches
# for sudo; the CI container runs as root and has no sudo installed at all,
# which is exactly how this script failed there - not on the image, which had
# built correctly, but on the tool checking it.
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    echo "FAIL: not root and sudo is not installed; cannot mount the ISO" >&2
    exit 1
fi

WORK="$(mktemp -d)"
cleanup() {
    [ -n "${WORK:-}" ] || return 0
    $SUDO umount "$WORK/iso" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$WORK/iso"

echo "==> Mounting $(basename "$ISO")"
$SUDO mount -o loop,ro "$ISO" "$WORK/iso"

SQUASH="$WORK/iso/live/filesystem.squashfs"
[ -f "$SQUASH" ] || { echo "FAIL: no live/filesystem.squashfs in the ISO"; exit 1; }
echo "    squashfs: $(stat -c %s "$SQUASH") bytes"

echo "==> Extracting the installed zeroize package"
# The package installs to /usr/lib/zeroize, not dist-packages - it ships with
# its own interpreter path rather than onto the system one.
$SUDO unsquashfs -n -f -d "$WORK/root" "$SQUASH" 'usr/lib/zeroize' >/dev/null

PKG="$WORK/root/usr/lib/zeroize/zeroize"
[ -d "$PKG" ] || { echo "FAIL: the zeroize package is not installed in the image"; exit 1; }
echo "    package: /usr/lib/zeroize/zeroize"
echo "    version: $($SUDO grep -o '__version__ = .[^\"]*.' "$PKG/__init__.py" | head -1)"
echo "    modules: $($SUDO find "$PKG" -name '*.py' | wc -l)"

status=0
for needle in "${EXPECTED[@]}"; do
    if $SUDO grep -rqF "$needle" "$PKG"; then
        echo "    PRESENT  $needle"
    else
        echo "    MISSING  $needle"
        status=1
    fi
done

if [ $status -eq 0 ]; then
    echo "==> PASS: the image contains the expected source"
else
    echo "==> FAIL: the image is stale or was built from the wrong tree"
fi
exit $status
