"""Enumerate attached block devices and their partition layout via ``lsblk``.

``lsblk --json --bytes`` is the source of truth for the drive list because it
already resolves the whole stack - partitions, LVM, LUKS, RAID - into a tree,
and it is present on every target distribution without an extra dependency.
Everything vendor-specific (NVMe capability registers, ATA security state) is
layered on afterwards by the sibling modules.

The other job of this module is the safety interlock. :func:`_apply_protection`
marks any device that carries the running system - root, ``/boot``, ``/usr``,
active swap, or the live medium the tool itself booted from - so the UI can
refuse to select it. That check is deliberately conservative and based on what
is *actually mounted* rather than on device naming, because on a live USB the
boot medium is frequently ``/dev/sda`` while the drive to be wiped is
``/dev/nvme0n1``, and a naming heuristic would get it exactly backwards.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..logging_setup import get_logger
from ..models import Device, DeviceKind, Partition
from ..process import run, tool_available

_log = get_logger("Discovery")

#: Mount points that make the owning device the running system. A device
#: carrying any of these is never erasable, whatever else is true of it.
_CRITICAL_MOUNTPOINTS = frozenset(
    {
        "/",
        "/boot",
        "/boot/efi",
        "/efi",
        "/usr",
        "/var",
        "/etc",
        "/home",
        # Live-boot media, under the names Debian live, Ubuntu casper and
        # dracut respectively use. Wiping the USB we are running from would
        # take the tool down mid-erase.
        "/run/live/medium",
        "/lib/live/mount/medium",
        "/cdrom",
        "/run/initramfs/live",
        "/run/rootfsbase",
    }
)

#: lsblk node types that represent a whole drive rather than a slice of one.
_WHOLE_DEVICE_TYPES = frozenset({"disk", "loop"})

_LSBLK_COLUMNS = [
    "NAME",
    "PATH",
    "TYPE",
    "SIZE",
    "MODEL",
    "SERIAL",
    "REV",
    "VENDOR",
    "TRAN",
    "ROTA",
    "RM",
    "RO",
    "PTTYPE",
    "PARTTYPENAME",
    "PARTUUID",
    "FSTYPE",
    "LABEL",
    "MOUNTPOINT",
    # MOUNTPOINTS, plural, is essential and not optional: a device mounted in
    # more than one place reports only the first through MOUNTPOINT, and the
    # one that matters is not reliably first. Requesting an explicit column
    # list means anything not named here is simply absent from the output.
    "MOUNTPOINTS",
    "LOG-SEC",
    "PHY-SEC",
    "START",
    "SUBSYSTEMS",
]


def _lsblk_json() -> dict:
    """Run lsblk and return the parsed tree, or an empty tree on failure.

    Two invocations are attempted: the explicit column list first, then a bare
    ``--json --bytes``. Older util-linux builds reject a column they do not
    know, and losing one optional column is not a reason to show the operator
    no drives at all.
    """
    if not tool_available("lsblk"):
        _log.error("lsblk is not installed - cannot enumerate block devices")
        return {}

    attempts = [
        ["lsblk", "--json", "--bytes", "--paths", "--output", ",".join(_LSBLK_COLUMNS)],
        ["lsblk", "--json", "--bytes", "--paths", "--output-all"],
        ["lsblk", "--json", "--bytes", "--paths"],
    ]
    for argv in attempts:
        result = run(argv, timeout=30.0, log_output=False)
        if not result.ok:
            continue
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            _log.warning("lsblk returned unparseable JSON: %s", error)
    _log.error("Every lsblk invocation failed - no devices discovered")
    return {}


def _as_int(value: object, default: int = 0) -> int:
    """lsblk emits numbers as ints, strings or null depending on the version."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _as_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _mountpoints(node: dict) -> list[str]:
    """Collect this node's mount points across lsblk's singular/plural fields."""
    found: list[str] = []
    single = _as_text(node.get("mountpoint"))
    if single:
        found.append(single)
    plural = node.get("mountpoints")
    if isinstance(plural, list):
        found.extend(_as_text(entry) for entry in plural if entry)
    return [point for point in found if point]


