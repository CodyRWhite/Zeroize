"""Data model shared by discovery, the erase engine, the UI and the certificate.

Everything the application knows about a drive is captured in :class:`Device`,
which discovery fills in once and the rest of the code only reads. The erase
engine turns a (device, method) pair into a job, and a finished job carries an
:class:`EraseResult` that the certificate renders verbatim - so the PDF never
has to re-query the hardware, and a certificate can be re-issued from a saved
run without the drive still being attached.

Sizes are always bytes; durations are always seconds; timestamps are always
timezone-aware ``datetime`` objects. Conversions to human-readable forms live
in :func:`format_size` and :func:`format_duration` so the UI and the PDF agree.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


def format_size(num_bytes: int) -> str:
    """Render a byte count the way drive vendors and KillDisk do (base 10).

    A "233 GB" disk in the reference certificate is 250,059,350,016 bytes, so
    base-10 units are what an operator comparing the certificate against the
    drive label expects to see.
    """
    if num_bytes <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(num_bytes)
    index = 0
    while size >= 1000 and index < len(units) - 1:
        size /= 1000
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    precision = 0 if size >= 100 else 1
    return f"{size:.{precision}f} {units[index]}"


def format_duration(seconds: float) -> str:
    """Render a duration as ``HH:MM:SS``, matching the certificate's format."""
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class DeviceKind(enum.StrEnum):
    """Transport family of a block device - decides which erase paths apply."""

    NVME = "nvme"
    ATA = "ata"
    SCSI = "scsi"
    USB = "usb"
    MMC = "mmc"
    VIRTUAL = "virtual"
    UNKNOWN = "unknown"


class JobState(enum.StrEnum):
    """Lifecycle of a single drive's erase."""

    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


@dataclass(slots=True)
class Partition:
    """One partition (or other child node) of a block device."""

    name: str
    path: str
    size_bytes: int
    fstype: str = ""
    label: str = ""
    mountpoint: str = ""
    part_type_name: str = ""
    part_uuid: str = ""
    start_sector: int = 0
    sector_count: int = 0
    children: list[Partition] = field(default_factory=list)

    @property
    def is_mounted(self) -> bool:
        if self.mountpoint:
            return True
        return any(child.is_mounted for child in self.children)

    @property
    def display_label(self) -> str:
        """Caption for the partition map: label, else filesystem, else name."""
        return self.label or self.fstype or self.name


@dataclass(slots=True)
class NvmeCapabilities:
    """What an NVMe controller reports it can actually do.

    Populated from ``nvme id-ctrl``. The booleans here - not the mere presence
    of ``nvme-cli`` - are what gate the erase methods offered for the drive,
    which is the whole point: not every controller implements Format NVM, and
    far fewer implement Sanitize.
    """

    controller_path: str = ""
    namespace_id: int = 1
    #: OACS bit 1 - the Format NVM command is supported at all.
    format_supported: bool = False
    #: FNA bit 2 - Format NVM can do a cryptographic erase (``--ses=2``).
    crypto_erase_supported: bool = False
    #: FNA bit 0 - a format applies to *all* namespaces, not just this one.
    format_applies_to_all_namespaces: bool = False
    #: FNA bit 1 - a secure erase applies to all namespaces.
    secure_erase_applies_to_all_namespaces: bool = False
    #: SANICAP bits 0/1/2 - the three Sanitize actions.
    sanitize_crypto_supported: bool = False
    sanitize_block_supported: bool = False
    sanitize_overwrite_supported: bool = False
    #: SMART critical warning bit 3. The controller has exhausted its spare
    #: blocks and put the media into read-only mode - permanently, by design.
    #: Every erase method fails on such a drive, firmware and software alike,
    #: and the controller answers a format or sanitize with "Access Denied".
    #: Knowing this before the operator selects the drive turns a confusing
    #: refusal into a plain statement that the drive is finished.
    media_read_only: bool = False
    #: SMART critical warning bit 2 - the subsystem reports its reliability as
    #: degraded. Not itself a bar to erasing, but worth saying out loud.
    reliability_degraded: bool = False
    #: Wear indicator, as a percentage. Over 100 is allowed and means the drive
    #: is past its rated endurance.
    percentage_used: int = 0
    #: A sanitize started before this boot may still be running.
    sanitize_in_progress: bool = False
    sanitize_progress_percent: float = 0.0
    #: How many namespaces are active. Note this is the *enumerated* count from
    #: ``nvme list-ns``, not the controller's NN field, which reports the
    #: maximum it could support rather than how many exist.
    namespace_count: int = 1
    #: The active namespace ids. On a multi-namespace drive whose FNA bit 0 is
    #: clear, a Format NVM covers only the namespace it was addressed to, so
    #: the erase path must walk this list to cover the whole drive.
    active_namespaces: list[int] = field(default_factory=list)
    model: str = ""
    firmware: str = ""
    #: Identify Namespace DLFEAT bits 2:0 - what a deallocated block is
    #: GUARANTEED to read back as. 0 means the controller reports nothing,
    #: 1 means 0x00, 2 means 0xFF.
    #:
    #: This is what decides whether a discard counts as an erase. A drive
    #: reporting 0 may return zeros for a deallocated block today and its old
    #: contents after a power cycle or a garbage-collection pass, so verifying
    #: by reading measures a courtesy rather than a guarantee - and a method
    #: that passes its own verification while leaving data recoverable is worse
    #: than one that plainly fails.
    deallocated_read_behaviour: int = 0
    #: Whether Sanitize is accepted RIGHT NOW, as opposed to advertised in
    #: SANICAP. ``None`` means it was not probed.
    #:
    #: Tri-state on purpose. "Not probed" is not "available", and collapsing
    #: them is how an operator ends up selecting a drive, waiting, and being
    #: told the erase was refused.
    sanitize_reachable: bool | None = None
    #: Raw register values, kept for the log and for diagnosing odd drives.
    raw_oacs: int = 0
    raw_fna: int = 0
    raw_sanicap: int = 0
    raw_dlfeat: int = 0

    @property
    def deterministic_zeros_after_deallocate(self) -> bool:
        """Does the controller guarantee zeros from a deallocated block?"""
        return self.deallocated_read_behaviour == 1

    @property
    def sanitize_blocked(self) -> bool:
        """Sanitize is advertised but the controller is refusing it."""
        return self.any_sanitize_supported and self.sanitize_reachable is False

    @property
    def any_sanitize_supported(self) -> bool:
        return (
            self.sanitize_crypto_supported
            or self.sanitize_block_supported
            or self.sanitize_overwrite_supported
        )


