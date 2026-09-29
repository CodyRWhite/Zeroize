#!/bin/bash
# Find an erase method this drive will actually accept.
#
# These Samsung PM991s on HP OEM firmware refuse both admin erase paths:
# Sanitize answers Access Denied for every action including the harmless one,
# and Format NVM answers Invalid Opcode while OACS bit 1 claims Format NVM is
# supported. Nothing is locked - Pyrite SSC v1.00 with LockingEnabled false,
# no namespace write protect, no read-only media, 100% spare.
#
# So rather than keep asking why, this asks what works. It walks a ladder of
# methods from strongest to weakest, stops at the first one the drive accepts,
# and verifies the result by reading the media back.
#
# Phase 1 is non-destructive and always runs. Phase 2 needs --destroy.
set -u

# Re-exec under bash if started with sh: dash does not have process
# substitution and stops on the first line of real work with a message that
# looks like a broken script rather than the wrong interpreter.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"
export PATH

if [ "$(id -u)" -ne 0 ]; then
    echo "This needs root. Try:  sudo bash $0 $*"
    exit 1
fi

DESTROY=0
SUSPEND=0
CONTROLLER=""

usage() {
    cat <<'USAGE'
usage: find-erase-method.sh /dev/nvme0 [--destroy] [--suspend]

Without --destroy it only probes, and changes nothing on the drive.

When the firmware erase paths are refused it first tries to unstick them with
a controller reset and a subsystem reset. --suspend adds an S3 suspend to that
list: a suspend and resume is a documented fix for Samsung NVMe controllers
answering Invalid Opcode to format, and it is worth far more than it costs
compared with overwriting a whole drive. It is opt-in because on a live USB
session a suspend can re-enumerate the boot medium and take the session with
it.

With --destroy it works down this ladder and stops at the first method the
drive accepts, verifying the media after each:

  1. nvme sanitize --sanact=2     Block Erase          NIST 800-88 Purge
  2. nvme format --ses=1          User Data Erase      NIST 800-88 Purge
  3. nvme format --ses=0          plain format         no erase, diagnostic
  4. blkdiscard                   Dataset Mgmt Deallocate
  5. blkdiscard -z                Write Zeroes
  6. dd sample overwrite          proves a full overwrite would work

Step 3 erases nothing by itself. It is there to tell two very different
situations apart: if a plain format is accepted then Format NVM exists and
only --ses is being refused, whereas Invalid Opcode again means the command is
absent from this firmware entirely.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --destroy) DESTROY=1; shift ;;
        --suspend) SUSPEND=1; shift ;;
        -h|--help) usage; exit 0 ;;
        /dev/*)    CONTROLLER="$1"; shift ;;
        *)         echo "unrecognised argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$CONTROLLER" ]; then
    usage >&2
    exit 2
fi

# Accept either the controller or the namespace and derive the other, because
# sanitize takes the controller and format takes the namespace, and getting
# that wrong produces an error that reads like the drive refusing.
case "$CONTROLLER" in
    *n[0-9]) NAMESPACE="$CONTROLLER"; CONTROLLER="${CONTROLLER%n[0-9]}" ;;
    *)       NAMESPACE="${CONTROLLER}n1" ;;
esac

if [ ! -e "$CONTROLLER" ] || [ ! -e "$NAMESPACE" ]; then
    echo "no such device: $CONTROLLER / $NAMESPACE" >&2
    exit 1
fi

# Refuse to touch the medium this session is running from. Getting this wrong
# destroys the running system mid-command, and the operator finds out when the
# screen stops responding.
ROOT_SOURCE="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
LIVE_SOURCE="$(findmnt -n -o SOURCE /run/live/medium 2>/dev/null || true)"
for GUARD in "$ROOT_SOURCE" "$LIVE_SOURCE"; do
    case "$GUARD" in
        "$NAMESPACE"*|"$CONTROLLER"*)
            echo "$NAMESPACE carries the running system. Refusing." >&2
            exit 1
            ;;
    esac
done

# Pin the drive to something that survives a reset.
#
# Device node numbers do not. A controller reset, a subsystem reset and a PCI
# rescan all re-enumerate, and the kernel hands out the next free instance - so
# the drive that was /dev/nvme0 comes back as /dev/nvme2, and every command
# after that either fails as "not found" or, far worse, succeeds against a
# DIFFERENT drive that inherited the old number. The serial is the identity;
# the node is just where it happens to be today.
SERIAL="$(nvme id-ctrl "$CONTROLLER" 2>/dev/null | sed -n 's/^sn *: *//p' | tr -d ' ')"
PCI_ADDRESS="$(basename "$(readlink -f "/sys/class/nvme/$(basename "$CONTROLLER")/device" 2>/dev/null)" 2>/dev/null)"
case "$PCI_ADDRESS" in
    [0-9a-f]*:[0-9a-f]*:*) ;;
    *) PCI_ADDRESS="" ;;
