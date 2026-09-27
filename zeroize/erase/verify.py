"""Post-erase verification for firmware-driven methods.

A software overwrite verifies itself - it knows exactly what it wrote, so it
can compare digests (see :mod:`zeroize.erase.overwrite`). A firmware erase
cannot be checked that way, because the specification does not say what a drive
must return afterwards. Most return zeros; some return a fixed pattern; some
return whatever the flash translation layer now maps there.

So the check performed here is the one that is actually meaningful: read a
sample of the device and confirm that **nothing recognisable survived**. That
means no partition table, no filesystem superblock, no LVM or LUKS header - the
structures that would let data be recovered, and the structures whose survival
proves an erase did not happen. The proportion of sampled bytes that came back
zero is reported alongside, because it is informative even though it is not the
pass condition.

This is an evidence-gathering step, and it is reported honestly on the
certificate: "no recoverable structures found in N% of the device, sampled" is
a true and useful statement. "100% verified" would not be.
"""

from __future__ import annotations

import os
import time

from ..logging_setup import get_logger
from ..models import JobState, format_size
from .context import JobContext

_log = get_logger("Verify")

#: Size of each sampled window, in bytes. Large enough to contain any of the
#: signatures below at their defined offsets.
_WINDOW_SIZE = 1024 * 1024

#: Cap on sampled windows, so verifying a 20 TB drive stays bounded.
_MAX_WINDOWS = 4096

#: Signatures that must not survive an erase, as ``(offset, magic, name)``.
#: ``offset`` is relative to the start of the window being examined; a value of
#: ``None`` means "anywhere in the window".
#: Structures whose presence means data survived the erase.
#:
#: Each entry is ``(offset, magic, name)``. An *offset* anchors the magic to a
#: fixed position; ``None`` searches the whole window.
#:
#: UNANCHORED MAGICS MUST BE LONG. A 4-byte magic searched across a 10% sample
#: of a 500 GB drive is examined at roughly 5e10 positions, so on random data it
#: appears about a dozen times by pure chance - and random data is exactly what
#: a correct erase produces. An Enhanced Secure Erase on a self-encrypting
#: drive is a cryptographic erase: it discards the key, so every subsequent
#: read returns old ciphertext decrypted with a new one. That is NIST SP
#: 800-88 Purge working, and short magics would have reported it as a failed
#: verification with "XFS superblock still present".
#:
#: So anything shorter than eight bytes is anchored to the offset where the
#: structure actually lives, and the zstd frame magic - four bytes, and not a
#: disk structure in any case - is gone.
_MINIMUM_UNANCHORED_MAGIC = 8

_SIGNATURES: tuple[tuple[int | None, bytes, str], ...] = (
    (0x1FE, b"\x55\xaa", "MBR boot signature"),
    (None, b"EFI PART", "GPT header"),
    (0x438, b"\x53\xef", "ext2/3/4 superblock"),
    (0x3, b"NTFS    ", "NTFS boot sector"),
    (0x3, b"MSDOS", "FAT boot sector"),
    (0x52, b"FAT32   ", "FAT32 boot sector"),
    # XFS puts its superblock magic at the very start of the filesystem. It was
    # searched unanchored, and at four bytes that is a coin toss on any drive
    # holding high-entropy data.
    (0x0, b"XFSB", "XFS superblock"),
    (None, b"_BHRfS_M", "Btrfs superblock"),
    # LUKS1 and LUKS2 both put this at offset 0 of the container.
    (0x0, b"LUKS\xba\xbe", "LUKS header"),
    (None, b"LABELONE", "LVM2 label"),
    (0x10034, b"ReIsEr", "ReiserFS superblock"),
    (None, b"SWAPSPACE2", "Linux swap header"),
    (None, b"\x89HDF\r\n\x1a\n", "HDF5 container"),
)

# Enforced rather than trusted to review: the cost of a short unanchored magic
# is a correctly erased drive certified as a failure.
for _offset, _magic, _name in _SIGNATURES:
    if _offset is None and len(_magic) < _MINIMUM_UNANCHORED_MAGIC:
        raise AssertionError(
            f"unanchored signature {_name!r} is only {len(_magic)} bytes; "
            f"anchor it or lengthen it - see _MINIMUM_UNANCHORED_MAGIC"
        )