def _classify(node: dict) -> DeviceKind:
    """Map lsblk's transport and subsystem strings onto a :class:`DeviceKind`."""
    transport = _as_text(node.get("tran")).lower()
    subsystems = _as_text(node.get("subsystems")).lower()
    name = _as_text(node.get("name")).lower()

    if transport == "nvme" or "nvme" in subsystems or "/nvme" in name or name.startswith("nvme"):
        return DeviceKind.NVME
    if transport == "usb" or "usb" in subsystems:
        return DeviceKind.USB
    if transport in ("sata", "ata") or "ata" in subsystems.split(":"):
        return DeviceKind.ATA
    if transport in ("sas", "scsi", "iscsi", "fc"):
        return DeviceKind.SCSI
    if transport == "mmc" or "mmc" in subsystems:
        return DeviceKind.MMC
    if name.startswith(("/dev/loop", "/dev/zram", "/dev/ram", "/dev/vd", "/dev/dm-", "/dev/md")):
        return DeviceKind.VIRTUAL
    return DeviceKind.UNKNOWN


def _build_partition(node: dict) -> Partition:
    """Convert one lsblk child node (partition, LVM LV, LUKS mapping) over."""
    mounts = _mountpoints(node)
    partition = Partition(
        name=_as_text(node.get("name")).rsplit("/", 1)[-1],
        path=_as_text(node.get("path")) or _as_text(node.get("name")),
        size_bytes=_as_int(node.get("size")),
        fstype=_as_text(node.get("fstype")),
        label=_as_text(node.get("label")),
        mountpoint=mounts[0] if mounts else "",
        part_type_name=_as_text(node.get("parttypename")),
        part_uuid=_as_text(node.get("partuuid")),
        start_sector=_as_int(node.get("start")),
    )
    logical_sector = _as_int(node.get("log-sec"), 512) or 512
    partition.sector_count = partition.size_bytes // logical_sector
    partition.children = [_build_partition(child) for child in node.get("children", []) or []]
    return partition


def _build_device(node: dict) -> Device:
    """Convert one whole-disk lsblk node into a :class:`Device`."""
    device = Device(
        path=_as_text(node.get("path")) or _as_text(node.get("name")),
        name=_as_text(node.get("name")).rsplit("/", 1)[-1],
        kind=_classify(node),
        model=_as_text(node.get("model")),
        vendor=_as_text(node.get("vendor")),
        serial=_as_text(node.get("serial")),
        firmware=_as_text(node.get("rev")),
        size_bytes=_as_int(node.get("size")),
        logical_sector_size=_as_int(node.get("log-sec"), 512) or 512,
        physical_sector_size=_as_int(node.get("phy-sec"), 512) or 512,
        rotational=_as_bool(node.get("rota")),
        removable=_as_bool(node.get("rm")),
        read_only=_as_bool(node.get("ro")),
        transport=_as_text(node.get("tran")),
        partition_table=_as_text(node.get("pttype")),
    )
    device.mountpoints = _mountpoints(node)
    device.partitions = [_build_partition(child) for child in node.get("children", []) or []]
    return device


