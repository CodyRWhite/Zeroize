"""Host identification for the certificate's System and Hardware blocks.

The certificate has to identify the machine the erase was performed on, not
just the drives, so that a chain-of-custody record ties the two together. The
reference KillDisk certificate names the host by its BIOS serial number and
then lists OS, kernel, CPU, motherboard, BIOS and memory; the same fields are
gathered here from their Linux equivalents.

Two sources are used, in order of preference:

* ``/sys/class/dmi/id/*`` - no subprocess, but the serial-number files are
  mode 0400 and readable only by root. Since the tool runs as root this is
  normally sufficient and is tried first.
* ``dmidecode`` - the fallback, and the only option on hosts where the sysfs
  DMI tree is absent (some VMs and most ARM boards).

Everything degrades to ``Unknown`` rather than raising. A certificate with an
unknown motherboard serial is still a valid record of the erase; a crash at the
point of issuing it is not.
"""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path

from ..logging_setup import get_logger
from ..models import format_size
from ..process import run, tool_available

_log = get_logger("SysInfo")

_UNKNOWN = "Unknown"

#: sysfs DMI values that mean "the vendor left the field blank".
_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "to be filled by o.e.m.",
        "to be filled by oem",
        "default string",
        "system serial number",
        "not specified",
        "not available",
        "none",
        "o.e.m.",
        "unknown",
    }
)

_DMI_ROOT = Path("/sys/class/dmi/id")


def _clean(value: str | None) -> str:
    """Normalise a DMI string, mapping vendor placeholders to an empty string."""
    text = (value or "").strip()
    return "" if text.lower() in _PLACEHOLDER_VALUES else text


def _read_dmi(field: str) -> str:
    """Read one ``/sys/class/dmi/id`` field, or an empty string."""
    try:
        return _clean((_DMI_ROOT / field).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _dmidecode(keyword: str) -> str:
    """Read one ``dmidecode -s <keyword>`` value, or an empty string."""
    if not tool_available("dmidecode"):
        return ""
    result = run(["dmidecode", "-s", keyword], timeout=20.0, log_output=False)
    if not result.ok:
        return ""
    # dmidecode prints warning lines starting with '#' before the value.
    for line in result.stdout.splitlines():
        cleaned = _clean(line)
        if cleaned and not cleaned.startswith("#"):
            return cleaned
    return ""


def _dmi_value(sysfs_field: str, dmidecode_keyword: str) -> str:
    """First non-empty answer from sysfs then dmidecode, else ``Unknown``."""
    return _read_dmi(sysfs_field) or _dmidecode(dmidecode_keyword) or _UNKNOWN


def _os_pretty_name() -> str:
    """``PRETTY_NAME`` from ``/etc/os-release``, e.g. ``Ubuntu 24.04.1 LTS``."""
    try:
        content = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return f"{platform.system()} {platform.release()}"
    match = re.search(r'^PRETTY_NAME="?(?P<name>[^"\n]+)"?$', content, re.MULTILINE)
    return match.group("name").strip() if match else f"{platform.system()} {platform.release()}"


def _processor_model() -> str:
    """CPU model name from ``/proc/cpuinfo``, with the architecture appended."""
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            # x86 uses "model name"; arm64 uses "Model" or "CPU implementer".
            if line.lower().startswith(("model name", "cpu model", "model\t")):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    model = model or platform.processor() or _UNKNOWN
    architecture = platform.machine()
    return f"{model} ({architecture})" if architecture and architecture not in model else model


def _total_memory() -> str:
    """Installed RAM, read from ``/proc/meminfo`` and rendered like the label."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                kilobytes = int(re.sub(r"[^\d]", "", line.split(":", 1)[1]))
                return format_size(kilobytes * 1024)
    except (OSError, ValueError):
        pass
    return _UNKNOWN


def machine_identifier() -> str:
    """The certificate's "Computer" field.

    The reference certificate uses the system (BIOS) serial number, which is
    what asset tags are keyed on, so that is preferred; the hostname is the
    fallback when the serial is blank or a vendor placeholder.
    """
    serial = _read_dmi("product_serial") or _dmidecode("system-serial-number")
    return serial or platform.node() or _UNKNOWN


def collect_system_info() -> dict[str, str]:
    """The certificate's "System Information" block."""
    bits = "64-bit" if platform.machine() in ("x86_64", "aarch64", "ppc64le", "s390x") else "32-bit"
    return {
        "OS": _os_pretty_name(),
        "Type": bits,
        "Kernel": f"{platform.release()} (linux)",
        "Hostname": platform.node() or _UNKNOWN,
    }


def collect_hardware_info() -> dict[str, str]:
    """The certificate's "Hardware Information" block."""
    board_vendor = _dmi_value("board_vendor", "baseboard-manufacturer")
    board_name = _dmi_value("board_name", "baseboard-product-name")
    motherboard = " ".join(part for part in (board_vendor, board_name) if part != _UNKNOWN) or _UNKNOWN

    bios_vendor = _dmi_value("bios_vendor", "bios-vendor")
    bios_version = _dmi_value("bios_version", "bios-version")
    bios = " ".join(part for part in (bios_vendor, bios_version) if part != _UNKNOWN) or _UNKNOWN

    return {
        "Processor": _processor_model(),
        "Logical Processors": str(os.cpu_count() or _UNKNOWN),
        "Motherboard": motherboard,
        "Motherboard Serial": _dmi_value("board_serial", "baseboard-serial-number"),
        "BIOS": bios,
        "BIOS Serial": _dmi_value("product_serial", "system-serial-number"),
        "Memory": _total_memory(),
    }