def _sample_offsets(size_bytes: int, percent: float) -> list[int]:
    """Byte offsets to sample, always including the head and the tail.

    The first and last megabyte matter disproportionately: they hold the
    primary and backup partition tables, which is where a partial or refused
    erase shows up first.
    """
    if size_bytes <= 0 or percent <= 0:
        return []

    head = 0
    tail = max(0, size_bytes - _WINDOW_SIZE)
    wanted = max(2, min(_MAX_WINDOWS, int((size_bytes * percent / 100) / _WINDOW_SIZE)))

    offsets = {head, tail}
    if wanted > len(offsets):
        stride = max(_WINDOW_SIZE, size_bytes // (wanted - len(offsets) + 1))
        position = stride
        while position < tail and len(offsets) < wanted:
            offsets.add(position - position % _WINDOW_SIZE)
            position += stride

    return sorted(offsets)


def _find_signature(window: bytes) -> str:
    """Return the name of the first recoverable structure found, or empty."""
    for offset, magic, name in _SIGNATURES:
        if offset is None:
            if magic in window:
                return name
        elif len(window) >= offset + len(magic) and window[offset : offset + len(magic)] == magic:
            return name
    return ""


def verify_firmware_erase(context: JobContext) -> tuple[bool | None, str, float]:
    """Sample the device and confirm no recoverable structures remain.

    Returns ``(verdict, detail, percent_of_device_sampled)`` where *verdict* is:

    ``True``
        Sampled, and nothing recoverable was found.
    ``False``
        Sampled, and recoverable structures survived. The erase has failed.
    ``None``
        **Could not be checked.** Verification was not requested, the device is
        too small to sample, or it could not be reopened.

    The third case is deliberately not ``True``. It used to be, and that is the
    same mistake that made this tool certify a drive as unerased for a whole
    evening: a check that cannot run is not a check that passed. A device that
    cannot be reopened is still not treated as a failed erase - some
    controllers keep the namespace offline briefly after a format, and that is
    no evidence the erase went wrong - but it must not be reported as verified
    either. ``None`` says "not performed", which is what the certificate
    prints.
    """
    device = context.device
    percent_requested = context.settings.erase.verification_percent

    if percent_requested <= 0:
        return None, "verification not requested", 0.0

    if device.simulated:
        return True, "simulated - no device read", percent_requested

    offsets = _sample_offsets(device.size_bytes, percent_requested)
    if not offsets:
        return None, "device too small to sample", 0.0

    try:
        descriptor = os.open(device.path, os.O_RDONLY)
    except OSError as error:
        _log.warning("Could not reopen %s to verify: %s", device.path, error)
        return None, f"not verified - the device could not be reopened ({error})", 0.0

    started = time.monotonic()
    sampled_bytes = 0
    zero_bytes = 0
    findings: list[str] = []

    try:
        for position, offset in enumerate(offsets, start=1):
            if context.cancelled:
                break

            length = min(_WINDOW_SIZE, device.size_bytes - offset)
            if length <= 0:
                continue

            try:
                os.lseek(descriptor, offset, os.SEEK_SET)
                window = os.read(descriptor, length)
            except OSError as error:
                _log.warning("Read failed at byte %d on %s: %s", offset, device.path, error)
                findings.append(f"read error at byte {offset}: {error}")
                continue

            sampled_bytes += len(window)
            zero_bytes += len(window) - len(window.replace(b"\x00", b""))

            found = _find_signature(window)
            if found:
                findings.append(f"{found} still present at byte {offset}")
                _log.error("%s: %s survived at offset %d", device.path, found, offset)

            if position % 32 == 0 or position == len(offsets):
                context.report(
                    f"Verifying - {position} of {len(offsets)} sampled regions",
                    state=JobState.VERIFYING,
                    fraction=position / len(offsets),
                    bytes_done=sampled_bytes,
                    bytes_total=device.size_bytes,
                    elapsed_seconds=time.monotonic() - started,
                )
    finally:
        os.close(descriptor)

    percent_sampled = (sampled_bytes / device.size_bytes * 100) if device.size_bytes else 0.0
    zero_fraction = (zero_bytes / sampled_bytes * 100) if sampled_bytes else 0.0

    if findings:
        detail = "; ".join(findings[:3])
        if len(findings) > 3:
            detail += f"; and {len(findings) - 3} more"
        return False, detail, percent_sampled

    # A low zero fraction is not a bad sign on its own. A cryptographic erase
    # leaves the media full of old ciphertext that no longer decrypts, which
    # reads as random - so "0.0% of sampled bytes were zero" is the expected
    # result of a correct Purge on a self-encrypting drive, and saying so keeps
    # it from reading like a failure on the certificate.
    if zero_fraction >= 99.0:
        pattern = f"{zero_fraction:.1f}% of sampled bytes were zero"
    elif zero_fraction < 10.0:
        pattern = (
            f"{zero_fraction:.1f}% of sampled bytes were zero, consistent with a "
            f"cryptographic erase leaving indecipherable data rather than zeros"
        )
    else:
        pattern = f"{zero_fraction:.1f}% of sampled bytes were zero"

    detail = (
        f"no recoverable structures found in {format_size(sampled_bytes)} sampled "
        f"({percent_sampled:.1f}% of the device); {pattern}"
    )
    _log.info("Verification passed on %s: %s", device.path, detail)
    return True, detail, percent_sampled