esac

# Find the node currently carrying our serial, and follow it if it has moved.
resolve_by_serial() {
    local candidate sn
    [ -n "$SERIAL" ] || return 1
    for candidate in /dev/nvme[0-9]; do
        [ -e "$candidate" ] || continue
        sn="$(nvme id-ctrl "$candidate" 2>/dev/null | sed -n 's/^sn *: *//p' | tr -d ' ')"
        if [ "$sn" = "$SERIAL" ]; then
            if [ "$candidate" != "$CONTROLLER" ]; then
                echo "  device moved: $CONTROLLER -> $candidate (serial $SERIAL)"
                CONTROLLER="$candidate"
                NAMESPACE="${candidate}n1"
            fi
            return 0
        fi
    done
    return 1
}

# Tear the PCI function down and bring it back.
#
# A reset can drop the function without the kernel re-probing it, and then the
# drive is simply gone - which happened on the first successful run and had to
# be undone by hand. Doing it here means the script recovers instead of
# reporting a missing device in the middle of a working erase.
recover_device() {
    if [ -z "$PCI_ADDRESS" ]; then
        echo "  no PCI address recorded; cannot rescan"
        return 1
    fi
    echo "  removing and rescanning PCI $PCI_ADDRESS"
    echo 1 > "/sys/bus/pci/devices/$PCI_ADDRESS/remove" 2>/dev/null || true
    sleep 2
    echo 1 > /sys/bus/pci/rescan 2>/dev/null || true
    sleep 3
    udevadm settle >/dev/null 2>&1 || true
    resolve_by_serial
}

resolve_report_root() {
    local label device mount_point
    for label in DIAGS ZEROIZE-OUT; do
        device="$(blkid -L "$label" 2>/dev/null || true)"
        [ -n "$device" ] || continue
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
LOG="$REPORT_DIR/erase-method-$(date +%Y-%m-%d-%H-%M-%S).txt"
exec > >(tee -a "$LOG") 2>&1

section() { echo; echo "--- $* ---"; }

# Show the command, run it, show its status. An option this nvme-cli build does
# not know is a usage error, not a drive answer, and in a log the two look
# identical unless the command line is printed beside the result.
STATUS=0
run() {
    echo "\$ $*"
    "$@" 2>&1
    STATUS=$?
    echo "  [exit $STATUS]"
    return $STATUS
}

# Read 4 KiB at a given 4 KiB offset and describe it in one word.
#
# Reported rather than judged: "zeros" is the expected result of a successful
# block erase on most controllers, but a drive that returns random data has
# also erased successfully if it holds an encryption key, and one that returns
# the ORIGINAL data has not erased at all. Only the last of those is a failure,
# and the caller needs to be able to tell them apart.
sample() {
    # Bounded, because a drive in an unknown state can block a read forever
    # and a hang inside a survey is indistinguishable from a slow drive. 512
    # bytes is plenty to characterise a block and keeps the hexdump small.
    timeout 15 dd if="$NAMESPACE" bs=512 count=1 skip="$(( $1 * 8 ))" \
        iflag=direct 2>/dev/null | od -A n -t x1 -v | tr -d ' \n'
}

describe_sample() {
    local hex="$1"
    if [ -z "$hex" ]; then
        echo "unreadable"
    elif [ -z "${hex//0/}" ]; then
        echo "all zeros"
    elif [ -z "${hex//f/}" ]; then
        echo "all ones"
    else
        echo "data present (${hex:0:32}...)"
    fi
}

DEVICE_SECTORS="$(timeout 10 blockdev --getsz "$NAMESPACE" 2>/dev/null || echo 8)"
case "$DEVICE_SECTORS" in ""|*[!0-9]*) DEVICE_SECTORS=8 ;; esac
LAST_LBA=$(( DEVICE_SECTORS / 8 - 1 ))
MID_LBA=$(( LAST_LBA / 2 ))