#: Escapes the kernel applies to paths in /proc/self/mountinfo.
_MOUNTINFO_ESCAPES = (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\"))


def _mount_table() -> dict[str, list[str]]:
    """Map ``"major:minor"`` to every path that device is mounted at.

    Read straight from ``/proc/self/mountinfo`` rather than taken from lsblk.
    This is the safety interlock, and it should not depend on which columns a
    given lsblk version was asked for or chose to populate - a missing column
    is silently empty, and silently empty here means offering the running
    system as erasable.

    Keying on the device number rather than the source path also sidesteps
    naming: the same device can appear as ``/dev/sda1``, a ``/dev/disk/by-uuid``
    symlink, or a mapper name, and all of them share one ``major:minor``.
    """
    table: dict[str, list[str]] = {}
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return table

    for line in lines:
        fields = line.split(" ")
        if len(fields) < 5:
            continue
        device_id = fields[2]
        mountpoint = fields[4]
        for escape, literal in _MOUNTINFO_ESCAPES:
            mountpoint = mountpoint.replace(escape, literal)
        table.setdefault(device_id, []).append(mountpoint)

    return table


def _device_id(path: str) -> str:
    """The ``"major:minor"`` of a block device node, or an empty string."""
    try:
        rdev = os.stat(path).st_rdev
    except OSError:
        return ""
    return f"{os.major(rdev)}:{os.minor(rdev)}"


def _resolved_mountpoints(path: str, declared: list[str], mount_table: dict[str, list[str]]) -> list[str]:
    """Every place *path* is mounted, from lsblk and the kernel combined."""
    found = list(declared)
    for mountpoint in mount_table.get(_device_id(path), []):
        if mountpoint not in found:
            found.append(mountpoint)
    return found


def _active_swap_sources() -> set[str]:
    """Device paths currently in use as swap, resolved through symlinks."""
    sources: set[str] = set()
    try:
        lines = Path("/proc/swaps").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return sources
    for line in lines:
        fields = line.split()
        if not fields:
            continue
        candidate = fields[0]
        if candidate.startswith("/dev/"):
            sources.add(os.path.realpath(candidate))
    return sources


def _is_critical_mountpoint(mountpoint: str) -> bool:
    """True when a filesystem mounted here makes its device the running system."""
    if not mountpoint:
        return False
    normalised = mountpoint.rstrip("/") or "/"
    return normalised in _CRITICAL_MOUNTPOINTS or mountpoint == "/"


def _protection_reason(
    device: Device,
    swap_sources: set[str],
    mount_table: dict[str, list[str]],
) -> str:
    """Return why *device* must not be erased, or an empty string if it may be.

    Three things are checked, and the order matters less than the coverage:

    * the device's own swap use and its own mount points - a disk with no
      partition table can carry a filesystem directly, and then there are no
      partitions to walk. mdadm members, ZFS vdevs, LUKS containers, anything
      formatted with ``mkfs /dev/sdb``, and the root disk of a WSL
      distribution all look like this;
    * every partition, recursively, so root on LVM on LUKS on a partition
      still protects the physical drive underneath.
    """

    def walk(nodes: list[Partition]) -> str:
        for node in nodes:
            if os.path.realpath(node.path) in swap_sources:
                return f"{node.path} is in use as swap"
            declared = [node.mountpoint] if node.mountpoint else []
            for mountpoint in _resolved_mountpoints(node.path, declared, mount_table):
                if _is_critical_mountpoint(mountpoint):
                    return f"{node.path} is mounted at {mountpoint}"
            nested = walk(node.children)
            if nested:
                return nested
        return ""

    if os.path.realpath(device.path) in swap_sources:
        return f"{device.path} is in use as swap"

    for mountpoint in _resolved_mountpoints(device.path, device.mountpoints, mount_table):
        if _is_critical_mountpoint(mountpoint):
            return f"{device.path} is mounted at {mountpoint}"

    return walk(device.partitions)


def _apply_protection(devices: list[Device]) -> None:
    """Flag every device that carries the running system or the boot medium."""
    swap_sources = _active_swap_sources()
    mount_table = _mount_table()
    for device in devices:
        # Fold anything the kernel knows about back onto the device, so the
        # rest of the application sees the complete picture too - the engine's
        # unmount step reads these.
        device.mountpoints = _resolved_mountpoints(device.path, device.mountpoints, mount_table)

        reason = _protection_reason(device, swap_sources, mount_table)
        if reason:
            device.is_system = True
            device.protection_reason = reason
            _log.warning("Protected %s: %s", device.path, reason)
        elif device.read_only:
            device.protection_reason = f"{device.path} is read-only"


def _should_list(node: dict, include_virtual: bool) -> bool:
    """Filter out nodes that are not erasable physical media."""
    if _as_text(node.get("type")) not in _WHOLE_DEVICE_TYPES:
        return False
    path = _as_text(node.get("path")) or _as_text(node.get("name"))
    if _as_int(node.get("size")) <= 0:
        return False
    if not include_virtual and path.startswith(("/dev/loop", "/dev/zram", "/dev/ram")):
        return False
    return True


def discover_block_devices(*, include_virtual: bool = False) -> list[Device]:
    """Return every whole block device, with partitions and protection applied.

    ``include_virtual`` adds loop and ram devices, which is only useful for
    testing against a file-backed fake drive.
    """
    tree = _lsblk_json()
    nodes = tree.get("blockdevices", []) if isinstance(tree, dict) else []

    devices = [_build_device(node) for node in nodes if _should_list(node, include_virtual)]
    _apply_protection(devices)

    devices.sort(key=lambda device: (device.kind != DeviceKind.NVME, device.path))
    _log.info(
        "Discovered %d block device(s): %s",
        len(devices),
        ", ".join(f"{device.path} ({device.kind})" for device in devices) or "none",
    )
    return devices