@dataclass(slots=True)
class AtaSecurity:
    """The ATA security feature set as reported by ``hdparm -I``.

    ``frozen`` is the field that matters most in practice: a BIOS that issues
    SECURITY FREEZE LOCK at POST makes ATA Secure Erase impossible until the
    drive is power-cycled, and the operator needs to be told that rather than
    watching the command fail for no visible reason.
    """

    supported: bool = False
    enabled: bool = False
    locked: bool = False
    frozen: bool = False
    expired: bool = False
    enhanced_erase_supported: bool = False
    estimated_minutes: int = 0
    enhanced_estimated_minutes: int = 0
    #: SCSI/SAS SANITIZE (via ``sg_sanitize``) - separate from ATA security.
    scsi_sanitize_supported: bool = False
    #: The ATA SANITIZE feature set (ACS-3). Entirely separate from the
    #: security feature set above, and crucially *not* blocked by
    #: SECURITY FREEZE LOCK - so a frozen drive can still be purged this way,
    #: when it supports it. Many consumer SSDs do not.
    ata_sanitize_block_supported: bool = False
    ata_sanitize_crypto_supported: bool = False
    #: Data Set Management TRIM.
    trim_supported: bool = False
    #: The firmware guarantees TRIMmed blocks read back as zeros. Without this,
    #: a discard proves nothing; with it, a whole-device discard is verifiable.
    deterministic_zeros_after_trim: bool = False
    #: "Device encrypts all user data" - a self-encrypting drive. Worth
    #: recording on the certificate: it changes what residual data means.
    self_encrypting: bool = False


@dataclass(slots=True)
class Device:
    """A whole block device and everything needed to erase and certify it."""

    path: str
    name: str
    kind: DeviceKind = DeviceKind.UNKNOWN
    model: str = ""
    vendor: str = ""
    serial: str = ""
    firmware: str = ""
    wwn: str = ""
    size_bytes: int = 0
    logical_sector_size: int = 512
    physical_sector_size: int = 512
    rotational: bool = False
    removable: bool = False
    read_only: bool = False
    transport: str = ""
    partition_table: str = ""
    partitions: list[Partition] = field(default_factory=list)
    #: Mount points of the *whole device*, not of a partition on it. A disk
    #: with no partition table can carry a filesystem directly - mdadm members,
    #: ZFS vdevs, LUKS containers and anything formatted with ``mkfs /dev/sdb``
    #: all look like this, as does the root disk of a WSL distribution. Such a
    #: device has no partitions to walk, so its own mounts are the only thing
    #: standing between it and being offered as erasable.
    mountpoints: list[str] = field(default_factory=list)
    #: Set when the device carries the running system (root, /boot, active swap).
    is_system: bool = False
    #: Human-readable reason the device is protected, shown in the UI.
    protection_reason: str = ""
    nvme: NvmeCapabilities | None = None
    ata: AtaSecurity | None = None
    #: True when this device came from ``--simulate`` rather than real hardware.
    simulated: bool = False

    @property
    def total_sectors(self) -> int:
        if self.logical_sector_size <= 0:
            return 0
        return self.size_bytes // self.logical_sector_size

    @property
    def is_mounted(self) -> bool:
        return bool(self.mountpoints) or any(partition.is_mounted for partition in self.partitions)

    @property
    def can_be_erased(self) -> bool:
        """False for anything we refuse to touch regardless of method."""
        return not self.is_system and not self.read_only

    @property
    def product_name(self) -> str:
        """Vendor + model, collapsed - the certificate's Product Name field."""
        parts = [part for part in (self.vendor.strip(), self.model.strip()) if part]
        if len(parts) == 2 and parts[1].lower().startswith(parts[0].lower()):
            return parts[1]
        return " ".join(parts) or self.name

    @property
    def partition_table_display(self) -> str:
        """Certificate wording for the partitioning scheme."""
        table = (self.partition_table or "").lower()
        if table == "gpt":
            return "GPT"
        if table in ("dos", "mbr", "msdos"):
            return "MBR (Basic)"
        if not table:
            return "Unknown" if self.partitions else "None (unpartitioned)"
        return self.partition_table.upper()


