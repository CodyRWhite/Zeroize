"""Stable drive identity, independent of the device node.

``/dev/nvme0n1`` names a position, not a drive. Everything that renumbers -
a controller reset, a subsystem reset, a PCI rescan, a suspend and resume -
can move a drive to a different node and free the old one for another drive.

The kernel already publishes stable handles, and this module reads them:

    /dev/disk/by-id/nvme-eui.002538a901d1201a
    /dev/disk/by-id/nvme-SAMSUNG_MZVLQ256HAJD-000H1_S4UJNF1N976769
    /sys/class/block/nvme0n1/wwid

None of this prevents a drive from moving. It makes a move detectable, which
is the part that matters before issuing a command that cannot be undone.
"""

from __future__ import annotations

from pathlib import Path

from ..logging_setup import get_logger
from ..models import Device

_log = get_logger("Identity")

_BY_ID = Path("/dev/disk/by-id")
_SYS_BLOCK = Path("/sys/class/block")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def identity_of(device_path: str) -> tuple[str, str]:
    """Return ``(serial, wwid)`` for whatever is at *device_path* right now.

    Read from sysfs rather than from a cached scan, because the entire point is
    to find out whether the thing at this path has changed since the scan.
    Missing values come back empty, and an empty value never matches, so an
    unreadable drive fails the comparison rather than passing it by default.
    """
    name = Path(device_path).name
    base = _SYS_BLOCK / name
    if not base.exists():
        return "", ""

    serial = _read(base / "device" / "serial")
    if not serial:
        # SCSI and USB keep it a level further out; NVMe namespaces point at
        # the controller.
        serial = _read(base / "device" / "device" / "serial")
    wwid = _read(base / "wwid") or _read(base / "device" / "wwid")
    return serial, wwid


def current_path_for(serial: str, wwid: str = "") -> str:
    """The device node currently carrying *serial* (or *wwid*), or "".

    Searches /dev/disk/by-id first because those names embed the identity, and
    falls back to reading sysfs for every block device. Returns "" when the
    drive is not present - which is a refusal to guess, not a failure to look.
    """
    if not serial and not wwid:
        return ""

    if _BY_ID.is_dir():
        for link in sorted(_BY_ID.iterdir()):
            name = link.name
            if serial and serial not in name:
                if not wwid or wwid.replace("eui.", "") not in name:
                    continue
            try:
                resolved = link.resolve()
            except OSError:
                continue
            if resolved.exists():
                return str(resolved)

    if _SYS_BLOCK.is_dir():
        for entry in sorted(_SYS_BLOCK.iterdir()):
            candidate = f"/dev/{entry.name}"
            found_serial, found_wwid = identity_of(candidate)
            if serial and found_serial == serial:
                return candidate
            if wwid and found_wwid and found_wwid == wwid:
                return candidate

    return ""


def confirm_identity(device: Device) -> str:
    """Empty string when *device* is still at its path, else why it is not.

    Called immediately before a destructive command, not at scan time. A scan
    minutes old proves nothing: the whole failure mode is that something moved
    in between.

    A drive that reports no serial at all cannot be confirmed either way. That
    is reported as a refusal rather than waved through, because the alternative
    is issuing an irreversible command to a device whose identity is unknown.
    """
    expected = (device.serial or "").strip()
    if not expected:
        # Nothing to compare against. Only the path is known, so say so plainly
        # rather than implying a check happened.
        _log.warning("%s reports no serial; identity cannot be confirmed", device.path)
        return ""

    actual, _ = identity_of(device.path)
    if not actual:
        moved_to = current_path_for(expected, device.wwn)
        if moved_to:
            return (
                f"{device.path} no longer reports a serial; the drive with serial "
                f"{expected} is now at {moved_to}. Rescan before erasing."
            )
        return (
            f"{device.path} no longer reports a serial, and no attached drive "
            f"has serial {expected}. Rescan before erasing."
        )

    if actual != expected:
        moved_to = current_path_for(expected, device.wwn)
        where = f" The selected drive is now at {moved_to}." if moved_to else ""
        return (
            f"{device.path} now reports serial {actual}, but {expected} was "
            f"selected. The device nodes have been renumbered.{where} "
            "Rescan before erasing."
        )

    return ""