survey() {
    local where result
    for where in 0 "$MID_LBA" "$LAST_LBA"; do
        # Announced before the read, not after. A survey that prints nothing
        # while it blocks looks like a script that has stopped, and gives the
        # operator no way to tell which of the three reads is the one hanging.
        printf '  4K block %-12s ... ' "$where"
        result="$(describe_sample "$(sample "$where")")"
        printf '%s\n' "$result"
    done
}

# Is Sanitize reachable right now? Judged on the message rather than the exit
# status: on a drive that has never sanitized, Exit Failure Mode may legitimately
# return Invalid Field, which is a refusal of the ARGUMENT, not of the command.
# Only Access Denied means the firmware is refusing Sanitize itself.
probe_sanitize() {
    local out
    out="$(nvme sanitize "$CONTROLLER" --sanact=1 2>&1)"
    case "$out" in
        *"Access Denied"*|*ACCESS_DENIED*) return 1 ;;
        *) return 0 ;;
    esac
}

echo "Zeroize erase-method ladder"
echo "date       : $(date -Is)"
echo "controller : $CONTROLLER"
echo "namespace  : $NAMESPACE"
echo "model      : $(nvme id-ctrl "$CONTROLLER" 2>/dev/null | sed -n 's/^mn *: *//p')"
echo "firmware   : $(nvme id-ctrl "$CONTROLLER" 2>/dev/null | sed -n 's/^fr *: *//p')"
echo "mode       : $( [ "$DESTROY" -eq 1 ] && echo 'DESTRUCTIVE ladder' || echo 'probe only' )"
echo "report     : $LOG"

# ---------------------------------------------------------------------------
# Phase 1 - non-destructive
# ---------------------------------------------------------------------------
echo
echo "==================== phase 1: probes (nothing is changed) ===================="

section "media before"
survey

# Exit Failure Mode erases nothing: it asks the controller to leave a failed
# sanitize state, and on a drive that never sanitized it is a no-op. That makes
# it the one safe way to ask "is the Sanitize command reachable at all", which
# is the question the capability registers answer wrongly on these drives.
section "is Sanitize reachable? (sanact=1, Exit Failure Mode, erases nothing)"
# Classified on what the controller SAID, not on whether the command exited
# non-zero. An earlier version reported "not reachable - Access Denied means
# the firmware refuses the command" on a run where the drive had actually
# answered Invalid Field, which means very nearly the opposite. Asserting a
# cause the script has not checked is worse than reporting the raw status.
SANITIZE_PROBE="$(nvme sanitize "$CONTROLLER" --sanact=1 2>&1)"
echo "$ nvme sanitize $CONTROLLER --sanact=1"
echo "$SANITIZE_PROBE" | sed 's/^/  /'
case "$SANITIZE_PROBE" in
    *"Access Denied"*|*ACCESS_DENIED*)
        SANITIZE_REACHABLE=0
        echo "  >> Sanitize is NOT reachable: the firmware is refusing the"
        echo "     command itself, whatever SANICAP advertises."
        ;;
    *)
        SANITIZE_REACHABLE=1
        echo "  >> Sanitize IS reachable."
        echo "     Anything but Access Denied means the controller knows the"
        echo "     command. Invalid Field refuses the ARGUMENT: Exit Failure"
        echo "     Mode on a drive with no failed sanitize to leave has nothing"
        echo "     to do, and says so."
        ;;
