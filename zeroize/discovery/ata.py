"""ATA / SATA / SAS security-state discovery via ``hdparm`` and ``sg3_utils``.

Non-NVMe drives have their own in-firmware erase commands, and the same
principle applies as for NVMe: find out what the drive will actually accept
before offering it to the operator.

For ATA and SATA that means the ATA security feature set, read from
``hdparm -I``. Its output is a small block of tab-indented flags where a
leading ``not`` negates the flag on that line::

    Security:
            Master password revision code = 65534
                    supported
            not     enabled
            not     locked
                    frozen
            not     expired: security count
                    supported: enhanced erase
            114min for SECURITY ERASE UNIT. 114min for ENHANCED SECURITY ERASE UNIT.

``frozen`` is the field that decides whether ATA Secure Erase is usable at all.
Most system BIOSes issue SECURITY FREEZE LOCK during POST specifically to stop
malware wiping a disk, and a frozen drive rejects the erase command until it
has been power-cycled - typically by hot-unplugging it, or by suspending the
machine to RAM and resuming. Detecting that here lets the UI tell the operator
what to do instead of surfacing a bare command failure.

SAS and other true SCSI drives do not implement ATA security; they use the
SCSI SANITIZE command, probed with ``sg_sanitize``.
"""

from __future__ import annotations

import re

from ..logging_setup import get_logger
from ..models import AtaSecurity, Device, DeviceKind
from ..process import run, tool_available

_log = get_logger("Ata")

#: Kinds that may implement the ATA security feature set. USB bridges usually
#: do not pass the commands through, but some do, so they are probed and the
#: answer is believed.
_ATA_LIKE = frozenset({DeviceKind.ATA, DeviceKind.USB, DeviceKind.SCSI, DeviceKind.UNKNOWN})

_SECURITY_HEADING = re.compile(r"^\s*Security:\s*$")
_ERASE_TIME = re.compile(
    r"(?P<normal>\d+)\s*min\s+for\s+SECURITY\s+ERASE\s+UNIT"
    r"(?:.*?(?P<enhanced>\d+)\s*min\s+for\s+ENHANCED\s+SECURITY\s+ERASE\s+UNIT)?",
    re.IGNORECASE | re.DOTALL,
)


def _parse_security_block(output: str) -> AtaSecurity:
    """Parse the ``Security:`` section of ``hdparm -I`` output.

    Only lines inside the block are considered, because words like ``enabled``
    appear elsewhere in hdparm's output and would otherwise produce false
    positives.
    """
    security = AtaSecurity()
    lines = output.splitlines()

    try:
        start = next(index for index, line in enumerate(lines) if _SECURITY_HEADING.match(line))
    except StopIteration:
        return security

    block: list[str] = []
    for line in lines[start + 1 :]:
        # The block ends at the first line that is not indented, which is the
        # next section heading (e.g. "Logical Unit WWN Device Identifier:").
        if line.strip() and not line.startswith((" ", "\t")):
            break
        block.append(line)

    block_text = "\n".join(block)

    for line in block:
        stripped = line.strip()
        if not stripped:
            continue
        negated = bool(re.match(r"^not\b", stripped, re.IGNORECASE))
        flag = re.sub(r"^not\b", "", stripped, flags=re.IGNORECASE).strip().lower()

        if flag.startswith("supported: enhanced erase"):
            security.enhanced_erase_supported = not negated
        elif flag == "supported":
            security.supported = not negated
        elif flag == "enabled":
            security.enabled = not negated
        elif flag == "locked":
            security.locked = not negated
        elif flag == "frozen":
            security.frozen = not negated
        elif flag.startswith("expired"):
            security.expired = not negated

    timing = _ERASE_TIME.search(block_text)
    if timing:
        security.estimated_minutes = int(timing.group("normal"))
        enhanced = timing.group("enhanced")
        security.enhanced_estimated_minutes = int(enhanced) if enhanced else security.estimated_minutes

    return security


