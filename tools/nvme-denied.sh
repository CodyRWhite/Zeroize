#!/bin/bash
# Why is this NVMe erase being refused? Round two.
#
# The first diagnostic (sed-check.sh) ruled out the easy answers: no namespace
# write protect, no read-only media, healthy spare. It also printed
#
#     TCG features: none
#
# which I read as "not a self-encrypting drive". That reading was too strong.
# That line is only reached when Level 0 Discovery SUCCEEDS and reports a
# non-zero length, and then finds a zero feature code at the first offset. A
# drive that is genuinely not TCG either refuses the Security Receive or
# returns a zero length. A real length followed by zeros is just as likely a
# parser reading at the wrong offset as it is a plain answer of "no".
#
# So this one does not interpret. It dumps the raw bytes, asks the drive which
# security protocols it supports at all, and collects the states that can deny
# a command without appearing anywhere the first script looked: reservations,
# a failed sanitize the controller has not cleared, and the kernel's own view.
#
# READ ONLY unless --attempt is given, which runs the real erase command so
# its full status can be captured.
set -u

# Re-exec under bash if started with sh: the logging below uses process
# substitution, and dash answers that with "Syntax error: redirection
# unexpected" on the first line of real work.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

# nvme and friends live in /usr/sbin, which is not on a normal user's PATH.
# Without this the script blames the packaging for a PATH difference.
PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"
export PATH

if [ "$(id -u)" -ne 0 ]; then
    echo "This needs root - every command it runs is privileged."
    echo "Try:  sudo bash $0 $*"
    exit 1
fi

ATTEMPT=""
TARGETS=()

usage() {
    cat <<'USAGE'
usage: nvme-denied.sh [--attempt sanitize|format] [/dev/nvme0 ...]

With no device, every NVMe controller is examined read-only.

  --attempt exit-failure   nvme sanitize <dev> --sanact=1   (NOT destructive)
  --attempt sanitize       nvme sanitize <dev> --sanact=2   (DESTRUCTIVE)
  --attempt format         nvme format <dev>n1 --ses=1 --force  (DESTRUCTIVE)

exit-failure is Sanitize Exit Failure Mode. A sanitize that failed part way
leaves the controller in a failed state, and in that state it denies further
erase commands - which is what "Access Denied" means here. Exit Failure Mode
clears the state and erases nothing, so it is the one to try first.

Name the device explicitly when using --attempt.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --attempt)
            ATTEMPT="${2:-}"
            shift 2 || true
            ;;
        --attempt=*)
            ATTEMPT="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        /dev/*)
            TARGETS+=("$1")
            shift
            ;;
        *)
            echo "unrecognised argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$ATTEMPT" in
    ""|exit-failure|sanitize|format) ;;
    *)
        echo "--attempt takes 'exit-failure', 'sanitize' or 'format', not '$ATTEMPT'" >&2
        exit 2
        ;;
esac