esac

section "firmware slots"
# Three slots on these drives with activation-without-reset supported, so a
# second populated slot can be activated with fw-commit alone - no image to
# obtain, and reversible by committing the original back.
run nvme fw-log "$CONTROLLER"

section "TCG state"
# --al is the Allocation Length. Without it the controller is told to transfer
# zero bytes and returns nothing, and a parser reads that silence as "no TCG".
run nvme security-recv "$CONTROLLER" --secp=0x01 --spsp=0x0001 \
    --size=2048 --al=2048 --namespace-id=0

# Does the media accept writes at all? Read a block and write the same bytes
# back: the content is unchanged either way, so this is safe on a drive nobody
# has authorised us to erase yet, and it settles whether a software overwrite
# is even an option before the ladder gets that far.
section "can the media be written? (reads a block and writes it back unchanged)"
PROBE="$(mktemp)"
if dd if="$NAMESPACE" of="$PROBE" bs=4096 count=1 skip="$MID_LBA" iflag=direct 2>/dev/null &&
   dd if="$PROBE" of="$NAMESPACE" bs=4096 count=1 seek="$MID_LBA" oflag=direct conv=fsync 2>/dev/null; then
    echo "  writes are accepted - a software overwrite is available"
    WRITABLE=1
else
    echo "  writes REFUSED - no erase method can work on this drive"
    WRITABLE=0
fi
rm -f "$PROBE"

# What is a deallocated block guaranteed to read back as?
#
# DLFEAT bits 2:0. A value of zero means "not reported" - the controller makes
# no promise at all. blkdiscard on such a drive may read back as zeros and
# still be holding the data, because nothing obliges it to keep returning them
# after a power cycle or a garbage collection pass. Verifying by reading is
# then measuring a courtesy, not a guarantee, and a method that passes its own
# verification while leaving the data recoverable is worse than one that
# plainly fails.
section "deallocate guarantee (DLFEAT)"
DLFEAT="$(nvme id-ns "$NAMESPACE" 2>/dev/null | sed -n 's/^dlfeat *: *//p' | tr -d ' ')"
DLFEAT="${DLFEAT:-0}"
case $(( DLFEAT & 7 )) in
    1) DEALLOC_GUARANTEED=1; echo "  dlfeat $DLFEAT - deallocated blocks read back as 0x00" ;;
    2) DEALLOC_GUARANTEED=1; echo "  dlfeat $DLFEAT - deallocated blocks read back as 0xFF" ;;
    *) DEALLOC_GUARANTEED=0
       echo "  dlfeat $DLFEAT - NOT REPORTED."
       echo "  A deallocate on this drive carries no guarantee about what the"
       echo "  blocks read back as, so a discard alone cannot be treated as an"
       echo "  erase however clean the read-back looks."
       ;;
esac

# ---------------------------------------------------------------------------
# Phase 1b - try to unstick the firmware erase paths
# ---------------------------------------------------------------------------
# Worth doing before anything destructive, because if one of these works the
# drive can be PURGED rather than merely cleared, and the whole job takes
# seconds instead of the time it takes to write 256 GB.
#
# The suspend is not a guess. A suspend and resume is a documented fix for
# Samsung NVMe controllers that answer Invalid Opcode to format, and these are
# Samsung controllers answering exactly that.

FIRMWARE_UNSTUCK=0

