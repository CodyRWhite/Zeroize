#!/usr/bin/env bash
#
# Build the Zeroize bootable live image.
#
#   ./build-iso.sh <path-to-zeroize.deb> <output-directory>
#
# Produces a hybrid BIOS/UEFI ISO that boots to a desktop with Zeroize already
# installed and a launcher on the desktop and in the menu.
#
# This wraps Debian live-build rather than assembling a squashfs by hand.
# live-build already solves the parts that are tedious and easy to get subtly
# wrong - the bootloader for both BIOS and UEFI, the initramfs with the live
# hooks, Secure Boot shim chaining, and the hybrid ISO layout - and it is in
# the Debian and Ubuntu archives, so the build host needs nothing unusual.
#
# Requirements: a Debian or Ubuntu host (or container) with live-build,
# xorriso and root privileges, plus network access to the archive.
#
# In a container:
#   docker run --rm --privileged -v "$PWD":/src -w /src debian:12 \
#     bash -c 'apt-get update && apt-get install -y live-build xorriso &&
#              packaging/iso/build-iso.sh build/dist/zeroize_1.0.0_all.deb build/dist'
#
# --privileged is needed because live-build mounts /proc and loop devices
# inside the chroot it assembles.

set -euo pipefail

DEB_PATH="${1:?usage: build-iso.sh <zeroize.deb> <output-dir>}"
OUTPUT_DIR="${2:?usage: build-iso.sh <zeroize.deb> <output-dir>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The scratch tree goes under /var/tmp, not /tmp. live-build needs 10-15 GB of
# working space, and /tmp is a tmpfs on an increasing number of systems - every
# WSL distribution, and any host using systemd's default. A tmpfs is RAM, so a
# build there either exhausts memory or dies on a full filesystem partway
# through. /var/tmp is the FHS location for exactly this: large temporary data
# that must survive on real storage.
WORK_DIR="${WORK_DIR:-$(mktemp -d /var/tmp/zeroize-iso-XXXXXX)}"

# Each build leaves 4-5 GB behind in WORK_DIR: an unpacked chroot, the package
# cache and the staged binary tree. Nothing removed them, and fourteen of them
# had accumulated to 58 GB before anyone noticed - which is the kind of thing
# that is invisible right up until a build fails on a full disk.
#
# A successful build keeps nothing. A failed one keeps everything and says
# where, because the chroot is where the evidence is. Set ZEROIZE_KEEP_WORK=1
# to keep it either way.
WORK_DIR_OWNED=0
case "${WORK_DIR}" in
    /var/tmp/zeroize-iso-*) WORK_DIR_OWNED=1 ;;
esac

cleanup_work_dir() {
    status=$?
    if [ "$WORK_DIR_OWNED" != "1" ] || [ "${ZEROIZE_KEEP_WORK:-0}" = "1" ]; then
        return $status
    fi
    # Unmount first, whatever the outcome. live-build bind-mounts /proc, /sys
    # and /dev/pts inside the chroot, and an interrupted build leaves them
    # mounted. rm -rf then fails on every file under them - "Operation not
    # permitted", thousands of lines of it - and the tree survives. That is how
    # 58 GB of abandoned build trees accumulated: not because nothing tried to
    # remove them, but because removal could not work.
    #
    # Deepest path first so children unmount before their parents, and lazily
    # so a mount something still holds does not block the rest.
    findmnt -rn -o TARGET 2>/dev/null         | grep -F "$WORK_DIR"         | awk '{ print length, $0 }' | sort -rn | cut -d" " -f2-         | while read -r MOUNTPOINT; do
            umount -lf "$MOUNTPOINT" 2>/dev/null                 || sudo umount -lf "$MOUNTPOINT" 2>/dev/null || true
        done || true

    if [ "$status" -ne 0 ]; then
        echo "==> Build tree kept for inspection: $WORK_DIR" >&2
        return $status
    fi
    # sudo because live-build creates root-owned files inside the chroot.
    rm -rf "$WORK_DIR" 2>/dev/null || sudo rm -rf "$WORK_DIR" 2>/dev/null || true
    return $status
}
trap cleanup_work_dir EXIT

# The base distribution. bookworm is chosen over a newer release because it is
# the oldest target the application supports, so an image built on it runs on
# the widest range of bench hardware. DISTRIBUTION can override it.
DISTRIBUTION="${DISTRIBUTION:-bookworm}"
ARCHITECTURE="${ARCHITECTURE:-amd64}"
IMAGE_NAME="zeroize-live-${DISTRIBUTION}-${ARCHITECTURE}"

if [ "$(id -u)" -ne 0 ]; then
    echo "This must run as root: live-build assembles a chroot." >&2
    exit 1
fi

for tool in lb xorriso; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Missing $tool. Install live-build and xorriso." >&2
        exit 1
    fi
done

if [ ! -f "$DEB_PATH" ]; then
    echo "No such .deb: $DEB_PATH  (run: python3 build.py deb)" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# WSL