if [ -n "$ATTEMPT" ] && [ ${#TARGETS[@]} -ne 1 ]; then
    echo "--attempt needs exactly one device, so there is no doubt which drive" >&2
    echo "is about to be erased. Example:  $0 --attempt sanitize /dev/nvme0" >&2
    exit 2
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
    for CONTROLLER in /dev/nvme[0-9]; do
        [ -e "$CONTROLLER" ] && TARGETS+=("$CONTROLLER")
    done
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "No NVMe controllers found under /dev."
    exit 1
fi

# Where the report goes.
#
# DIAGS is a stick kept solely for carrying diagnostics off this machine. That
# it is SEPARATE from the boot medium is the point: pulling and reinserting the
# boot stick tears down the live session, because the squashfs the running
# system is reading from vanishes underneath it. So nothing here is written to
# the boot stick, and the diagnostics stick can be removed at any time.
#
# Order of preference: the DIAGS volume, then the certificate volume, then
# home. Home is a tmpfs on the live image and does not survive a power cycle,
# which is exactly when the report is wanted - so it is the last resort and it
# says so out loud rather than looking like a success.
resolve_report_root() {
    local label device mount_point

    for label in DIAGS ZEROIZE-OUT; do
        device="$(blkid -L "$label" 2>/dev/null || true)"
        [ -n "$device" ] || continue

        # Already mounted? Use where it is rather than mounting it a second
        # time somewhere else, which would leave two views of one filesystem
        # and the report on whichever one the reader did not look at.
        mount_point="$(findmnt -n -o TARGET --source "$device" 2>/dev/null | head -1)"
        if [ -z "$mount_point" ]; then
            mount_point="/media/$(printf '%s' "$label" | tr '[:upper:]' '[:lower:]')"
            mkdir -p "$mount_point" 2>/dev/null || continue
            mount "$device" "$mount_point" 2>/dev/null || continue
        fi

        if [ -d "$mount_point" ] && [ -w "$mount_point" ]; then
            echo "$mount_point"
            return 0
        fi
    done

    echo "$HOME"
}

REPORT_ROOT="$(resolve_report_root)"
REPORT_DIR="$REPORT_ROOT/Zeroize Diagnostics"
mkdir -p "$REPORT_DIR" 2>/dev/null || REPORT_DIR="$REPORT_ROOT"

LOG="$REPORT_DIR/nvme-denied-$(date +%Y-%m-%d-%H-%M-%S).txt"
exec > >(tee -a "$LOG") 2>&1

section() {
    echo
    echo "--- $* ---"
}

# Run a command, showing it first and its exit status after.
#
# Options differ between nvme-cli releases, and an option this build does not
# know is a usage error, not a drive answer - which reads in the log exactly
# like the drive refusing. Printing the command line and the exit status
# separates "the drive said no" from "the tool said no".
run() {
    echo "\$ $*"
    "$@" 2>&1
    echo "  [exit $?]"
}

# Does this build accept a global --verbose? Version 2.x added it; earlier
# builds reject it outright. Probed rather than assumed, because guessing
# wrong turns every attempt below into a usage error.
VERBOSE=()
if nvme sanitize --help 2>&1 | grep -q -- "--verbose"; then
    VERBOSE=(--verbose)
fi

echo "Zeroize NVMe Access-Denied diagnostic, round two"
echo "date    : $(date -Is)"
echo "kernel  : $(uname -sr)"
echo "nvme    : $(nvme version 2>&1 | head -1)"
echo "verbose : ${VERBOSE[*]:-not supported by this nvme-cli}"
echo "targets : ${TARGETS[*]}"
echo "attempt : ${ATTEMPT:-none (read only)}"
echo "report  : $LOG"
if [ "$REPORT_ROOT" = "$HOME" ]; then
    echo
    echo "!! Neither DIAGS nor ZEROIZE-OUT was found, so this landed in $HOME."
    echo "!! On the live image that is a tmpfs: copy it off before powering down."
fi

for CONTROLLER in "${TARGETS[@]}"; do
    echo
    echo "==================== $CONTROLLER ===================="
    NAMESPACE="${CONTROLLER}n1"

    # Unfiltered this time. The first script grepped for the fields I expected
    # to matter, which is fine right up until the answer is in a field I did
    # not expect.
    section "id-ctrl (full)"
    run nvme id-ctrl "$CONTROLLER" -H

    section "smart-log (full)"
    run nvme smart-log "$CONTROLLER" -H

    # -r retains the asynchronous event: reading this page without it tells the
    # controller to clear the event, which is the wrong thing to do when the
    # point is to observe state. SSTAT 011b means a PREVIOUS sanitize failed,
    # and a controller left in that state can deny commands until it is
    # restarted.
    section "sanitize-log (full)"
    run nvme sanitize-log "$CONTROLLER" -H -r

    # Which firmware is in which slot.
    #
    # frmw reports three slots on these drives, slot 1 writable, and activation
    # without reset supported. If a second slot holds a different revision it
    # can be activated with fw-commit alone - no image to find, no download, and
    # reversible by committing the original slot back. That is worth knowing
    # before going looking for a vendor firmware package, because for an OEM
    # drive there may not be one that is obtainable at all.
    section "firmware slots"
    run nvme fw-log "$CONTROLLER"

    section "list-ns (allocated namespaces)"
    run nvme list-ns "$CONTROLLER" -a

    if [ -e "$NAMESPACE" ]; then
        section "id-ns $NAMESPACE (full)"
        run nvme id-ns "$NAMESPACE" -H

        section "write-protect feature 0x84"
        run nvme get-feature "$NAMESPACE" -f 0x84 -H

        # A persistent reservation held by another host denies exactly this
        # way, and "persist through power loss" means it survives the move from
        # the drive's old machine to this bench. Neither the first script nor
        # the app looked here.
        section "reservation report"
        run nvme resv-report "$NAMESPACE" -e
    else
        section "$NAMESPACE"
        echo "  namespace node does not exist - the namespace may be unattached"
    fi

    # Protocol 0x00 is the list of security protocols the drive supports. If
    # 0x01 appears in it the drive speaks TCG, whatever Level 0 Discovery
    # parsed to.
    #
    # --al is the Allocation Length, and it is NOT --size.
    #
    # --size is how big the HOST buffer is. --al is how many bytes the
    # CONTROLLER is told to transfer, and it defaults to zero. The first two
    # versions of this diagnostic set only --size, so every Security Receive
    # asked the drive for nothing, got nothing, and handed the parser 2048
    # zero bytes - which it reported as "TCG features: none". The drive had
    # never been asked the question. Both are passed now, and the unparsed
    # hexdump goes in the log beside the parse so the next reader can see for
    # themselves rather than trusting the parser.
    section "supported security protocols (secp 0x00)"
    run nvme security-recv "$CONTROLLER" --secp=0x00 --spsp=0x0000         --size=512 --al=512 --namespace-id=0

    section "TCG Level 0 Discovery (secp 0x01)"
    run nvme security-recv "$CONTROLLER" --secp=0x01 --spsp=0x0001         --size=2048 --al=2048 --namespace-id=0

    # Protocol 0xEF is ATA device server password security - the ATA SECURITY
    # feature set reached through NVMe. These drives advertise it, and it is
    # how an OEM BIOS drive lock is implemented on an NVMe drive. A password
    # set by the machine these drives came out of would deny an erase here
    # while leaving every TCG locking flag clear, which is exactly the shape of
    # what we are looking at.
    section "ATA device server password security (secp 0xEF)"
    run nvme security-recv "$CONTROLLER" --secp=0xef --spsp=0x0000         --size=512 --al=512 --namespace-id=0
    run nvme security-recv "$CONTROLLER" --secp=0xef --spsp=0x0001         --size=512 --al=512 --namespace-id=0

    # And the same two again as raw bytes, for the parser and for the archive.
    section "security data, parsed"
    for SPEC in "0x00 0x0000 512 secp00" "0x01 0x0001 2048 l0-discovery"; do
        set -- $SPEC
        SECP="$1"; SPSP="$2"; LEN="$3"; TAG="$4"
        RAW="$(mktemp)"
        if nvme security-recv "$CONTROLLER" --secp="$SECP" --spsp="$SPSP"                 --size="$LEN" --al="$LEN" --namespace-id=0                 --raw-binary > "$RAW" 2>/dev/null; then
            python3 - "$RAW" "$TAG" <<'PARSE'
import sys
from pathlib import Path

BANNER = b"NVME Security Receive Command Success"

raw = Path(sys.argv[1]).read_bytes()
tag = sys.argv[2]

# nvme-cli prints a success line on stdout even with --raw-binary, so the
# payload starts after it. Stripped by matching the exact banner rather than
# by skipping to the first newline: a newline is a perfectly ordinary byte in
# binary data, and guessing would silently shift the whole buffer.
if raw.startswith(BANNER):
    cut = raw.find(bytes([10]), len(BANNER))
    raw = raw[cut + 1:] if cut >= 0 else raw[len(BANNER):]

print(f"  {tag}: {len(raw)} payload bytes")
if not any(raw):
    print("  ALL ZERO - the controller transferred nothing.")
    print("  If --al was accepted, this drive genuinely has no data for this")
    print("  protocol. If --al was rejected as an unknown option, the command")
    print("  never asked for any bytes and this result means nothing.")
    raise SystemExit

if tag == "secp00":
    KNOWN = {
        0x00: "Security protocol information",
        0x01: "TCG Storage - Level 0 Discovery",
        0x02: "TCG Storage", 0x03: "TCG Storage", 0x04: "TCG Storage",
        0x05: "TCG Storage", 0x06: "TCG Storage",
        0xEA: "NVMe RPMB", 0xEE: "IEEE 1667",
        0xEF: "ATA device server password security",
    }
    count = int.from_bytes(raw[6:8], "big")
    if count == 0 or 8 + count > len(raw):
        print(f"  list length {count} is not usable; header: {raw[:16].hex(' ')}")
        raise SystemExit
    codes = raw[8:8 + count]
    for code in codes:
        print(f"    0x{code:02x}  {KNOWN.get(code, 'vendor specific / unknown')}")
    if any(0x01 <= code <= 0x06 for code in codes):
        print("  >> The drive speaks TCG. Opal locking is on the table.")
    else:
        print("  >> No TCG protocol supported. Opal is not the cause.")
    raise SystemExit

NAMES = {
    0x0001: "TPer", 0x0002: "Locking", 0x0003: "Geometry Reporting",
    0x0100: "Enterprise SSC",
    0x0200: "Opal SSC v1.00", 0x0201: "Single User Mode",
    0x0202: "DataStore Table", 0x0203: "Opal SSC v2.00",
    0x0301: "Opalite SSC",
    0x0302: "Pyrite SSC v1.00", 0x0303: "Pyrite SSC v2.00",
    0x0304: "Ruby SSC",
    0x0402: "Block SID Authentication", 0x0403: "Namespace Locking",
    0x0404: "Data Removal Mechanism", 0x0405: "Namespace Geometry",
}

length = int.from_bytes(raw[0:4], "big")
print(f"  declared length: {length}")
offset, found, locking = 48, [], None
while offset + 4 <= min(length + 4, len(raw)):
    code = int.from_bytes(raw[offset:offset + 2], "big")
    size = raw[offset + 3]
    if code == 0:
        break
    found.append(NAMES.get(code, hex(code)))
    if code == 0x0002 and size:
        flags = raw[offset + 4]
        locking = {
            "LockingSupported": bool(flags & 0x01),
            "LockingEnabled": bool(flags & 0x02),
            "Locked": bool(flags & 0x04),
            "MediaEncryption": bool(flags & 0x08),
            "MBREnabled": bool(flags & 0x10),
            "MBRDone": bool(flags & 0x20),
        }
    offset += 4 + size

print("  TCG features:", ", ".join(found) if found else "none")
for key, value in (locking or {}).items():
    print(f"    {key:<18}{value}")

if locking and (locking["LockingEnabled"] or locking["Locked"]):
    print("  >> LOCKED SED. This is why the erase is denied.")
    print("     A PSID revert clears the lock and erases the media. The PSID is")
    print("     printed on the drive's own label and cannot be read in software,")
    print("     which is deliberate.")
elif locking:
    print("  >> TCG-capable, but locking is NOT enabled - look elsewhere.")
    if not locking["MediaEncryption"]:
        print("     MediaEncryption is false, so this drive has no encryption")
        print("     engine and no key. A crypto erase is therefore impossible")
        print("     on it by construction, which is what fna 0 and sanicap")
        print("     without bit 0 were already saying.")
PARSE
            cp "$RAW" "$REPORT_DIR/$(basename "$CONTROLLER")-$TAG.bin" 2>/dev/null
        else
            echo "  $TAG: Security Receive refused"
        fi
        rm -f "$RAW"
    done
done

echo
echo "==================== block devices ===================="
lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,RO,MOUNTPOINT,MODEL 2>&1 | sed 's/^/  /'

echo
echo "==================== kernel messages ===================="
# The controller's own complaints, which never reach nvme-cli's exit status.
dmesg 2>/dev/null | grep -iE "nvme|pcie|aer" | tail -60 | sed 's/^/  /' || true

if [ -n "$ATTEMPT" ]; then
    CONTROLLER="${TARGETS[0]}"
    echo
    echo "==================== attempting $ATTEMPT on $CONTROLLER ===================="
    if [ "$ATTEMPT" = "exit-failure" ]; then
        echo "Exit Failure Mode erases nothing. It clears a stuck sanitize state."
    else
        echo "This destroys the contents of $CONTROLLER."
    fi
    echo
    echo "nvme-cli prints the status itself on failure, in the form"
    echo "  NVMe status: ACCESS_DENIED: ... (0x286)"
    echo "so the code is captured whether or not this build has --verbose."
    echo

    case "$ATTEMPT" in
        exit-failure)
            # sanact=1 is Exit Failure Mode. If a previous sanitize failed, the
            # controller stays in that state and denies erase commands until it
            # is told to leave it. Nothing is erased; the media is untouched.
            run nvme sanitize "$CONTROLLER" --sanact=1 "${VERBOSE[@]}"
            ;;
        sanitize)
            # sanact=2 is Block Erase, the only action SANICAP 0x2 reports.
            run nvme sanitize "$CONTROLLER" --sanact=2 "${VERBOSE[@]}"
            ;;
        format)
            # ses=1 is User Data Erase. FNA 0 means crypto erase (ses=2) is
            # not implemented on these drives, so ses=1 is the only option.
            run nvme format "${CONTROLLER}n1" --ses=1 --force "${VERBOSE[@]}"
            ;;
    esac

    section "sanitize-log immediately after"
    run nvme sanitize-log "$CONTROLLER" -H -r

    section "kernel messages after the attempt"
    dmesg 2>/dev/null | grep -iE "nvme" | tail -20 | sed 's/^/  /' || true
fi

echo
echo "written to: $LOG"
sync