# Wait for the controller and namespace nodes to come back.
#
# A reset takes the device away and brings it back, and everything after it
# fails confusingly while it is gone - the errors read as the drive refusing
# rather than the drive being absent.
wait_for_device() {
    local waited=0
    while [ "$waited" -lt 30 ]; do
        if resolve_by_serial && [ -e "$CONTROLLER" ] && [ -e "$NAMESPACE" ]; then
            return 0
        fi
        sleep 1
        waited=$(( waited + 1 ))
    done
    echo "  drive $SERIAL has not come back after 30s; rescanning the bus"
    if recover_device && [ -e "$NAMESPACE" ]; then
        echo "  recovered at $CONTROLLER"
        return 0
    fi
    echo "  drive $SERIAL is still missing"
    return 1
}

unlock_attempt() {
    local label="$1"
    shift
    [ "$FIRMWARE_UNSTUCK" -eq 1 ] && return 0

    section "unlock attempt: $label"
    # Every one of these can block forever.
    #
    # subsystem-reset in particular takes the device node away while nvme-cli
    # is still holding it, and the command simply never returns - which on a
    # terminal looks identical to a long-running erase, so the operator waits.
    # A bounded wait turns a hang into a result.
    run timeout 30 "$@" || true
    if [ "$STATUS" -eq 124 ]; then
        echo "  timed out after 30s - treating as no answer and moving on"
    fi

    wait_for_device || return 0
    sleep 2
    nvme ns-rescan "$CONTROLLER" >/dev/null 2>&1 || true

    if probe_sanitize; then
        echo "  >> Sanitize is now REACHABLE. $label unstuck it."
        FIRMWARE_UNSTUCK=1
    else
        echo "  >> still Access Denied"
    fi
}

if [ "$SANITIZE_REACHABLE" -eq 0 ]; then
    echo
    echo "==================== phase 1b: unstick the firmware paths ===================="

    unlock_attempt "controller reset" nvme reset "$CONTROLLER"
    unlock_attempt "subsystem reset" nvme subsystem-reset "$CONTROLLER"

    if [ "$FIRMWARE_UNSTUCK" -eq 0 ] && [ "$SUSPEND" -eq 1 ]; then
        # Flush the log to the stick FIRST. If the suspend takes the live
        # session with it - which it has done before on this image, when the
        # USB re-enumerated and the squashfs went with it - the record of
        # everything up to this point still survives on removable media.
        echo
        echo "  flushing the log before suspending, in case the session does not"
        echo "  come back"
        sync

        unlock_attempt "S3 suspend for 3 seconds" rtcwake -m mem -s 3
    elif [ "$FIRMWARE_UNSTUCK" -eq 0 ]; then
        echo
        echo "  Not trying an S3 suspend: pass --suspend to include it. It is the"
        echo "  documented fix for Samsung controllers refusing format, and it is"
        echo "  the most likely of these to work."
    fi

    if [ "$FIRMWARE_UNSTUCK" -eq 1 ]; then
        SANITIZE_REACHABLE=1
        echo
        echo "  >> A firmware erase may now be possible. That would be a PURGE"
        echo "     rather than a Clear, so it is worth re-running with --destroy."
    fi
fi

if [ "$DESTROY" -ne 1 ]; then
    echo
    echo "==================== summary ===================="
    echo "  Sanitize reachable : $( [ "$SANITIZE_REACHABLE" -eq 1 ] && echo yes || echo NO )"
    echo "  Media writable     : $( [ "$WRITABLE" -eq 1 ] && echo yes || echo NO )"
    echo "  Discard guaranteed : $( [ "$DEALLOC_GUARANTEED" -eq 1 ] && echo yes || echo NO )"
    echo
    echo "Re-run with --destroy to work down the ladder. That ERASES the drive."
    echo
    echo "written to: $LOG"
    sync
    exit 0
fi

# ---------------------------------------------------------------------------
# Phase 2 - the ladder
# ---------------------------------------------------------------------------
echo
echo "==================== phase 2: ladder (THIS ERASES $NAMESPACE) ===================="

