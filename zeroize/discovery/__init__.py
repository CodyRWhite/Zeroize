"""Device discovery - one call that returns fully populated devices.

:func:`discover_devices` is the only entry point the UI and CLI use. It runs
the three stages in order, because each depends on the previous one:

1. ``lsblk`` enumerates the drives, their partition layout and what is mounted,
   which is also what decides the system-disk protection flag.
2. ``nvme-cli`` fills in the capability registers for NVMe drives.
3. ``hdparm`` / ``sg3_utils`` fill in the security state for everything else.

Stages 2 and 3 are skipped for protected devices - there is no reason to send
identify commands to the disk the tool is running from.
"""

from __future__ import annotations

from ..logging_setup import get_logger
from ..models import Device
from ..process import run
from .ata import enrich_ata_devices, read_ata_security
from .block_devices import discover_block_devices
from .nvme import controller_path_for, enrich_nvme_devices, read_capabilities, read_sanitize_status
from .simulation import simulated_devices
from .sysinfo import collect_hardware_info, collect_system_info, machine_identifier

_log = get_logger("Discovery")

__all__ = [
    "discover_devices",
    "collect_hardware_info",
    "collect_system_info",
    "machine_identifier",
    "controller_path_for",
    "read_capabilities",
    "read_sanitize_status",
    "read_ata_security",
]


def refresh_stale_caches(devices: list[Device]) -> bool:
    """Make the kernel and udev forget what they think is on erasable drives.

    True if anything was refreshed.

    THREE caches hold this information and clearing one is not enough:

    * the **kernel's** in-memory partition table, read when the device appeared
      and never revisited;
    * **blkid's** cache in ``/run/blkid/blkid.tab``;
    * **udev's** property database, which is where ``lsblk`` actually gets
      FSTYPE and LABEL from.

    Clear only the first and ``lsblk`` keeps reporting the old filesystem from
    udev, which is how an erased drive goes on showing "ntfs 999 GB" through
    repeated rescans. A drive erased by anything the kernel did not itself
    perform - another tool, a BIOS utility, a manual hdparm run - lands in
    exactly this state.

    Only drives already eligible for erasure are touched: never the running
    system, never anything mounted, never the live medium. On those the calls
    would fail anyway, since the kernel refuses while a partition is in use,
    but the point is not to go near a disk the operator is relying on however
    harmless the operation looks.

    Every failure is silent and expected. A busy drive, or a transport without
    the ioctl, simply keeps the listing it had.
    """
    refreshed = False

    for device in devices:
        if not device.can_be_erased or device.is_mounted:
            continue

        # Drop the partition device nodes first. Without this the kernel keeps
        # sdaN alive and udev keeps its stale properties, even once the table
        # itself has been re-read.
        # "partx -d <disk>" deletes every partition the kernel holds for it.
        # An earlier version passed "--nr 1-256", which is simply wrong:
        # partx ranges are colon-separated (M:N), so the argument was rejected
        # and - with stderr discarded - the failure was invisible. The stale
        # partition nodes then survived every refresh, and an erased drive went
        # on showing a partition that existed nowhere on the media.
        run(["partx", "-d", device.path], timeout=30.0, log_output=False)

        result = run(["blockdev", "--rereadpt", device.path], timeout=30.0, log_output=False)
        if result.ok:
            refreshed = True
            _log.debug("Re-read the partition table on %s", device.path)

        # Force udev to re-probe, which is what refreshes FSTYPE and LABEL.
        run(
            [
                "udevadm",
                "trigger",
                "--action=change",
                "--subsystem-match=block",
                f"--sysname-match={device.name}*",
            ],
            timeout=30.0,
            log_output=False,
        )

    if refreshed:
        # Prune blkid's own cache of entries whose devices no longer match.
        run(["blkid", "-g"], timeout=30.0, log_output=False)
        run(["udevadm", "settle"], timeout=30.0, log_output=False)

    return refreshed


def discover_devices(
    *,
    simulate: bool = False,
    include_virtual: bool = False,
    auto_unfreeze: bool = False,
    refresh_partition_tables: bool = True,
) -> list[Device]:
    """Return every attached drive with capabilities and protection resolved.

    ``simulate`` swaps in a fixed set of fake drives covering the interesting
    capability combinations, so the UI and the certificate can be exercised on
    a machine with nothing to erase.

    ``auto_unfreeze`` detaches and re-attaches any drive the firmware has
    frozen, then scans again. System firmware issues SECURITY FREEZE LOCK to
    every ATA drive at POST, which blocks Secure Erase - so on a bench tool the
    default state of the world is "the good method is unavailable", and making
    the operator notice and fix that by hand is a poor trade. Doing it here,
    before the list is ever drawn, also sidesteps the one real objection to
    automating it: a re-attached drive can come back under a different name,
    and nobody sees the old one.

    ``refresh_partition_tables`` asks the kernel to re-read the partition table
    of every erasable drive before listing it. ``lsblk`` reports the table the
    KERNEL holds, which it read when the device appeared and never revisits, so
    a drive erased by any means the kernel did not perform - another tool, a
    BIOS utility, a manual ``hdparm`` run - keeps listing partitions that are
    no longer on the media. The operator sees the same NTFS partition as before
    and concludes the wipe did nothing.
    """
    if simulate:
        devices = simulated_devices()
        _log.warning("Simulated device set in use - no real hardware will be touched")
        return devices

    devices = discover_block_devices(include_virtual=include_virtual)
    enrich_nvme_devices(devices)
    enrich_ata_devices(devices)

    if refresh_partition_tables and refresh_stale_caches(devices):
        devices = discover_block_devices(include_virtual=include_virtual)
        enrich_nvme_devices(devices)
        enrich_ata_devices(devices)

    if auto_unfreeze:
        from ..erase.unfreeze import clear_freezes

        if clear_freezes(devices):
            # Names may have changed, so nothing from the first pass can be
            # trusted - discover again from scratch.
            devices = discover_block_devices(include_virtual=include_virtual)
            enrich_nvme_devices(devices)
            enrich_ata_devices(devices)

    erasable = [device for device in devices if device.can_be_erased]
    _log.info("%d of %d device(s) are eligible for erasure", len(erasable), len(devices))
    return devices
