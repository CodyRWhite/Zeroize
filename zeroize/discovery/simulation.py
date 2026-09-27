"""A fixed set of fake drives for exercising the app without hardware.

Selected with ``--simulate``. This exists for three reasons that all matter on
a tool whose real code path is irreversible:

* The UI, the method gating and the certificate can be developed and reviewed
  on a workstation with nothing to erase.
* Every interesting capability combination is represented, including the awkward
  ones - a drive with Format NVM but no crypto erase, a drive with Sanitize but
  no Format, a BIOS-frozen SATA disk, and a protected system disk - so the
  gating logic is exercised against cases that are rare on any one bench.
* The certificate layout can be regression-checked against a known input.

Simulated devices carry ``simulated=True``, which the erase engine checks before
issuing any command, so a simulated run can never reach real hardware even if a
device path happens to collide with something real.
"""

from __future__ import annotations

from ..models import AtaSecurity, Device, DeviceKind, NvmeCapabilities, Partition

_GB = 1000**3
_TB = 1000**4


def _partition(
    name: str,
    size_bytes: int,
    *,
    fstype: str = "",
    label: str = "",
    mountpoint: str = "",
    type_name: str = "",
    start_sector: int = 2048,
) -> Partition:
    return Partition(
        name=name.rsplit("/", 1)[-1],
        path=name,
        size_bytes=size_bytes,
        fstype=fstype,
        label=label,
        mountpoint=mountpoint,
        part_type_name=type_name,
        start_sector=start_sector,
        sector_count=size_bytes // 512,
    )