WINNER=""
GRADE=""

attempt() {
    local label grade
    label="$1"; grade="$2"; shift 2
    [ -n "$WINNER" ] && return 0

    section "$label"
    if run "$@"; then
        echo "  >> ACCEPTED"
        WINNER="$label"
        GRADE="$grade"
    else
        echo "  >> refused, moving down the ladder"
    fi
    return 0
}

attempt "NVMe Sanitize, Block Erase" "NIST 800-88 Purge" \
    nvme sanitize "$CONTROLLER" --sanact=2

# Sanitize runs in the background, so acceptance is not completion. Wait for
# SSTAT to settle before deciding it worked.
# Read one field out of the sanitize log. -r retains the asynchronous event:
# without it, reading the page tells the controller to clear the event, which
# is the wrong thing to do when the point is to observe state.
sanitize_field() {
    # awk for the first field, not tr for the spaces. nvme-cli prints SPROG as
    #     Sanitize Progress (SPROG) :  11763	(17.948914%)
    # so stripping spaces leaves "11763(17.948914%)", which any numeric guard
    # then rejects - reporting a running sanitize as an unreadable log. The
    # value is the first whitespace-delimited token and nothing else.
    nvme sanitize-log "$CONTROLLER" -H -r 2>/dev/null |
        sed -n "s/.*($1).*: *//p" | awk 'NR == 1 { print $1 }'
}

# Wait for the sanitize to finish, and say plainly which of the four states it
# ended in.
#
# The previous version looked for the substring " 1" anywhere in the SSTAT line
# and treated an unreadable log as "keep waiting". Both were wrong in the same
# direction: after a PCI rescan the node had moved, every read returned
# nothing, no match ever fired, and it sat for its full hour while the drive
# had in fact finished in seconds. Silence is not progress.
wait_for_sanitize() {
    local deadline misses sstat sprog sstat_value sprog_value
    deadline=$(( $(date +%s) + 7200 ))
    misses=0

    while [ "$(date +%s)" -lt "$deadline" ]; do
        resolve_by_serial >/dev/null 2>&1 || true
        sstat="$(sanitize_field SSTAT)"

        case "$sstat" in
            ""|*[!0-9a-fA-FxX]*)
                misses=$(( misses + 1 ))
                echo "  sanitize log unreadable ($misses)"
                if [ "$misses" -ge 6 ]; then
                    echo "  >> six consecutive failed reads - recovering the device"
                    recover_device || true
                    misses=0
                fi
                sleep 10
                continue
                ;;
        esac

        misses=0

        # Converted once, defensively: "0x1" and "0" are both valid here, but
        # an unexpected third format would abort the whole script inside an
        # arithmetic expansion, in the middle of a running erase.
        if ! sstat_value=$(( sstat )) 2>/dev/null; then
            echo "  SSTAT $sstat - cannot be read as a number"
            sleep 10
            continue
        fi

        sprog="$(sanitize_field SPROG)"
        case "$sprog" in ""|*[!0-9a-fA-FxX]*) sprog=0 ;; esac

        case $(( sstat_value & 7 )) in
            0)
                # Never sanitized. Seen briefly right after the command is
                # accepted, before the controller updates the log.
                echo "  SSTAT $sstat - not started yet"
                ;;
            1)
                echo "  SSTAT $sstat - completed successfully"
                nvme sanitize-log "$CONTROLLER" -H -r 2>&1 | sed 's/^/    /'
                return 0
                ;;
            2)
                sprog_value=$(( sprog )) 2>/dev/null || sprog_value=0
                echo "  SSTAT $sstat - in progress, $(( sprog_value * 100 / 65535 ))% (SPROG $sprog)"
                ;;
            3)
                echo "  SSTAT $sstat - FAILED"
                nvme sanitize-log "$CONTROLLER" -H -r 2>&1 | sed 's/^/    /'
                return 1
                ;;
            *)
                echo "  SSTAT $sstat - unrecognised state"
                ;;
        esac
        sleep 10
    done

    echo "  gave up waiting after two hours"
    return 1
}