@dataclass(slots=True)
class PassSpec:
    """One pass of an overwrite method.

    ``pattern`` is the repeating byte sequence written for the pass, or ``None``
    for a pseudorandom pass. ``verify`` marks a pass whose contents are read
    back and compared. ``label`` is the exact wording used in the certificate's
    "Erase Passes" list, e.g. ``Pass 4 (0x969696969696)``.
    """

    pattern: bytes | None
    label: str
    verify: bool = False


@dataclass(slots=True)
class EraseMethod:
    """A selectable way to erase a drive.

    ``family`` routes the job to an implementation; ``certificate_name`` is the
    exact string printed as "Erase Method" on the certificate, worded the way
    auditors expect (e.g. ``US DoD 5220.22-M (ECE); 7 passes``).
    """

    key: str
    certificate_name: str
    family: str
    summary: str
    standard: str = ""
    passes: list[PassSpec] = field(default_factory=list)
    #: Erases everything on the drive, not just the selected namespace.
    whole_device: bool = True
    #: Typical wall-clock behaviour, used to set operator expectations.
    speed_hint: str = ""
    #: Whether a verification read is meaningful for this method.
    supports_verification: bool = True
    #: Once started, the drive cannot be returned to service until it finishes.
    non_interruptible: bool = False

    @property
    def pass_count(self) -> int:
        return len(self.passes)


@dataclass(slots=True)
class MethodAvailability:
    """Whether a method may be offered for a specific device, and why not.

    The UI shows unsupported methods greyed out with ``reason`` as the tooltip
    rather than hiding them, so an operator can see that a drive lacks Sanitize
    support instead of wondering where the option went.
    """

    method: EraseMethod
    supported: bool
    reason: str = ""


@dataclass(slots=True)
class PassResult:
    """Outcome of a single pass, rendered in the certificate's pass list."""

    label: str
    succeeded: bool
    detail: str = ""

    @property
    def certificate_line(self) -> tuple[str, str]:
        """``(label, outcome)`` - the PDF colours the outcome independently."""
        return self.label, ("OK" if self.succeeded else "FAILED")


@dataclass(slots=True)
class EraseResult:
    """Everything the certificate needs about one finished drive erase."""

    device: Device
    method: EraseMethod
    state: JobState
    started_at: datetime
    finished_at: datetime
    passes: list[PassResult] = field(default_factory=list)
    verification_percent: float = 0.0
    verification_passed: bool | None = None
    error_messages: list[str] = field(default_factory=list)
    #: Commands actually issued, for the audit log and troubleshooting.
    commands: list[str] = field(default_factory=list)
    #: True when the run completed without the process being interrupted.
    uninterrupted: bool = True
    bytes_written: int = 0

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def succeeded(self) -> bool:
        return self.state is JobState.SUCCEEDED

    @property
    def result_word(self) -> str:
        """The certificate's "Result" field."""
        return {
            JobState.SUCCEEDED: "Erased",
            JobState.FAILED: "Failed",
            JobState.CANCELLED: "Cancelled",
        }.get(self.state, "Incomplete")

    @property
    def errors_word(self) -> str:
        """The certificate's "Errors" field."""
        if not self.error_messages:
            return "No Errors"
        if len(self.error_messages) == 1:
            return self.error_messages[0]
        return f"{len(self.error_messages)} errors"


@dataclass(slots=True)
class RunSummary:
    """A whole session: every drive erased together, and the shared context.

    One certificate is issued per :class:`RunSummary`, so erasing four NVMe
    drives in one go produces one PDF listing all four serial numbers rather
    than four separate documents.
    """

    results: list[EraseResult]
    started_at: datetime
    finished_at: datetime
    operator: str = ""
    machine: str = ""
    system_info: dict[str, str] = field(default_factory=dict)
    hardware_info: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    @property
    def serials(self) -> list[str]:
        """Serial numbers of every drive in the run, in certificate order."""
        return [result.device.serial or result.device.name for result in self.results]

    @property
    def all_succeeded(self) -> bool:
        return bool(self.results) and all(result.succeeded for result in self.results)

    @property
    def outcome_word(self) -> str:
        """``Success`` / ``Failed`` / ``Partial`` - used in the PDF filename."""
        if not self.results:
            return "Empty"
        if self.all_succeeded:
            return "Success"
        if any(result.succeeded for result in self.results):
            return "Partial"
        return "Failed"

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()