def simulated_devices() -> list[Device]:
    """Return the standard simulated drive set."""
    devices: list[Device] = []

    # A mainstream consumer NVMe drive: Format NVM with crypto erase, but no
    # Sanitize support at all. This is by far the most common real case.
    consumer_nvme = Device(
        path="/dev/nvme0n1",
        name="nvme0n1",
        kind=DeviceKind.NVME,
        model="Samsung SSD 970 EVO 250GB",
        vendor="Samsung",
        serial="DD56419883A62",
        firmware="2B2QEXE7",
        size_bytes=250_059_350_016,
        logical_sector_size=512,
        physical_sector_size=512,
        rotational=False,
        transport="nvme",
        partition_table="gpt",
        simulated=True,
        nvme=NvmeCapabilities(
            controller_path="/dev/nvme0",
            namespace_id=1,
            format_supported=True,
            crypto_erase_supported=True,
            format_applies_to_all_namespaces=True,
            model="Samsung SSD 970 EVO 250GB",
            firmware="2B2QEXE7",
            raw_oacs=0x0017,
            raw_fna=0x05,
            raw_sanicap=0x00000000,
        ),
    )
    consumer_nvme.partitions = [
        _partition("/dev/nvme0n1p1", 536_870_912, fstype="vfat", label="EFI", type_name="EFI System"),
        _partition("/dev/nvme0n1p2", 214_748_364_800, fstype="ext4", label="root", type_name="Linux filesystem"),
        _partition("/dev/nvme0n1p3", 34_359_738_368, fstype="swap", label="swap", type_name="Linux swap"),
    ]
    devices.append(consumer_nvme)

    # An enterprise NVMe drive that supports the full Sanitize set. The method
    # list for this one should offer block, crypto and overwrite sanitize.
    enterprise_nvme = Device(
        path="/dev/nvme1n1",
        name="nvme1n1",
        kind=DeviceKind.NVME,
        model="KIOXIA KCD6XLUL1T92",
        vendor="KIOXIA",
        serial="Y0V0A01ATU18",
        firmware="0104",
        size_bytes=1_920_383_410_176,
        logical_sector_size=4096,
        physical_sector_size=4096,
        rotational=False,
        transport="nvme",
        partition_table="gpt",
        simulated=True,
        nvme=NvmeCapabilities(
            controller_path="/dev/nvme1",
            namespace_id=1,
            format_supported=True,
            crypto_erase_supported=True,
            format_applies_to_all_namespaces=True,
            sanitize_crypto_supported=True,
            sanitize_block_supported=True,
            sanitize_overwrite_supported=True,
            model="KIOXIA KCD6XLUL1T92",
            firmware="0104",
            raw_oacs=0x005F,
            raw_fna=0x05,
            raw_sanicap=0x00000007,
        ),
    )
    enterprise_nvme.partitions = [
        _partition("/dev/nvme1n1p1", 1_920_000_000_000, fstype="xfs", label="data", type_name="Linux filesystem"),
    ]
    devices.append(enterprise_nvme)

    # An older NVMe drive with no Format NVM support at all - every NVMe method
    # should be greyed out, leaving only the software overwrite methods.
    limited_nvme = Device(
        path="/dev/nvme2n1",
        name="nvme2n1",
        kind=DeviceKind.NVME,
        model="INTEL SSDPEKKW256G7",
        vendor="Intel",
        serial="BTPY72960ATT256D",
        firmware="PSF109C",
        size_bytes=256_060_514_304,
        transport="nvme",
        partition_table="",
        simulated=True,
        nvme=NvmeCapabilities(
            controller_path="/dev/nvme2",
            namespace_id=1,
            format_supported=False,
            crypto_erase_supported=False,
            model="INTEL SSDPEKKW256G7",
            firmware="PSF109C",
            raw_oacs=0x0006,
            raw_fna=0x00,
            raw_sanicap=0x00000000,
        ),
    )
    devices.append(limited_nvme)

    # A SATA SSD whose ATA security has been frozen by the BIOS - Secure Erase
    # must be offered but blocked, with the unfreeze instructions shown.
    frozen_sata = Device(
        path="/dev/sda",
        name="sda",
        kind=DeviceKind.ATA,
        model="CT500MX500SSD1",
        vendor="Crucial",
        serial="2015E4A1B2C3",
        firmware="M3CR045",
        size_bytes=500_107_862_016,
        rotational=False,
        transport="sata",
        partition_table="dos",
        simulated=True,
        ata=AtaSecurity(
            supported=True,
            enabled=False,
            locked=False,
            frozen=True,
            enhanced_erase_supported=True,
            estimated_minutes=2,
            enhanced_estimated_minutes=2,
        ),
    )
    frozen_sata.partitions = [
        _partition("/dev/sda1", 500_000_000_000, fstype="ntfs", label="Windows", type_name="Microsoft basic data"),
    ]
    devices.append(frozen_sata)

    # A spinning disk with usable ATA security - the classic multi-pass
    # overwrite candidate, where the pass count actually costs hours.
    rotational_disk = Device(
        path="/dev/sdb",
        name="sdb",
        kind=DeviceKind.ATA,
        model="WDC WD20EFRX-68EUZN0",
        vendor="WDC",
        serial="WD-WCC4M0KZ7X9L",
        firmware="82.00A82",
        size_bytes=2_000_398_934_016,
        rotational=True,
        transport="sata",
        partition_table="gpt",
        simulated=True,
        ata=AtaSecurity(
            supported=True,
            enabled=False,
            locked=False,
            frozen=False,
            enhanced_erase_supported=True,
            estimated_minutes=254,
            enhanced_estimated_minutes=254,
        ),
    )
    rotational_disk.partitions = [
        _partition("/dev/sdb1", 2_000_000_000_000, fstype="ext4", label="archive", type_name="Linux filesystem"),
    ]
    devices.append(rotational_disk)

    # The system disk, protected. It must appear in the list but never be
    # selectable - proving the interlock is visible, not just enforced.
    system_disk = Device(
        path="/dev/sdc",
        name="sdc",
        kind=DeviceKind.USB,
        model="SanDisk Ultra",
        vendor="SanDisk",
        serial="4C530001120523107323",
        firmware="1.00",
        size_bytes=61_530_439_680,
        removable=True,
        transport="usb",
        partition_table="gpt",
        is_system=True,
        protection_reason="/dev/sdc1 is mounted at /run/live/medium",
        simulated=True,
    )
    system_disk.partitions = [
        _partition(
            "/dev/sdc1",
            61_000_000_000,
            fstype="vfat",
            label="ZEROIZE",
            mountpoint="/run/live/medium",
            type_name="EFI System",
        ),
    ]
    devices.append(system_disk)

    return devices