if [ "$WINNER" = "NVMe Sanitize, Block Erase" ]; then
    section "waiting for sanitize to finish"
    if wait_for_sanitize; then
        echo "  >> sanitize confirmed complete"
    else
        # Accepted but not confirmed. Retracting the win matters: the ladder
        # would otherwise report a purge that the controller never finished,
        # and the certificate would say so.
        echo "  >> sanitize did NOT confirm - withdrawing it as the result"
        WINNER=""
        GRADE=""
    fi
fi

attempt "NVMe Format, User Data Erase (ses=1)" "NIST 800-88 Purge" \
    nvme format "$NAMESPACE" --ses=1 --force

attempt "NVMe Format, no secure erase (ses=0)" "diagnostic only, NOT an erase" \
    nvme format "$NAMESPACE" --ses=0 --force

if [ "$DEALLOC_GUARANTEED" -eq 1 ]; then
    attempt "Dataset Management Deallocate (blkdiscard)" "NIST 800-88 Clear" \
        blkdiscard -f "$NAMESPACE"
else
    section "Dataset Management Deallocate - run, but NOT accepted as the answer"
    echo "  Running it because it is fast and makes the overwrite below"
    echo "  cheaper, but not treating it as the result: dlfeat $DLFEAT means"
    echo "  the drive promises nothing about what deallocated blocks return."
    run blkdiscard -f "$NAMESPACE" || true
fi

attempt "Write Zeroes (blkdiscard -z)" "NIST 800-88 Clear" \
    blkdiscard -f -z "$NAMESPACE"

# Last resort, and only a sample: 1 GiB at each end. A full overwrite of a
# 256 GB drive takes long enough that proving the method works is the useful
# thing to do here, not performing it.
if [ -z "$WINNER" ]; then
    section "sample overwrite with dd (1 GiB at each end)"
    if dd if=/dev/zero of="$NAMESPACE" bs=1M count=1024 oflag=direct conv=fsync 2>&1 &&
       dd if=/dev/zero of="$NAMESPACE" bs=1M count=1024 \
          seek=$(( $(blockdev --getsize64 "$NAMESPACE") / 1048576 - 1024 )) \
          oflag=direct conv=fsync 2>&1; then
        echo "  >> ACCEPTED - a full software overwrite will work"
        WINNER="software overwrite (dd)"
        GRADE="NIST 800-88 Clear"
    else
        echo "  >> refused - this drive cannot be erased by any method tried"
    fi
fi

# ---------------------------------------------------------------------------
section "media after"
# The caches lie after an erase: the kernel keeps the old partition table, udev
# keeps the old properties, and blkid keeps its own copy. Clear all three
# before reading anything back, or the verification describes the drive as it
# was rather than as it is.
partx -d "$NAMESPACE" >/dev/null 2>&1 || true
blockdev --rereadpt "$NAMESPACE" >/dev/null 2>&1 || true
udevadm trigger --action=change "$NAMESPACE" >/dev/null 2>&1 || true
udevadm settle >/dev/null 2>&1 || true
blkid -g >/dev/null 2>&1 || true
survey

echo
echo "  lsblk view:"
lsblk -o NAME,SIZE,FSTYPE,LABEL "$NAMESPACE" 2>&1 | sed 's/^/    /'

echo
echo "==================== result ===================="
if [ -n "$WINNER" ]; then
    echo "  working method : $WINNER"
    echo "  classification : $GRADE"
    echo
    echo "  Put this method into Zeroize for this drive model."
else
    echo "  NO METHOD WORKED."
    echo
    echo "  Every path was refused, including plain writes. That is a drive"
    echo "  which cannot be sanitised in software at all, and physical"
    echo "  destruction is the only remaining disposal route."
fi

echo
echo "written to: $LOG"
sync