def _parse_capabilities(output: str, security: AtaSecurity) -> None:
    """Read the Commands/features block for things the security block omits.

    Three of these decide which methods are worth offering:

    * ``SANITIZE feature set`` - a purge path the security freeze cannot block.
    * ``Deterministic read ZEROs after TRIM`` - without it a discard proves
      nothing, because the drive is free to return the old contents.
    * ``Device encrypts all user data`` - a self-encrypting drive, which
      changes what residual data in unreachable cells actually means.

    hdparm's capitalisation varies between versions ("ZEROs" and "ZEROS" both
    occur in the wild), so matching is case-insensitive throughout.
    """
    lowered = output.lower()

    if "sanitize feature set" in lowered and "not supported" not in lowered:
        security.ata_sanitize_block_supported = "block_erase_ext" in lowered
        security.ata_sanitize_crypto_supported = "crypto_scramble_ext" in lowered

    security.trim_supported = "data set management trim supported" in lowered
    security.deterministic_zeros_after_trim = "deterministic read zeros after trim" in lowered
    security.self_encrypting = "device encrypts all user data" in lowered


def read_ata_security(device_path: str) -> AtaSecurity | None:
    """Read the ATA security state for one device, or ``None`` if unavailable."""
    if not tool_available("hdparm"):
        _log.warning("hdparm is not installed - ATA Secure Erase will be unavailable")
        return None

    result = run(["hdparm", "-I", device_path], timeout=30.0, log_output=False)
    if not result.ok or "Security:" not in result.stdout:
        _log.info("%s does not report an ATA security block", device_path)
        return None

    security = _parse_security_block(result.stdout)
    _parse_capabilities(result.stdout, security)
    _log.info(
        "%s ATA security: supported=%s enabled=%s frozen=%s locked=%s enhanced=%s (%dmin/%dmin)",
        device_path,
        security.supported,
        security.enabled,
        security.frozen,
        security.locked,
        security.enhanced_erase_supported,
        security.estimated_minutes,
        security.enhanced_estimated_minutes,
    )
    _log.info(
        "%s capabilities: trim=%s deterministic_zeros=%s self_encrypting=%s "
        "ata_sanitize(block=%s crypto=%s)",
        device_path,
        security.trim_supported,
        security.deterministic_zeros_after_trim,
        security.self_encrypting,
        security.ata_sanitize_block_supported,
        security.ata_sanitize_crypto_supported,
    )
    return security


def read_scsi_sanitize_support(device_path: str) -> bool:
    """Probe whether the drive accepts the SCSI SANITIZE command.

    ``sg_sanitize`` without an action and with ``--dry-run`` reports whether the
    command is understood without issuing it. A drive that rejects the probe is
    assumed not to support it, which is the safe direction to be wrong in.
    """
    if not tool_available("sg_sanitize"):
        return False

    result = run(["sg_sanitize", "--dry-run", "--block", device_path], timeout=30.0, log_output=False)
    combined = f"{result.stdout}\n{result.stderr}".lower()
    if "invalid" in combined or "illegal request" in combined or "not supported" in combined:
        return False
    return result.ok


def enrich_ata_devices(devices: list[Device]) -> None:
    """Attach :class:`AtaSecurity` to every non-NVMe device in *devices*.

    Mutates the list in place. Protected devices are skipped entirely: there is
    no reason to send identify commands to the disk the tool is running from.
    """
    for device in devices:
        if device.kind is DeviceKind.NVME or device.is_system:
            continue
        if device.kind not in _ATA_LIKE:
            continue

        security = read_ata_security(device.path)
        if security is None:
            security = AtaSecurity()
        if device.kind in (DeviceKind.SCSI, DeviceKind.UNKNOWN) and not security.supported:
            security.scsi_sanitize_supported = read_scsi_sanitize_support(device.path)
        device.ata = security