# ---------------------------------------------------------------------------
# WSL2 builds this image perfectly well - it runs a real kernel with loop
# devices, mount namespaces and working chroot semantics. Two things about it
# are not obvious, and both fail deep into a 30-minute build rather than at the
# start, so they are checked here.
if grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null; then
    echo "==> Running under WSL"

    # WSL1 has no Linux kernel: no loop devices, no real mount namespaces,
    # and debootstrap's device nodes cannot be created. It cannot do this.
    if [ ! -d /proc/sys/fs/binfmt_misc ] && [ ! -e /dev/loop-control ]; then
        echo "This looks like WSL1, which cannot build a live image: it has no" >&2
        echo "Linux kernel, so loop devices and chroot semantics are unavailable." >&2
        echo "Convert the distribution to WSL2:  wsl --set-version <distro> 2" >&2
        exit 1
    fi

    # The build tree must live on the WSL ext4 filesystem, never on a Windows
    # drive mounted through DrvFs (/mnt/c, /mnt/d). debootstrap needs real
    # ownership, setuid bits, symlinks and device nodes, and DrvFs provides
    # none of them convincingly - the chroot stage fails partway through with
    # confusing permission errors. The default WORK_DIR under /var/tmp is
    # already correct; this catches an override.
    case "$WORK_DIR" in
        /mnt/*)
            echo "WORK_DIR is on a Windows drive ($WORK_DIR)." >&2
            echo "Under WSL the build tree must sit on the Linux filesystem - DrvFs" >&2
            echo "cannot represent the ownership and device nodes debootstrap needs." >&2
            echo "Use a native path, for example:  WORK_DIR=/var/tmp/zeroize-iso" >&2
            exit 1
            ;;
    esac

    # live-build needs roughly 10-15 GB of scratch space. Under WSL that comes
    # out of the distribution's virtual disk. Note that /tmp is a tmpfs in every
    # WSL distribution, which is why the default WORK_DIR is under /var/tmp.
    available_kib="$(df -Pk "$(dirname "$WORK_DIR")" | awk 'NR==2 {print $4}')"
    if [ -n "$available_kib" ] && [ "$available_kib" -lt 15000000 ]; then
        echo "Warning: only $((available_kib / 1024 / 1024)) GB free on $(dirname "$WORK_DIR")." >&2
        echo "live-build typically needs 10-15 GB of scratch space." >&2
    fi
fi

DEB_PATH="$(readlink -f "$DEB_PATH")"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(readlink -f "$OUTPUT_DIR")"

echo "==> Building $IMAGE_NAME in $WORK_DIR"
cd "$WORK_DIR"

# ---------------------------------------------------------------------------
# live-build configuration
# ---------------------------------------------------------------------------
# --bootappend-live:
#   boot=live components   - the standard live-boot entry point
#   username / hostname    - identity of the autologin account
#   noeject                - do NOT prompt to remove the medium at shutdown.
#                            A wipe appliance is routinely shut down with the
#                            USB still in it, and the prompt blocks unattended
#                            teardown.
#   nomodeset is deliberately NOT set: it breaks the compositor on most modern
#   hardware and the desktop is the whole point of this image.
#
# --uefi-secure-boot enable, rather than the "auto" default: auto quietly
# produces an unsigned image when the signed bootloader packages are missing,
# and an appliance that will not boot on a Secure Boot machine is something you
# discover at the bench with the drives already pulled. Asking for it
# explicitly makes the build fail instead of the boot.
lb config \
    --distribution "$DISTRIBUTION" \
    --architectures "$ARCHITECTURE" \
    --binary-images iso-hybrid \
    --debian-installer none \
    --uefi-secure-boot enable \
    --archive-areas "main contrib non-free non-free-firmware" \
    --firmware-binary true \
    --firmware-chroot true \
    --memtest none \
    --iso-application "Zeroize Drive Wiper" \
    --iso-publisher "Zeroize" \
    --iso-volume "ZEROIZE" \
    --image-name "$IMAGE_NAME" \
    --bootappend-live "boot=live components quiet splash noeject username=zeroize hostname=zeroize"

mkdir -p config/package-lists config/includes.chroot config/hooks/live

# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------
# A deliberately small desktop. XFCE is used rather than GNOME because it boots
# faster on the elderly hardware these images usually run on, and because a
# GNOME session on a live image pulls in an installer, a software centre and an
# online-accounts stack that have no business on a wipe appliance.
cat > config/package-lists/zeroize.list.chroot <<'PACKAGES'
# Desktop session
xserver-xorg
xinit
lightdm
xfce4
xfce4-terminal
dbus-x11
# xrandr and xset. Pulled in by xfce4 in practice, but both are load-bearing
# here - xrandr names the monitor connectors the wallpaper is keyed on, and
# xset enforces blank-without-lock - so neither is left to chance.
x11-xserver-utils
# gio(1), used to write the GIO metadata that marks the desktop launcher
# trusted. Without it xfdesktop refuses to run the launcher no matter how the
# permissions are set.
libglib2.0-bin

# Secure Boot. shim is what the firmware validates, and Debian's is
# dual-signed by both the Microsoft UEFI CA 2011 and the Microsoft UEFI CA
# 2023, so the image boots on machines whose firmware carries either. mokutil
# lets an operator check the machine's Secure Boot state from the live session.
shim-signed
grub-efi-amd64-signed
mokutil

# grub-common supplies grub-mkfont, and fonts-inter the face it converts.
# The boot menu's fonts are built HERE, inside the chroot, rather than on the
# build host: the host runs Debian 13 (GRUB 2.12) while the image runs Debian
# 12 (GRUB 2.06), and a PF2 file written by the newer tool is not reliably
# loadable by the older bootloader. When loadfont fails GRUB says nothing - it
# abandons the theme and draws its plain text menu - so the mismatch is
# invisible until someone looks at a booted screen.
grub-common
fonts-inter

# Reading the certificates the tool produces, without leaving the appliance.
# atril rather than evince: the same familiar document viewer, without pulling
# the GNOME desktop libraries onto an image whose desktop is XFCE.
atril
xdg-utils

# Zeroize runtime
python3
python3-gi
python3-gi-cairo
gir1.2-gtk-4.0
gir1.2-adw-1
python3-reportlab
policykit-1

# Drive tooling
nvme-cli
hdparm
sg3-utils
smartmontools
dmidecode
util-linux
gdisk
parted

# Useful on a bench
pciutils
usbutils
lshw
less
nano

# For growing the certificate partition to fill the stick. Doing this from
# Linux matters: Windows' partition manager rewrites the hybrid ISO layout
# when it touches the table and breaks the boot, while parted and fatresize
# leave the entries they were not asked about alone.
fatresize
dosfstools
PACKAGES

# ---------------------------------------------------------------------------
# The package itself
# ---------------------------------------------------------------------------
mkdir -p config/packages.chroot
cp "$DEB_PATH" config/packages.chroot/

# ---------------------------------------------------------------------------
# Autologin
# ---------------------------------------------------------------------------
mkdir -p config/includes.chroot/etc/lightdm/lightdm.conf.d
cat > config/includes.chroot/etc/lightdm/lightdm.conf.d/10-zeroize-autologin.conf <<'LIGHTDM'
[Seat:*]
autologin-user=zeroize
autologin-user-timeout=0
user-session=xfce
LIGHTDM

# ---------------------------------------------------------------------------
# Desktop and menu launchers
# ---------------------------------------------------------------------------
mkdir -p config/includes.chroot/etc/skel/Desktop
cat > config/includes.chroot/etc/skel/Desktop/zeroize.desktop <<'LAUNCHER'
[Desktop Entry]
Type=Application
Version=1.0
Name=Zeroize Drive Wiper
Comment=Erase drives and issue a certificate
Exec=pkexec /usr/bin/zeroize
Icon=io.zeroize.Zeroize
Terminal=false
Categories=System;Security;
StartupNotify=true
LAUNCHER
chmod +x config/includes.chroot/etc/skel/Desktop/zeroize.desktop

# xfdesktop refuses to run a .desktop file on the desktop unless it is marked
# executable, and shows an "Untrusted application launcher" prompt instead.
# The file *is* 0755 in /etc/skel, but the bit does not reliably survive the
# copy live-config makes when it creates the live user at boot - so it is set
# again at login, on the real file in the real home directory.
#
# A session hook rather than a hope: it costs nothing, and an operator who has
# closed the window and wants it back should not be reading a security prompt
# to get there.
mkdir -p config/includes.chroot/etc/xdg/autostart
# Created here rather than assumed: this is the first block to write into
# usr/local/bin, and the one that used to create it runs later.
mkdir -p config/includes.chroot/usr/local/bin

cat > config/includes.chroot/usr/local/bin/zeroize-trust-launcher <<'TRUSTSCRIPT'
#!/bin/sh
# Mark the desktop launcher trusted, so xfdesktop will run it.
#
# xfdesktop refuses to launch a .desktop file on the desktop unless it is
# satisfied the user put it there deliberately, and shows "Untrusted
# application launcher - the desktop file is in an insecure location and not
# marked as executable" instead. Setting the executable bit alone is NOT
# enough on 4.18: it also wants a GIO metadata attribute holding a checksum of
# the file, which is what the dialog's "Mark Executable" button writes. The
# earlier version of this hook only chmodded, which is why the prompt kept
# coming back even though the log said the launcher had been made executable.
#
# The attribute name has differed across versions, so both known spellings are
# written. Setting one that a given release ignores is harmless.
#
# This runs as the user inside the session, not from a system unit: GIO
# metadata lives in the user's own store and needs the session bus.
set -u

LOGFILE=/var/log/zeroize/trust-launcher.log
mkdir -p /var/log/zeroize 2>/dev/null || true
log() {
    echo "zeroize-trust-launcher: $*"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGFILE" 2>/dev/null || true
}

DESKTOP_DIR="$HOME/Desktop"
[ -d "$DESKTOP_DIR" ] || { log "no $DESKTOP_DIR"; exit 0; }

for LAUNCHER in "$DESKTOP_DIR"/*.desktop; do
    [ -f "$LAUNCHER" ] || continue

    chmod +x "$LAUNCHER" 2>/dev/null || log "could not chmod $LAUNCHER"

    if ! command -v gio >/dev/null 2>&1; then
        log "gio is not installed; $LAUNCHER may still be refused as untrusted"
        continue
    fi

    SUM=$(sha256sum "$LAUNCHER" 2>/dev/null | cut -d" " -f1)
    [ -n "$SUM" ] || { log "could not checksum $LAUNCHER"; continue; }

    for ATTRIBUTE in metadata::xfdesktop-exe-checksum metadata::xfce-exe-checksum; do
        gio set -t string "$LAUNCHER" "$ATTRIBUTE" "$SUM" 2>/dev/null \
            || log "could not set $ATTRIBUTE on $LAUNCHER"
    done

    # Some releases key trust on this instead.
    gio set -t string "$LAUNCHER" metadata::trusted true 2>/dev/null || true

    log "trusted $(basename "$LAUNCHER") ($SUM)"
done
TRUSTSCRIPT
chmod +x config/includes.chroot/usr/local/bin/zeroize-trust-launcher

cat > config/includes.chroot/etc/xdg/autostart/zeroize-trust-launcher.desktop <<'TRUST'
[Desktop Entry]
Type=Application
Version=1.0
Name=Zeroize desktop launcher trust
Comment=Mark the Zeroize desktop launcher trusted so xfdesktop will run it
Exec=/usr/local/bin/zeroize-trust-launcher
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
OnlyShowIn=XFCE;
TRUST

# X-level assertion, independent of any desktop setting: blank the screen on
# idle, never lock it. `s off` disables the X screensaver's own locking while
# `+dpms` leaves power management doing the blanking.
cat > config/includes.chroot/etc/xdg/autostart/zeroize-no-lock.desktop <<'NOLOCK'
[Desktop Entry]
Type=Application
Version=1.0
Name=Zeroize screen policy
Comment=Let the display sleep, never lock it - the live account has no password
Exec=sh -c "xset s off -dpms; xset s noblank; xset dpms 600 720 900"
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
OnlyShowIn=XFCE;
NOLOCK

# ---------------------------------------------------------------------------
# Start Zeroize automatically
# ---------------------------------------------------------------------------
# This is an appliance: the operator boots it to wipe drives, and there is
# nothing else on the image to do. Waiting for them to find an icon adds a step
# and, on a machine whose desktop launcher has been refused as untrusted, a
# dead end. The desktop icon stays for relaunching after a deliberate close.
cat > config/includes.chroot/etc/xdg/autostart/zeroize.desktop <<'AUTOSTART'
[Desktop Entry]
Type=Application
Version=1.0
Name=Zeroize Drive Wiper
Comment=Erase drives and issue a certificate
# The short delay is not cosmetic: pkexec needs the polkit agent, and GTK4
# needs the compositor. Both are started by the same session this entry is
# part of, so racing them produces an authentication failure or a blank window.
Exec=sh -c 'sleep 4; exec pkexec /usr/bin/zeroize'
Icon=io.zeroize.Zeroize
Terminal=false
X-GNOME-Autostart-enabled=true
OnlyShowIn=XFCE;
AUTOSTART

# ---------------------------------------------------------------------------
# Session preparation, before the display manager
# ---------------------------------------------------------------------------
# Two jobs that must happen before anyone can see a desktop:
#
# 1. Mark the desktop launcher executable. The autostart hook above does this
#    too, but it races xfdesktop - which reads the Desktop directory as it
#    starts and caches what it found. Losing that race is what produces the
#    "Untrusted application launcher" prompt, so the bit is set here, before
#    the session exists at all.
#
# 2. Mount the ZEROIZE-OUT partition. Nothing else does. udisks2 auto-mounts
#    media as it is *hotplugged*; the partitions of the medium already present
#    at boot are not hotplug events, so the output partition simply never
#    appeared and certificates fell back to the home directory - which is a
#    tmpfs, so they were lost at power off.
# Created here rather than relied upon: this block runs before the one that
# stages the expansion service, which is where these directories used to first
# appear.
mkdir -p config/includes.chroot/usr/local/sbin          config/includes.chroot/etc/systemd/system          config/includes.chroot/etc/xdg/autostart

cat > config/includes.chroot/usr/local/sbin/zeroize-prepare-session <<'PREPARE'
#!/bin/sh
# Prepare the live session: trust the desktop launcher, mount the output
# volume. Every step is best-effort - none of them is worth refusing to boot
# over, because a machine that boots without an output volume can still erase
# drives and report it on screen.
set -u

MOUNTPOINT=/media/zeroize-out
LABEL=ZEROIZE-OUT
LOGFILE=/var/log/zeroize/prepare-session.log
mkdir -p /var/log/zeroize 2>/dev/null || true
log() {
    echo "zeroize-prepare-session: $*"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGFILE" 2>/dev/null || true
}

for desktop_dir in /etc/skel/Desktop /home/*/Desktop /root/Desktop; do
    [ -d "$desktop_dir" ] || continue
    chmod +x "$desktop_dir"/*.desktop 2>/dev/null         && log "marked launchers executable in $desktop_dir"
done

# vfat needs to be resident before udisks or mount is asked for it; on a live
# image nothing else will have pulled it in yet.
modprobe vfat 2>/dev/null || log "could not load the vfat module"

PART="$(blkid -L "$LABEL" 2>/dev/null || true)"
if [ -z "$PART" ]; then
    log "no volume labelled $LABEL is attached; certificates will stay on the live filesystem"
    exit 0
fi

# Run the expansion from here rather than relying on two units firing in the
# right order. It has to happen before the mount and it is idempotent - it
# exits early when there is nothing to reclaim - so calling it unconditionally
# costs nothing and removes a whole class of ordering bug. Its output is
# captured, which the systemd-ordered version never managed: the log it wrote
# lived on tmpfs and was gone before anyone could read it.
EXPAND=/usr/local/sbin/zeroize-expand-certstore
if [ -x "$EXPAND" ]; then
    log "running the certificate-partition expansion"
    "$EXPAND" "$LABEL" 2>&1 | while IFS= read -r LINE; do log "expand: $LINE"; done
    # The partition may have changed size, so re-resolve it.
    PART="$(blkid -L "$LABEL" 2>/dev/null || echo "$PART")"
fi

if findmnt -n "$MOUNTPOINT" >/dev/null 2>&1; then
    log "$MOUNTPOINT is already mounted"
    exit 0
fi

mkdir -p "$MOUNTPOINT"
# umask=000 because the application runs as root through pkexec while the
# operator reads the certificates as the unprivileged desktop user.
if mount -t vfat -o rw,umask=000,flush "$PART" "$MOUNTPOINT" 2>/dev/null; then
    log "mounted $PART at $MOUNTPOINT"
else
    log "could not mount $PART at $MOUNTPOINT"
    exit 0
fi

# Copy the boot-time logs onto the medium now that it is writable.
#
# Everything under /var/log is on the live tmpfs and dies at power off, so the
# expansion service's log - the only record of why a partition did or did not
# grow - was unreadable by the time anyone could ask. Diagnosing it from the
# symptom alone is guesswork, and it has now cost two rebuilds.
DIAGNOSTICS="$MOUNTPOINT/Zeroize Diagnostics"
mkdir -p "$DIAGNOSTICS" 2>/dev/null || true
for SOURCE in /var/log/zeroize/*.log; do
    [ -f "$SOURCE" ] || continue
    cp -f "$SOURCE" "$DIAGNOSTICS/" 2>/dev/null || true
done

# Partition geometry as the kernel sees it, which is what the expansion acts
# on. If the partition did not grow, the answer is almost always here.
{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') boot ==="
    echo "--- lsblk ---"
    lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT 2>/dev/null
    echo "--- ZEROIZE-OUT ---"
    echo "device: $PART"
    echo "start:  $(cat "/sys/class/block/$(basename "$PART")/start" 2>/dev/null)"
    echo "size:   $(cat "/sys/class/block/$(basename "$PART")/size" 2>/dev/null) sectors"
    echo "--- whole medium ---"
    PARENT="$(lsblk -no PKNAME "$PART" 2>/dev/null | head -1)"
    echo "parent: /dev/$PARENT"
    echo "total:  $(blockdev --getsz "/dev/$PARENT" 2>/dev/null) sectors"
    echo "--- partition table ---"
    fdisk -l "/dev/$PARENT" 2>/dev/null
    echo "--- tools ---"
    for TOOL in parted fatresize partx blockdev blkid python3; do
        printf "%-10s %s
" "$TOOL" "$(command -v "$TOOL" || echo MISSING)"
    done
    echo "--- expansion service ---"
    systemctl status zeroize-expand-certstore.service --no-pager 2>/dev/null | head -20
} > "$DIAGNOSTICS/medium-geometry.txt" 2>&1 || true

log "diagnostics written to $DIAGNOSTICS"
PREPARE
chmod +x config/includes.chroot/usr/local/sbin/zeroize-prepare-session

cat > config/includes.chroot/etc/systemd/system/zeroize-prepare-session.service <<'PREPUNIT'
[Unit]
Description=Prepare the Zeroize live session
# After the expansion so the filesystem is its final size before it is mounted,
# and before the display manager so the desktop never sees an untrusted
# launcher or a missing output volume.
After=local-fs.target zeroize-expand-certstore.service
Before=display-manager.service graphical.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/zeroize-prepare-session
TimeoutStartSec=60
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
PREPUNIT

mkdir -p config/includes.chroot/etc/systemd/system/multi-user.target.wants
ln -sf ../zeroize-prepare-session.service     config/includes.chroot/etc/systemd/system/multi-user.target.wants/zeroize-prepare-session.service

# ---------------------------------------------------------------------------
# Live-image polkit rule
# ---------------------------------------------------------------------------
# On the live image only, the autologin operator may run Zeroize without an
# authentication prompt. There is no password to type - the live account has
# none - and a prompt that cannot be satisfied would make the appliance
# unusable. This file exists ONLY inside the ISO; the .deb never ships it, so
# an ordinary installation still requires authentication.
mkdir -p config/includes.chroot/etc/polkit-1/rules.d
cat > config/includes.chroot/etc/polkit-1/rules.d/49-zeroize-live.rules <<'POLKIT'
// Live image only. Not shipped in the .deb or .rpm.
polkit.addRule(function(action, subject) {
    if (action.id == "io.zeroize.Zeroize.run" && subject.user == "zeroize") {
        return polkit.Result.YES;
    }
});
POLKIT

# ---------------------------------------------------------------------------
# Grow the certificate partition to fill the stick, on first boot
# ---------------------------------------------------------------------------
# The image carries a fixed-size ZEROIZE-OUT partition, so writing it to a
# 64 GB stick leaves most of the stick unused. This reclaims it automatically
# the first time the image boots.
#
# It deliberately does NOT use parted or sfdisk. Both rewrite the whole
# partition table, and this is a hybrid ISO whose table is deliberately odd -
# a type 0x00 entry with the EFI partition nested inside its sector range,
# alongside a GPT. Rewriting it is exactly what breaks the boot when Windows
# does it. Instead four bytes are patched: the sector count of MBR entry 3.
# Entries 1 and 2, the GPT, and every byte the firmware reads are untouched.
mkdir -p config/includes.chroot/usr/local/sbin
cat > config/includes.chroot/usr/local/sbin/zeroize-expand-certstore <<'EXPAND'
#!/bin/sh
# Grow the ZEROIZE-OUT partition to fill the boot medium. Idempotent: it exits
# quietly when there is nothing to reclaim, so it can run on every boot.
set -e

LABEL="${1:-ZEROIZE-OUT}"
MIN_GAIN_SECTORS=131072   # 64 MiB; not worth unmounting for less

# Upper bound on the certificate partition, in 512-byte sectors. 32 GiB.
#
# Not a FAT32 hard limit - that is 2 TiB - but the largest volume Windows
# itself will format as FAT32, and therefore the size beyond which support is
# a matter of luck rather than design. It is also the point past which the
# exercise stops making sense: this partition holds PDFs and logs measured in
# hundreds of kilobytes, and filling a 237 GB stick with a single FAT32 volume
# to store them buys nothing while asking fatresize to do the one thing it is
# worst at. Growing a 1 GiB filesystem by two orders of magnitude is how the
# resize came to fail silently in the first place.
MAX_SECTORS=$((32 * 1024 * 1024 * 1024 / 512))

LOGFILE=/var/log/zeroize/expand-certstore.log
mkdir -p /var/log/zeroize 2>/dev/null || true
log() {
    echo "zeroize-expand-certstore: $*"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGFILE" 2>/dev/null || true
}

log "starting; looking for a volume labelled ${1:-ZEROIZE-OUT}"

PART="$(blkid -L "$LABEL" 2>/dev/null || true)"
if [ -z "$PART" ]; then
    log "no volume labelled $LABEL; nothing to do"
    exit 0
fi

PART_NAME="$(basename "$PART")"
PARENT_NAME="$(lsblk -no PKNAME "$PART" 2>/dev/null | head -1)"
if [ -z "$PARENT_NAME" ]; then
    log "$PART has no parent disk; refusing to touch it"
    exit 0
fi
DISK="/dev/$PARENT_NAME"

# Safety: only ever the medium we booted from. Without this the script would
# happily expand a partition on a drive that is about to be wiped.
MEDIUM="$(findmnt -n -o SOURCE /run/live/medium 2>/dev/null || true)"
if [ -z "$MEDIUM" ]; then
    log "not running from a live medium; refusing"
    exit 0
fi
MEDIUM_PARENT="$(lsblk -no PKNAME "$MEDIUM" 2>/dev/null | head -1)"
[ -n "$MEDIUM_PARENT" ] || MEDIUM_PARENT="$(basename "$MEDIUM")"
if [ "$MEDIUM_PARENT" != "$PARENT_NAME" ]; then
    log "$PART is not on the boot medium (/dev/$MEDIUM_PARENT); refusing"
    exit 0
fi

PART_NUMBER="$(cat "/sys/class/block/$PART_NAME/partition" 2>/dev/null || echo 0)"
if [ "$PART_NUMBER" != "3" ]; then
    log "$PART is partition $PART_NUMBER, expected 3; refusing"
    exit 0
fi

START="$(cat "/sys/class/block/$PART_NAME/start")"
CURRENT="$(cat "/sys/class/block/$PART_NAME/size")"
TOTAL="$(blockdev --getsz "$DISK")"
TARGET=$((TOTAL - START))
if [ "$TARGET" -gt "$MAX_SECTORS" ]; then
    log "capping the partition at $((MAX_SECTORS / 2048)) MiB (medium would allow $((TARGET / 2048)) MiB)"
    TARGET=$MAX_SECTORS
fi

# Whether the PARTITION needs to grow is a separate question from whether the
# FILESYSTEM inside it does, and conflating them left this stuck. The partition
# grew to 32 GiB and was written to the medium; the filesystem resize then
# died; and every later boot saw a partition already at target, said "nothing
# to do", and exited - so the filesystem stayed 1 GiB permanently, with no
# route back. The filesystem step now runs regardless.
GROW_PARTITION=1
if [ "$TARGET" -le "$((CURRENT + MIN_GAIN_SECTORS))" ]; then
    log "the partition is already $CURRENT sectors; checking the filesystem inside it"
    GROW_PARTITION=0
    TARGET=$CURRENT
fi

umount "$PART" 2>/dev/null || true

if [ "$GROW_PARTITION" = "1" ]; then

log "growing $PART from $CURRENT to $TARGET sectors on $DISK"

python3 - "$DISK" "$TARGET" <<'PATCH'
import struct
import sys

disk, target = sys.argv[1], int(sys.argv[2])
ENTRY = 0x1BE + 32          # MBR partition entry 3
SIZE_FIELD = ENTRY + 12     # its sector-count field

with open(disk, "r+b") as device:
    mbr = bytearray(device.read(512))
    if mbr[ENTRY + 4] != 0x0C:
        raise SystemExit(f"entry 3 is type 0x{mbr[ENTRY + 4]:02x}, not FAT32 LBA; refusing")
    # Only these four bytes change. Nothing else in the table is rewritten.
    mbr[SIZE_FIELD:SIZE_FIELD + 4] = struct.pack("<I", target)
    device.seek(0)
    device.write(mbr)
    device.flush()
PATCH

# Refresh just this partition rather than re-reading the whole table, which
# would fail while the ISO 9660 partition is mounted.
partx -u "$DISK" >/dev/null 2>&1 || true
udevadm settle 2>/dev/null || true

fi   # GROW_PARTITION

# The MBR now says the partition is bigger, but that is only true once the
# KERNEL has re-read it. fatresize asks libparted for the device size, so if
# the kernel still holds the old geometry then "-s max" means the OLD size and
# the resize is a no-op or an error. partx is best-effort - it cannot re-read
# a table while another partition on the disk is mounted, which on this medium
# is always the case - so the result is checked rather than assumed.
OBSERVED="$(cat "/sys/class/block/$PART_NAME/size" 2>/dev/null || echo 0)"
if [ "$OBSERVED" != "$TARGET" ]; then
    log "kernel still reports $OBSERVED sectors, not $TARGET; retrying the re-read"
    partprobe "$DISK" >/dev/null 2>&1 || true
    blockdev --rereadpt "$DISK" >/dev/null 2>&1 || true
    udevadm settle 2>/dev/null || true
    OBSERVED="$(cat "/sys/class/block/$PART_NAME/size" 2>/dev/null || echo 0)"
fi
log "kernel reports $PART as $OBSERVED sectors (target $TARGET)"

if ! command -v fatresize >/dev/null 2>&1; then
    log "fatresize not installed; the partition is larger but the filesystem is not"
    log "done"
    exit 0
fi

# The output is kept. Swallowing it is what made this failure opaque: the log
# said only "fatresize failed", which is true of every possible cause.
#
# errexit is lifted around this deliberately. When a command substitution's
# command fails, the ASSIGNMENT fails, and under `set -e` the shell exits
# there and then - so "RESIZE_STATUS=$?" never runs and nothing is logged.
# The previous attempt to capture this error was itself destroyed by the
# error: the log stopped mid-function with no fatresize line at all.
# "Ignore" is piped in because fatresize asks a question.
#
# libparted notices that the hybrid ISO's driver descriptor claims 2048-byte
# blocks while the kernel reports the stick's real 512, warns about it, and
# prompts "Ignore/Cancel:". With no stdin it reads EOF, treats that as Cancel
# and exits 1 - the same shape of fault as nvme-cli's confirmation prompt.
# The discrepancy is expected and harmless here: it is an artefact of writing
# an ISO image to a USB stick, and it says nothing about the FAT32 filesystem
# in partition 3.
set +e
RESIZE_OUTPUT="$(printf 'Ignore\n%.0s' 1 2 3 | fatresize -s max "$PART" 2>&1)"
RESIZE_STATUS=$?
set -e
[ -n "$RESIZE_OUTPUT" ] && log "fatresize: $RESIZE_OUTPUT"

if [ "$RESIZE_STATUS" -eq 0 ]; then
    log "filesystem grown"
    log "done"
    exit 0
fi

log "fatresize exited $RESIZE_STATUS"

# fatresize could not do it, so rebuild the filesystem instead.
#
# libparted asks "Ignore/Cancel:" about a block-size discrepancy that is an
# artefact of writing an ISO to a USB stick, and it reads the answer from the
# TERMINAL rather than from stdin - so piping "Ignore" into it changes
# nothing, which two builds confirmed. There is no way to answer it from a
# boot-time service.
#
# The contents are therefore copied off, the filesystem is recreated at the
# full partition size, and the contents are copied back. That is reliable
# where the resize is not, and this volume holds certificates and logs
# measured in hundreds of kilobytes - it is not a general-purpose disk where
# such a trade would be unreasonable.
#
# Every step is checked before the destructive one. If the copy off fails, or
# there is not enough room to stage it, or the file count does not match
# afterwards, the volume is left exactly as it was: a small working partition
# beats a large empty one that used to hold someone's certificates.
STAGING=""
STAGED_FILES=0
PROBE="$(mktemp -d)"

if mount -t vfat -o ro "$PART" "$PROBE" 2>/dev/null; then
    USED_KB="$(du -sk "$PROBE" 2>/dev/null | cut -f1)"
    [ -n "$USED_KB" ] || USED_KB=0
    AVAIL_KB="$(df -Pk /var/tmp 2>/dev/null | awk 'NR==2 {print $4}')"
    [ -n "$AVAIL_KB" ] || AVAIL_KB=0

    # 512 MiB ceiling, and room for twice what is being staged. Refusing is
    # always safe here; the volume simply stays its current size.
    if [ "$USED_KB" -gt 524288 ]; then
        log "the volume holds ${USED_KB} KiB, too much to stage safely; leaving it alone"
    elif [ "$AVAIL_KB" -lt "$((USED_KB * 2 + 65536))" ]; then
        log "not enough room in /var/tmp to stage ${USED_KB} KiB; leaving the volume alone"
    else
        STAGING="$(mktemp -d /var/tmp/zeroize-certstore-XXXXXX)"
        if cp -a "$PROBE/." "$STAGING/" 2>/dev/null; then
            STAGED_FILES="$(find "$STAGING" -type f 2>/dev/null | wc -l)"
            log "staged $STAGED_FILES file(s), ${USED_KB} KiB, from $PART"
        else
            log "could not stage the volume contents; leaving it alone"
            rm -rf "$STAGING"
            STAGING=""
        fi
    fi
    umount "$PROBE" 2>/dev/null || true
else
    # Nothing mountable means nothing to lose.
    STAGING="$(mktemp -d /var/tmp/zeroize-certstore-XXXXXX)"
    log "the volume could not be mounted to read; rebuilding it empty"
fi

rmdir "$PROBE" 2>/dev/null || true

if [ -n "$STAGING" ]; then
    if mkfs.vfat -F 32 -n "$LABEL" "$PART" >/dev/null 2>&1; then
        log "filesystem rebuilt at full size"

        if [ "$STAGED_FILES" -gt 0 ]; then
            RESTORE="$(mktemp -d)"
            if mount -t vfat -o rw "$PART" "$RESTORE" 2>/dev/null; then
                cp -a "$STAGING/." "$RESTORE/" 2>/dev/null || true
                RESTORED="$(find "$RESTORE" -type f 2>/dev/null | wc -l)"
                sync
                umount "$RESTORE" 2>/dev/null || true
                if [ "$RESTORED" -eq "$STAGED_FILES" ]; then
                    log "restored $RESTORED of $STAGED_FILES file(s)"
                else
                    log "WARNING restored only $RESTORED of $STAGED_FILES file(s); "
                    log "WARNING a copy is kept at $STAGING"
                    STAGING=""
                fi
            else
                log "WARNING could not remount $PART to restore; copy kept at $STAGING"
                STAGING=""
            fi
            rmdir "$RESTORE" 2>/dev/null || true
        fi
    else
        log "mkfs.vfat failed; the partition is larger but the filesystem is not"
    fi

    # Only removed once the contents are known to be back on the volume.
    [ -n "$STAGING" ] && rm -rf "$STAGING"
fi

log "done"
EXPAND
chmod +x config/includes.chroot/usr/local/sbin/zeroize-expand-certstore

mkdir -p config/includes.chroot/etc/systemd/system
cat > config/includes.chroot/etc/systemd/system/zeroize-expand-certstore.service <<'UNIT'
[Unit]
Description=Grow the Zeroize certificate partition to fill the boot medium
Documentation=man:zeroize(1)
# Ordered before the display manager because the desktop session is what
# mounts the volume, and fatresize needs it unmounted.
After=local-fs.target
Before=display-manager.service graphical.target
ConditionPathExists=/run/live/medium

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/zeroize-expand-certstore
# Never block the boot: a stick that cannot be grown should still wipe drives.
TimeoutStartSec=180
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

# ---------------------------------------------------------------------------
# Certificate destination
# ---------------------------------------------------------------------------
# The live filesystem is a tmpfs: anything written to it is gone at power off,
# which would take the certificates with it. This README tells the operator
# where to put them, and the directory is created so the application has a
# writable default.
mkdir -p "config/includes.chroot/etc/skel/Zeroize Certificates"
cat > "config/includes.chroot/etc/skel/Zeroize Certificates/READ ME FIRST.txt" <<'NOTE'
If certificates are landing in THIS folder, they are in RAM and will be LOST
when this machine powers off. Copy them off before shutting down.

To have them written somewhere that survives, plug in a USB stick with a
partition labelled  ZEROIZE-OUT  and run the erase again. Zeroize finds it
automatically and writes to "Zeroize Certificates" on it, which Windows and
Linux both read as an ordinary drive.

That can be a second partition on this same boot stick, or a separate stick.

The result screen always shows the full path a certificate was written to, so
check it there if you are unsure which happened.
NOTE

# ---------------------------------------------------------------------------
# Branding the session
# ---------------------------------------------------------------------------
# The display still blanks and powers down on idle - that is wanted, it saves
# a monitor on a bench machine left running overnight. What is disabled is the
# lock that would normally come with it.
mkdir -p config/includes.chroot/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml
cat > config/includes.chroot/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml <<'XFPOWER'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-power-manager" version="1.0">
  <property name="xfce4-power-manager" type="empty">
    <!-- Blank after 10 minutes, power the panel down after 15. -->
    <property name="dpms-enabled" type="bool" value="true"/>
    <property name="blank-on-ac" type="uint" value="10"/>
    <property name="dpms-on-ac-sleep" type="uint" value="12"/>
    <property name="dpms-on-ac-off" type="uint" value="15"/>
    <property name="blank-on-battery" type="uint" value="10"/>
    <property name="dpms-on-battery-sleep" type="uint" value="12"/>
    <property name="dpms-on-battery-off" type="uint" value="15"/>
    <!-- Never ask for a credential the live account does not have. -->
    <property name="lock-screen-suspend-hibernate" type="bool" value="false"/>
    <!-- Do not suspend the machine itself: a wipe in progress must not be
         interrupted because nobody touched the keyboard for an hour. -->
    <property name="inactivity-on-ac" type="uint" value="0"/>
    <property name="inactivity-on-battery" type="uint" value="0"/>
    <property name="logind-handle-lid-switch" type="bool" value="false"/>
  </property>
</channel>
XFPOWER

cat > config/includes.chroot/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-screensaver.xml <<'XFSAVER'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-screensaver" version="1.0">
  <property name="saver" type="empty">
    <property name="enabled" type="bool" value="false"/>
    <property name="idle-activation" type="empty">
      <property name="enabled" type="bool" value="false"/>
    </property>
  </property>
  <property name="lock" type="empty">
    <property name="enabled" type="bool" value="false"/>
    <property name="saver-activation" type="empty">
      <property name="enabled" type="bool" value="false"/>
    </property>
  </property>
</channel>
XFSAVER

cat > config/includes.chroot/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml <<'XFSESSION'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-session" version="1.0">
  <property name="shutdown" type="empty">
    <property name="LockScreen" type="bool" value="false"/>
  </property>
  <property name="general" type="empty">
    <property name="LockCommand" type="string" value=""/>
  </property>
</channel>
XFSESSION

cat > config/includes.chroot/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml <<'XFDESKTOP'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-desktop" version="1.0">
  <property name="desktop-icons" type="empty">
    <property name="file-icons" type="empty">
      <property name="show-home" type="bool" value="false"/>
      <property name="show-filesystem" type="bool" value="false"/>
      <property name="show-removable" type="bool" value="true"/>
      <property name="show-trash" type="bool" value="false"/>
    </property>
  </property>
</channel>
XFDESKTOP

# ---------------------------------------------------------------------------
# Chroot hook
# ---------------------------------------------------------------------------
cat > config/hooks/live/9000-zeroize.hook.chroot <<'HOOK'
#!/bin/sh
set -e

# The .deb was dropped into packages.chroot, so live-build has installed it
# already. This hook covers what installing a package cannot: refreshing the
# caches the desktop reads at login, and making sure the directories the
# application expects exist in the live filesystem.

if [ -x /usr/bin/gtk-update-icon-cache ]; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
if [ -x /usr/bin/update-desktop-database ]; then
    update-desktop-database -q || true
fi

install -d -m 0755 /var/log/zeroize
install -d -m 0755 /var/lib/zeroize

# Nothing may lock the screen. The live account has no password, so a lock
# is unrecoverable without a reboot - and a reboot in the middle of a wipe is
# considerably worse than an unlocked bench machine that is about to destroy
# the drives in front of it anyway.
DEBIAN_FRONTEND=noninteractive apt-get -y purge     light-locker xfce4-screensaver xscreensaver 2>/dev/null || true

# xfce4-session ships its own /etc/xdg/autostart/xscreensaver.desktop stub, so
# purging xscreensaver does not remove it - it survives, pointing at a wrapper
# that is no longer installed, and fails at every login. Harmless but noisy,
# and it makes the image look like it still has a screen locker when it does
# not. Removed by name rather than by package.
rm -f /etc/xdg/autostart/xscreensaver.desktop       /etc/xdg/autostart/light-locker.desktop       /etc/xdg/autostart/xfce4-screensaver.desktop || true
apt-get -y autoremove --purge 2>/dev/null || true

# Grow the certificate partition to fill the stick on first boot.
systemctl enable zeroize-expand-certstore.service || true

# Use the Zeroize boot splash if it was staged.
if [ -f /usr/share/plymouth/themes/zeroize/zeroize.plymouth ]; then
    plymouth-set-default-theme zeroize || true
    update-initramfs -u || true
fi

# Debian keeps /usr/sbin off a normal user's PATH, so rtcwake, blkdiscard,
# hdparm and nvme all report "command not found" to the operator even though
# they are installed. On an appliance whose entire purpose needs those tools,
# that is just an obstacle.
cat > /etc/profile.d/zeroize-path.sh <<'PROFILE'
case ":$PATH:" in
    *:/usr/sbin:*) ;;
    *) PATH="$PATH:/usr/sbin:/sbin" ; export PATH ;;
esac
PROFILE

# Nothing on a wipe appliance should be offering to install itself to disk.
rm -f /usr/share/applications/*install*.desktop || true

# ---------------------------------------------------------------------------
# GRUB menu fonts, built with THIS release's grub-mkfont
# ---------------------------------------------------------------------------
# See the package list for why these are not built on the host. The binary
# hook copies them out of here into the boot tree.
#
# grub-mkfont does not store the name given to -n verbatim: it appends the
# style and the size, so "-n Zeroize Menu -s 22" must be referenced by the
# theme as "Zeroize Menu Regular 22". The theme file and these two commands
# have to agree exactly or the theme is silently discarded.
FONT_OUT=/usr/share/zeroize/grub-fonts
INTER=/usr/share/fonts/opentype/inter
if command -v grub-mkfont >/dev/null 2>&1 && [ -d "$INTER" ]; then
    mkdir -p "$FONT_OUT"
    grub-mkfont -s 22 -n "Zeroize Menu" -o "$FONT_OUT/menu.pf2"         "$INTER/Inter-Medium.otf" 2>/dev/null || true
    grub-mkfont -s 16 -n "Zeroize Hint" -o "$FONT_OUT/hint.pf2"         "$INTER/Inter-Regular.otf" 2>/dev/null || true
    if [ -s "$FONT_OUT/menu.pf2" ] && [ -s "$FONT_OUT/hint.pf2" ]; then
        echo "Built GRUB menu fonts with $(grub-mkfont --version | head -1)"
    else
        echo "WARNING: grub-mkfont produced no usable font in the chroot" >&2
        rm -f "$FONT_OUT/menu.pf2" "$FONT_OUT/hint.pf2"
    fi
else
    echo "WARNING: grub-mkfont or Inter missing in the chroot" >&2
fi

echo "Zeroize live image hook complete."
HOOK
chmod +x config/hooks/live/9000-zeroize.hook.chroot

# ---------------------------------------------------------------------------
# Branding: the two screens shown before the desktop appears
# ---------------------------------------------------------------------------
# 1. The GRUB menu background. live-build bakes the distribution name, build
#    date and package versions into boot/grub/splash.png as a picture, so
#    replacing that one file replaces the whole Debian-branded block.
# 2. The Plymouth splash between GRUB and the desktop.
#
# Both images are rendered from the same geometry as the application icon, in
# zeroize/branding.py, so the mark has one definition and nothing drifts.
SOURCE_ROOT="${ZEROIZE_SOURCE_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
BRAND_DIR="$(mktemp -d)"

# The wordmark is set in Inter. fontconfig substitutes silently when it is
# missing, so the artwork still renders - in DejaVu Sans, which is the default
# on a bare build host and looks nothing like the brand. Nothing downstream
# complains, and the substitution is only visible by looking at the finished
# boot screen. Hence an explicit check.
if command -v fc-list >/dev/null 2>&1; then
    DISPLAY_FONT=""
    for CANDIDATE in Inter Roboto Cantarell; do
        if fc-list : family 2>/dev/null | tr "," "
" | grep -qix "$CANDIDATE"; then
            DISPLAY_FONT="$CANDIDATE"
            break
        fi
    done
    if [ -n "$DISPLAY_FONT" ]; then
        echo "==> Display font: $DISPLAY_FONT"
    else
        echo "WARNING: none of Inter, Roboto or Cantarell is installed on this" >&2
        echo "         build host, so the wordmark will render in DejaVu Sans." >&2
        echo "         Install the brand font:  sudo apt install fonts-inter" >&2
    fi
fi

ZEROIZE_BUILD_STAMP="${ZEROIZE_BUILD_STAMP:-$(date -u "+%Y-%m-%d %H:%M UTC")}"
export ZEROIZE_BUILD_STAMP

if command -v rsvg-convert >/dev/null 2>&1 && [ -f "$SOURCE_ROOT/zeroize/branding.py" ]; then
    python3 - "$BRAND_DIR" "$SOURCE_ROOT" <<'BRANDPY'
import os
import sys
from pathlib import Path

target = Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
from zeroize import __tagline__, __version__

# The build stamp is printed on both boot screens. Two images built minutes
# apart are otherwise indistinguishable once written to a stick, and telling
# them apart by symptom - which is what tonight came down to - is no way to
# run a bench.
stamp = os.environ.get("ZEROIZE_BUILD_STAMP", "")
footer = f"version {__version__}" + (f"  -  build {stamp}" if stamp else "")
from zeroize.branding import (
    boot_wordmark_svg,
    plymouth_logo_svg,
    spinner_svg,
    splash_svg,
    wallpaper_svg,
)

(target / "splash.svg").write_text(
    splash_svg(1920, 1080, tagline=__tagline__, footer=footer),
    encoding="utf-8",
)
(target / "wallpaper.svg").write_text(
    wallpaper_svg(1920, 1080, tagline=__tagline__, footer=footer),
    encoding="utf-8",
)
(target / "logo.svg").write_text(plymouth_logo_svg(320), encoding="utf-8")
(target / "wordmark.svg").write_text(boot_wordmark_svg(640, tagline=__tagline__), encoding="utf-8")
(target / "spinner.svg").write_text(spinner_svg(96), encoding="utf-8")
print("rendered branding SVGs")
BRANDPY

    rsvg-convert -w 1920 -h 1080 -o "$BRAND_DIR/splash.png" "$BRAND_DIR/splash.svg"
    rsvg-convert -w 1920 -h 1080 -o "$BRAND_DIR/wallpaper.png" "$BRAND_DIR/wallpaper.svg"
    rsvg-convert -w 320 -h 320 -o "$BRAND_DIR/logo.png" "$BRAND_DIR/logo.svg"
    rsvg-convert -w 640 -o "$BRAND_DIR/wordmark.png" "$BRAND_DIR/wordmark.svg"
    rsvg-convert -w 96 -h 96 -o "$BRAND_DIR/spinner.png" "$BRAND_DIR/spinner.svg"
    echo "==> Generated boot artwork"
else
    if ! command -v rsvg-convert >/dev/null 2>&1; then
        echo "WARNING: rsvg-convert is not installed (apt install librsvg2-bin);" >&2
    else
        echo "WARNING: no branding module at $SOURCE_ROOT/zeroize/branding.py;" >&2
        echo "         set ZEROIZE_SOURCE_ROOT to the checkout." >&2
    fi
    echo "         the boot screens will keep Debian's artwork." >&2
fi

# --- GRUB and isolinux splash --------------------------------------------
# live-build renders the distribution name, build date and package versions
# into splash.png as a picture, so replacing that file replaces the whole
# Debian-branded block on the boot menu. It is staged here and copied into the
# binary tree by the hook below, which runs after the bootloader stages.
if [ -f "$BRAND_DIR/splash.png" ]; then
    mkdir -p config/includes.binary/boot/grub config/includes.binary/isolinux
    cp "$BRAND_DIR/splash.png" config/includes.binary/boot/grub/splash.png
    cp "$BRAND_DIR/splash.png" config/includes.binary/isolinux/splash.png
    echo "==> Staged the boot splash"
fi

# --- Desktop wallpaper ----------------------------------------------------
# The same visual language as the two boot screens, so the machine does not
# change identity the moment the desktop appears. xfdesktop is pointed at it
# through xfconf rather than by replacing Debian's default image, because the
# default is owned by a package and would come back on any update.
if [ -f "$BRAND_DIR/wallpaper.png" ]; then
    mkdir -p config/includes.chroot/usr/share/backgrounds/zeroize \n             config/includes.chroot/usr/local/bin
    cp "$BRAND_DIR/wallpaper.png"         config/includes.chroot/usr/share/backgrounds/zeroize/wallpaper.png

    mkdir -p config/includes.chroot/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml
    cat > config/includes.chroot/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml <<'XFWALL'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-desktop" version="1.0">
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitor0" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="5"/>
          <property name="last-image" type="string"
                    value="/usr/share/backgrounds/zeroize/wallpaper.png"/>
        </property>
      </property>
      <!-- monitorVGA-1 and friends: xfdesktop keys the backdrop on the
           connector name, which is not knowable at build time. A wildcard
           property is not supported, so the common connectors are listed.
           One of them matches on any machine with a single display. -->
      <property name="monitorLVDS-1" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="5"/>
          <property name="last-image" type="string"
                    value="/usr/share/backgrounds/zeroize/wallpaper.png"/>
        </property>
      </property>
      <property name="monitoreDP-1" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="5"/>
          <property name="last-image" type="string"
                    value="/usr/share/backgrounds/zeroize/wallpaper.png"/>
        </property>
      </property>
      <property name="monitorHDMI-1" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="5"/>
          <property name="last-image" type="string"
                    value="/usr/share/backgrounds/zeroize/wallpaper.png"/>
        </property>
      </property>
      <property name="monitorDP-1" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="5"/>
          <property name="last-image" type="string"
                    value="/usr/share/backgrounds/zeroize/wallpaper.png"/>
        </property>
      </property>
      <property name="monitorVGA-1" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="5"/>
          <property name="last-image" type="string"
                    value="/usr/share/backgrounds/zeroize/wallpaper.png"/>
        </property>
      </property>
    </property>
  </property>
  <property name="desktop-icons" type="empty">
    <property name="style" type="int" value="2"/>
    <property name="file-icons" type="empty">
      <property name="show-home" type="bool" value="false"/>
      <property name="show-filesystem" type="bool" value="false"/>
      <property name="show-removable" type="bool" value="true"/>
      <property name="show-trash" type="bool" value="false"/>
    </property>
  </property>
</channel>
XFWALL
    echo "==> Staged the desktop wallpaper"
    # xfconf keys the backdrop on the monitor's CONNECTOR name -
    # monitoreDP-1, monitorHDMI-2, monitorVGA-1 - which is a property of the
    # machine, not of the image, so it cannot be written into a skeleton file
    # at build time. Listing the likely names and hoping one matches is what
    # the XML above does, and on hardware whose connector was not on the list
    # the desktop simply kept Debian's default background.
    #
    # So the XML is only a starting point. This runs once the session is up,
    # asks xfdesktop what it actually found, and sets every backdrop property
    # it reports.
    cat > config/includes.chroot/usr/local/bin/zeroize-set-wallpaper <<'WALLSCRIPT'
#!/bin/sh
# Apply the Zeroize wallpaper to every monitor xfdesktop knows about.
#
# xfconf keys the backdrop on the monitor's CONNECTOR name - monitoreDP-1,
# monitorHDMI-2, monitorVGA-1 - which is a property of the machine, not of the
# image, so it cannot be written into a skeleton file at build time. Listing
# likely names and hoping one matches does not work; that is what left the
# desktop on Debian's default background.
#
# Two sources are used, because neither alone is reliable. xfconf-query only
# lists backdrop properties that already exist, and xfdesktop may not have
# created any yet; xrandr names the connectors that are physically attached,
# which is what those properties will be called. Setting both covers the case
# where xfdesktop is slow and the case where it uses a name xrandr spells
# differently.
set -u

WALLPAPER=/usr/share/backgrounds/zeroize/wallpaper.png
LOGFILE=/var/log/zeroize/wallpaper.log
mkdir -p /var/log/zeroize 2>/dev/null || true
log() {
    echo "zeroize-set-wallpaper: $*"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGFILE" 2>/dev/null || true
}

if [ ! -f "$WALLPAPER" ]; then
    log "no wallpaper at $WALLPAPER"
    exit 0
fi
if ! command -v xfconf-query >/dev/null 2>&1; then
    log "xfconf-query is not installed"
    exit 0
fi

# image-style 5 is "zoomed": fills the screen preserving aspect, which is what
# a 1920x1080 image wants on a panel of any other shape.
STYLE=5

apply() {
    PROP="$1"
    xfconf-query -c xfce4-desktop -p "$PROP" -n -t string -s "$WALLPAPER" 2>/dev/null \
        || xfconf-query -c xfce4-desktop -p "$PROP" -s "$WALLPAPER" 2>/dev/null \
        || { log "could not set $PROP"; return 1; }
    STYLE_PROP=$(echo "$PROP" | sed 's/last-image$/image-style/')
    xfconf-query -c xfce4-desktop -p "$STYLE_PROP" -n -t int -s "$STYLE" 2>/dev/null \
        || xfconf-query -c xfce4-desktop -p "$STYLE_PROP" -s "$STYLE" 2>/dev/null || true
    log "set $PROP"
    return 0
}

# Wait for the session to be ready enough to have a display.
ATTEMPT=0
while [ "$ATTEMPT" -lt 30 ]; do
    if xfconf-query -c xfce4-desktop -l >/dev/null 2>&1; then
        break
    fi
    ATTEMPT=$((ATTEMPT + 1))
    sleep 0.5
done

APPLIED=0

# 1. Whatever xfdesktop has already created.
for PROP in $(xfconf-query -c xfce4-desktop -l 2>/dev/null | grep -E 'last-image$'); do
    apply "$PROP" && APPLIED=$((APPLIED + 1))
done

# 2. Every connected output xrandr reports, whether or not xfconf knows it yet.
if command -v xrandr >/dev/null 2>&1; then
    for OUTPUT in $(xrandr --query 2>/dev/null | awk '/ connected/ { print $1 }'); do
        apply "/backdrop/screen0/monitor$OUTPUT/workspace0/last-image" \
            && APPLIED=$((APPLIED + 1))
    done
else
    log "xrandr is not installed; relying on xfconf's own list"
fi

# 3. The generic fallback some xfdesktop versions use when there is one screen.
apply "/backdrop/screen0/monitor0/workspace0/last-image" && APPLIED=$((APPLIED + 1))

log "applied to $APPLIED backdrop propert(ies)"
xfdesktop --reload 2>/dev/null || log "xfdesktop --reload failed"
WALLSCRIPT
    chmod +x config/includes.chroot/usr/local/bin/zeroize-set-wallpaper

    cat > config/includes.chroot/etc/xdg/autostart/zeroize-wallpaper.desktop <<'WALLAUTO'
[Desktop Entry]
Type=Application
Version=1.0
Name=Zeroize wallpaper
Comment=Apply the Zeroize wallpaper to every connected monitor
Exec=/usr/local/bin/zeroize-set-wallpaper
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
OnlyShowIn=XFCE;
WALLAUTO
fi

# --- Plymouth theme -------------------------------------------------------
if [ -f "$BRAND_DIR/logo.png" ]; then
    THEME_DIR=config/includes.chroot/usr/share/plymouth/themes/zeroize
    mkdir -p "$THEME_DIR"
    cp "$BRAND_DIR/logo.png" "$THEME_DIR/logo.png"

    cat > "$THEME_DIR/zeroize.plymouth" <<'PLYMOUTH'
[Plymouth Theme]
Name=Zeroize
Description=Zeroize Drive Wiper boot splash
ModuleName=script

[script]
ImageDir=/usr/share/plymouth/themes/zeroize
ScriptFile=/usr/share/plymouth/themes/zeroize/zeroize.script
PLYMOUTH

    # A deliberately plain splash: the mark, centred, on the brand ground,
    # with a progress dot row. No spinner artwork to ship and nothing that
    # needs a compositor - this runs before anything graphical is up.
    cp "$BRAND_DIR/wordmark.png" "$THEME_DIR/wordmark.png" 2>/dev/null || true
    cp "$BRAND_DIR/spinner.png" "$THEME_DIR/spinner.png" 2>/dev/null || true

    SCRIPT_SRC="$SOURCE_ROOT/packaging/iso/plymouth/zeroize.script"
    if [ -f "$SCRIPT_SRC" ]; then
        install -m 0644 "$SCRIPT_SRC" "$THEME_DIR/zeroize.script"
    else
        echo "WARNING: $SCRIPT_SRC is missing; the boot splash will be the mark alone." >&2
        printf '%s
' 'Window.SetBackgroundTopColor(0.086, 0.094, 0.114);'             'Window.SetBackgroundBottomColor(0.086, 0.094, 0.114);'             'logo.image = Image("logo.png");'             'logo.sprite = Sprite(logo.image);'             'logo.sprite.SetX(Window.GetWidth() / 2 - logo.image.GetWidth() / 2);'             'logo.sprite.SetY(Window.GetHeight() / 2 - logo.image.GetHeight() / 2);'             > "$THEME_DIR/zeroize.script"
    fi
fi

# ---------------------------------------------------------------------------
# Binary hook: make the EFI bootloader's medium search resilient
# ---------------------------------------------------------------------------
# The EFI system partition carries a three-line stub whose only job is to find
# the ISO 9660 filesystem and hand over to the real menu. live-build's stub is:
#
#     search --set=root --file /.disk/info
#     set prefix=($root)/boot/grub
#     configfile ($root)/boot/grub/grub.cfg
#
# When that search finds nothing, $root is left pointing at the EFI partition -
# whose /boot/grub/grub.cfg is the stub itself. GRUB then either re-reads the
# stub or gives up, and the operator gets a bare `grub>` prompt with no menu
# and no explanation. Observed on HP firmware, which presents a hybrid ISO
# written to USB as a CD-ROM device rather than a disk.
#
# The replacement searches into a fresh variable so "not found" is actually
# detectable, then falls back to the volume label and finally to probing the
# devices directly. If everything fails it prints the device list and the two
# commands needed to boot by hand, which beats a bare prompt considerably.
#
# This runs as a *binary* hook: live-build executes those with the working
# directory set to the binary tree, after the bootloader stages have produced
# efi.img and before the ISO is assembled.
mkdir -p config/hooks/live
cat > config/hooks/live/9100-efi-search.hook.binary <<'BINHOOK'
#!/bin/sh
set -e

# Binary hooks run with the working directory inside the binary tree, but be
# tolerant of being invoked from the build root instead.
EFI_IMAGE=""
for CANDIDATE in boot/grub/efi.img binary/boot/grub/efi.img; do
    if [ -f "$CANDIDATE" ]; then
        EFI_IMAGE="$CANDIDATE"
        break
    fi
done

if [ -z "$EFI_IMAGE" ]; then
    echo "WARNING: no efi.img found; the EFI medium search was NOT hardened." >&2
    exit 0
fi

if ! command -v mcopy >/dev/null 2>&1; then
    echo "WARNING: mtools is not installed; the EFI medium search was NOT hardened." >&2
    exit 0
fi

STUB="$(mktemp)"
cat > "$STUB" <<'CFG'
# Locate the medium this image was written to.
#
# Searching into a dedicated variable, not into $root: GRUB has already set
# $root to the EFI partition it loaded from, so a failed search would leave it
# pointing there - and this file is that partition's grub.cfg.

search --no-floppy --file --set=medium /.disk/info

if [ -z "$medium" ]; then search --no-floppy --label --set=medium ZEROIZE; fi

# Some firmware does not expose a hybrid ISO on USB to GRUB's search at all,
# but will read it when named directly. Probe the usual devices.
if [ -z "$medium" ]; then if [ -e (cd0)/.disk/info ]; then set medium=cd0; fi; fi
if [ -z "$medium" ]; then if [ -e (cd1)/.disk/info ]; then set medium=cd1; fi; fi
if [ -z "$medium" ]; then if [ -e (hd0)/.disk/info ]; then set medium=hd0; fi; fi
if [ -z "$medium" ]; then if [ -e (hd1)/.disk/info ]; then set medium=hd1; fi; fi
if [ -z "$medium" ]; then if [ -e (hd2)/.disk/info ]; then set medium=hd2; fi; fi
if [ -z "$medium" ]; then if [ -e (hd3)/.disk/info ]; then set medium=hd3; fi; fi

if [ -n "$medium" ]; then
    set root=$medium
    set prefix=($root)/boot/grub
    configfile ($root)/boot/grub/grub.cfg
fi

echo ""
echo "Zeroize: the boot medium could not be located automatically."
echo ""
echo "Devices this firmware exposes to GRUB:"
ls
echo ""
echo "Find the one holding /live, then boot it with:"
echo "    set root=(DEVICE)"
echo "    configfile (DEVICE)/boot/grub/grub.cfg"
echo ""
echo "A stick that was pulled without being safely ejected is the usual cause."
echo ""
sleep 60
CFG

mcopy -i "$EFI_IMAGE" -D o "$STUB" ::/boot/grub/grub.cfg
rm -f "$STUB"

echo "Hardened the EFI medium search in $EFI_IMAGE"
BINHOOK
chmod +x config/hooks/live/9100-efi-search.hook.binary

# ---------------------------------------------------------------------------
# Boot menu and desktop presentation
# ---------------------------------------------------------------------------
# live-build writes a menu that says "Live system (amd64)" at 800x600. On a
# modern panel that is a small, soft, generic screen, and the name on it is not
# the name of this tool.
#
# The hook and the GRUB theme are kept as real files under packaging/iso/
# rather than written here from a heredoc. They are dense with backslashes -
# sed expressions, GRUB theme syntax - and a heredoc inside a heredoc is one
# escaping layer too many: an earlier revision of this script silently turned
# a sed backreference into a 0x01 byte. Files that are copied cannot be
# mangled by the copying.
HOOK_SRC="$SOURCE_ROOT/packaging/iso/hooks/9200-boot-menu.hook.binary"
if [ -f "$HOOK_SRC" ]; then
    install -m 0755 "$HOOK_SRC" config/hooks/live/9200-boot-menu.hook.binary
    echo "==> Staged the boot menu hook"
else
    echo "WARNING: $HOOK_SRC is missing; the boot menu keeps Debian's wording." >&2
fi

THEME_SRC="$SOURCE_ROOT/packaging/iso/grub-theme/theme.txt"
if [ -f "$THEME_SRC" ]; then
    mkdir -p config/includes.binary/boot/grub/zeroize
    install -m 0644 "$THEME_SRC" config/includes.binary/boot/grub/zeroize/theme.txt
    # The fallback names no fonts, so the branded menu survives a font that
    # the image's GRUB will not load. See the file's own header.
    FALLBACK_SRC="$SOURCE_ROOT/packaging/iso/grub-theme/theme-fallback.txt"
    [ -f "$FALLBACK_SRC" ] && install -m 0644 "$FALLBACK_SRC"         config/includes.binary/boot/grub/zeroize/theme-fallback.txt
    echo "==> Staged the GRUB theme"
fi

# --- GRUB menu fonts ------------------------------------------------------
# GRUB cannot read OTF or TTF; it needs its own bitmap format, PF2, generated
# at each size that will be displayed. Without this the theme can only ask for
# unicode.pf2 - a monospaced bitmap face that makes a 2026 boot menu look like
# a 1998 one, which was the whole complaint.
#
# The name grub-mkfont writes into the file is what the theme matches on, and
# it is not the name passed to -n: the tool appends the style and size, so
# "-n Zeroize Menu -s 22" is referenced as "Zeroize Menu Regular 22". Getting
# that string wrong makes the theme fail to parse, and GRUB then silently
# falls back to its legacy menu - which is exactly how the first attempt at
# this looked like it had done nothing.
INTER_DIR=""
for CANDIDATE in /usr/share/fonts/opentype/inter /usr/share/fonts/truetype/inter; do
    [ -d "$CANDIDATE" ] && INTER_DIR="$CANDIDATE" && break
done

if command -v grub-mkfont >/dev/null 2>&1 && [ -n "$INTER_DIR" ]; then
    FONT_DIR=config/includes.binary/boot/grub/zeroize/fonts
    mkdir -p "$FONT_DIR"
    # Medium for the menu: at 22px Regular is a little light against charcoal.
    grub-mkfont -s 22 -n "Zeroize Menu"         -o "$FONT_DIR/menu.pf2" "$INTER_DIR/Inter-Medium.otf" 2>/dev/null
    grub-mkfont -s 16 -n "Zeroize Hint"         -o "$FONT_DIR/hint.pf2" "$INTER_DIR/Inter-Regular.otf" 2>/dev/null
    if [ -s "$FONT_DIR/menu.pf2" ] && [ -s "$FONT_DIR/hint.pf2" ]; then
        echo "==> Generated GRUB menu fonts from Inter"
    else
        echo "WARNING: grub-mkfont produced no usable font; the menu keeps unicode.pf2." >&2
        rm -f "$FONT_DIR/menu.pf2" "$FONT_DIR/hint.pf2"
    fi
else
    echo "WARNING: grub-mkfont or Inter is missing (apt install grub-common fonts-inter);" >&2
    echo "         the boot menu will use the default bitmap font." >&2
fi

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
echo "==> Running lb build (this downloads a base system and takes a while)"
lb build

IMAGE="$(find . -maxdepth 1 -name '*.iso' -o -maxdepth 1 -name '*.hybrid.iso' | head -n 1)"
if [ -z "$IMAGE" ]; then
    echo "live-build finished but produced no ISO." >&2
    exit 1
fi

FINAL="$OUTPUT_DIR/${IMAGE_NAME}.iso"
mv "$IMAGE" "$FINAL"
sha256sum "$FINAL" > "$FINAL.sha256"

# ---------------------------------------------------------------------------
# USB image: the same thing plus a writable certificate partition
# ---------------------------------------------------------------------------
# Certificates have to land somewhere that survives a power off, and the live
# filesystem is a tmpfs while the boot medium is read-only ISO 9660. The
# obvious answer - write the ISO, then add a partition with Windows diskpart -
# does not work: a hybrid ISO has a deliberately odd partition table, with a
# type 0x00 entry and the EFI partition nested inside its sector range, and
# Windows "repairs" that layout when it writes a new entry, breaking the boot.
#
# So the partition is appended here instead, where the layout is understood.
# Everything the firmware reads lives before it and is byte-identical to the
# ISO; a FAT32 filesystem is concatenated on the end and registered as MBR
# entry 3, which was unused. One write, one stick, and Windows mounts the
# certificate partition as an ordinary drive.
CERT_PARTITION_MB="${CERT_PARTITION_MB:-1024}"
CERT_LABEL="${CERT_LABEL:-ZEROIZE-OUT}"

if [ "$CERT_PARTITION_MB" -gt 0 ] && command -v mkfs.vfat >/dev/null 2>&1; then
    echo "==> Building the USB image with a ${CERT_PARTITION_MB} MB ${CERT_LABEL} partition"

    CERT_IMAGE="$WORK_DIR/certstore.img"
    truncate -s "${CERT_PARTITION_MB}M" "$CERT_IMAGE"
    mkfs.vfat -F 32 -n "$CERT_LABEL" "$CERT_IMAGE" >/dev/null

    USB_IMAGE="$OUTPUT_DIR/${IMAGE_NAME}-usb.img"
    cat "$FINAL" "$CERT_IMAGE" > "$USB_IMAGE"

    python3 - "$USB_IMAGE" "$(stat -c %s "$FINAL")" "$(stat -c %s "$CERT_IMAGE")" <<'MBRPY'
import struct
import sys

image_path, iso_bytes, data_bytes = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
SECTOR = 512

start_lba = iso_bytes // SECTOR
sector_count = data_bytes // SECTOR

if iso_bytes % SECTOR:
    raise SystemExit(f"the ISO is not a whole number of sectors ({iso_bytes} bytes)")

with open(image_path, "r+b") as image:
    image.seek(0)
    mbr = bytearray(image.read(SECTOR))

    # Entries 1 and 2 belong to the hybrid ISO. Refuse rather than overwrite
    # something the firmware needs.
    entry_offset = 0x1BE + 32
    existing_type = mbr[entry_offset + 4]
    if existing_type != 0:
        raise SystemExit(f"MBR entry 3 is already in use (type 0x{existing_type:02x})")

    # 0x0C is FAT32 with LBA addressing, which is what Windows and Linux both
    # expect on removable media. CHS fields are set to the "use LBA" sentinel.
    entry = struct.pack(
        "<B3sB3sII",
        0x00,                     # not bootable
        b"\xfe\xff\xff",          # CHS start: ignore, use LBA
        0x0C,                     # FAT32 LBA
        b"\xfe\xff\xff",          # CHS end: ignore, use LBA
        start_lba,
        sector_count,
    )
    mbr[entry_offset : entry_offset + 16] = entry

    image.seek(0)
    image.write(mbr)

print(f"registered partition 3: start LBA {start_lba}, {sector_count} sectors")
MBRPY

    sha256sum "$USB_IMAGE" > "$USB_IMAGE.sha256"
    echo "==> Built $USB_IMAGE"
else
    USB_IMAGE=""
    if [ "$CERT_PARTITION_MB" -gt 0 ]; then
        echo "WARNING: mkfs.vfat not found; no USB image with a certificate partition." >&2
    fi
fi

# ---------------------------------------------------------------------------
# Verify the Secure Boot chain
# ---------------------------------------------------------------------------
# The image is only useful on locked-down hardware if the firmware will accept
# it, and that is easy to break without noticing - a bootloader package that
# failed to install still produces a bootable-looking ISO that a Secure Boot
# machine silently refuses. So the chain is checked here, while the operator is
# still looking at the build.
#
# The 2011 Microsoft third-party UEFI CA expired in June 2026 and is being
# replaced by the Microsoft UEFI CA 2023. Debian's shim carries BOTH
# signatures, which is what lets one image boot on firmware that trusts either
# certificate. Losing the 2023 signature would leave the image unable to boot
# on newer hardware, so its presence is asserted rather than assumed.
verify_secure_boot() {
    if ! command -v sbverify >/dev/null 2>&1; then
        echo "==> sbverify not installed; skipping the Secure Boot check"
        echo "    (install sbsigntool to have the build verify the boot chain)"
        return 0
    fi

    local scratch
    scratch="$(mktemp -d)"
    if ! xorriso -osirrox on -indev "$FINAL" -extract /EFI/boot/bootx64.efi             "$scratch/bootx64.efi" >/dev/null 2>&1; then
        echo "WARNING: no /EFI/boot/bootx64.efi in the image - it will NOT boot under Secure Boot." >&2
        return 1
    fi

    local signatures
    signatures="$(sbverify --list "$scratch/bootx64.efi" 2>/dev/null || true)"

    echo "==> Secure Boot chain:"
    if printf '%s' "$signatures" | grep -q "Microsoft Corporation UEFI CA 2011"; then
        echo "    shim signed by Microsoft UEFI CA 2011  (older firmware)"
    else
        echo "    WARNING: shim is NOT signed by the Microsoft UEFI CA 2011;" >&2
        echo "             firmware predating the 2023 certificate rollout will refuse it." >&2
    fi
    if printf '%s' "$signatures" | grep -q "Microsoft UEFI CA 2023"; then
        echo "    shim signed by Microsoft UEFI CA 2023  (current firmware)"
    else
        echo "    WARNING: shim is NOT signed by the Microsoft UEFI CA 2023." >&2
        echo "             The 2011 CA expired in June 2026; hardware shipping with only the" >&2
        echo "             2023 certificate in its db will refuse this image." >&2
    fi
}

verify_secure_boot || true

echo "==> Built $FINAL"
echo
if [ -n "$USB_IMAGE" ]; then
    echo "For a USB stick, write the .img - it carries the ${CERT_LABEL}"
    echo "partition that certificates and logs are written to:"
    echo
    echo "    sudo dd if=$USB_IMAGE of=/dev/sdX bs=4M status=progress oflag=sync"
    echo
    echo "On Windows use Rufus in DD Image mode. Do NOT add a partition with"
    echo "diskpart afterwards - Windows rewrites the hybrid partition table and"
    echo "breaks the boot. The image already has the partition."
else
    echo "Write it to a USB stick with:"
    echo "    sudo dd if=$FINAL of=/dev/sdX bs=4M status=progress oflag=sync"
fi
echo
echo "Replace /dev/sdX with the stick, NOT a drive you want to keep."
