"""Collecting everything needed to diagnose a failed run, in one action.

Every fault found in this tool so far was diagnosed from a log that nearly did
not survive: the live filesystem is a tmpfs, so ``/var/log`` dies at power off,
and the interesting evidence is scattered across the application log, the
systemd journal, the partition table and whatever the drive's own firmware
reports. Reconstructing that by hand over a chat session, from a machine that
has already been rebooted, is slow and lossy.

So this gathers the lot onto the writable medium in one pass. It is
deliberately generous: a few hundred kilobytes of text against the cost of
another boot cycle is not a close call.

**Nothing here is allowed to fail the operation.** Every command is best
effort, every section is wrapped, and a section that cannot be collected says
so in the output rather than aborting the export. An operator reaching for this
button already has a problem; handing them a second one would be unkind.

**Nothing here writes to a drive.** Every command is a read. This runs as root,
often with drives attached that are about to be erased and drives that must not
be, so the list is kept explicit and auditable rather than assembled
dynamically.
"""

from __future__ import annotations

import platform
import shutil
from datetime import datetime
from pathlib import Path

from . import __app_name__, __version__
from .discovery.nvme import controller_path_for
from .discovery.volumes import find_writable_volume
from .logging_setup import get_logger
from .models import Device
from .process import run, tool_available

_log = get_logger("Diagnostics")

#: Where the application writes its own logs.
_LOG_DIRECTORY = Path("/var/log/zeroize")

#: Directory created on the output medium.
_EXPORT_DIRECTORY = "Zeroize Diagnostics"

#: Commands that describe the machine and its storage. Each is (filename,
#: argv). All are read-only.
_SYSTEM_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    ("lsblk.txt", ["lsblk", "-O"]),
    ("lsblk-tree.txt", ["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS,TRAN,ROTA,RO"]),
    ("blkid.txt", ["blkid"]),
    ("mounts.txt", ["findmnt", "-A"]),
    ("partitions.txt", ["cat", "/proc/partitions"]),
    ("cmdline.txt", ["cat", "/proc/cmdline"]),
    ("uname.txt", ["uname", "-a"]),
    ("free.txt", ["free", "-h"]),
    ("lsusb.txt", ["lsusb"]),
    ("lspci.txt", ["lspci", "-nn"]),
    ("dmesg.txt", ["dmesg", "--ctime"]),
    ("systemd-failed.txt", ["systemctl", "--failed", "--no-pager"]),
)

#: Our own units, whose journals explain the boot-time work.
_UNITS = ("zeroize-prepare-session.service", "zeroize-expand-certstore.service")


def _capture(target: Path, argv: list[str]) -> None:
    """Run *argv* and write its output to *target*. Never raises."""
    try:
        if not tool_available(argv[0]):
            target.write_text(f"{argv[0]} is not installed\n", encoding="utf-8")
            return
        result = run(argv, timeout=60.0, log_output=False)
        body = result.stdout or ""
        if result.stderr:
            body += f"\n--- stderr ---\n{result.stderr}"
        if not result.ok:
            body += f"\n--- exit {result.returncode} ---\n"
        target.write_text(body or "(no output)\n", encoding="utf-8")
    except Exception as error:  # noqa: BLE001 - an export must not fail
        try:
            target.write_text(f"collection failed: {error}\n", encoding="utf-8")
        except OSError:
            pass


def _capture_drive_detail(directory: Path, devices: list[Device]) -> None:
    """Ask each drive's firmware what it thinks its own state is.

    This is the evidence that matters most and the evidence that disappears
    first: a controller that accepted a sanitize and ignored it looks identical
    to one that never got the command, unless its own log page was read.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for device in devices:
        safe = device.name.replace("/", "_")
        if device.kind.value == "nvme":
            controller = controller_path_for(device.path)
            for suffix, argv in (
                ("id-ctrl", ["nvme", "id-ctrl", controller]),
                ("sanitize-log", ["nvme", "sanitize-log", controller]),
                ("smart-log", ["nvme", "smart-log", controller]),
                ("id-ns", ["nvme", "id-ns", device.path]),
                ("list-ns", ["nvme", "list-ns", controller]),
            ):
                _capture(directory / f"{safe}-{suffix}.txt", argv)
        else:
            _capture(directory / f"{safe}-hdparm.txt", ["hdparm", "-I", device.path])
            _capture(directory / f"{safe}-smart.txt", ["smartctl", "-a", device.path])


def collect_diagnostics(
    devices: list[Device] | None = None,
    *,
    output_label: str = "",
    destination: Path | None = None,
) -> Path:
    """Gather logs and system state into a timestamped directory.

    Returns the directory written. Prefers the labelled output volume, because
    that is the one that survives a power cycle and can be read on another
    machine; falls back to the home directory so the button still does
    something useful when no medium is attached.
    """
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H-%M-%S")

    if destination is None:
        volume = find_writable_volume(output_label) if output_label else None
        base = volume if volume is not None else Path.home()
        destination = base / _EXPORT_DIRECTORY / f"diagnostics-{stamp}"

    destination.mkdir(parents=True, exist_ok=True)
    _log.info("Collecting diagnostics into %s", destination)

    # A summary first, so the directory explains itself to whoever opens it.
    summary = [
        f"{__app_name__} {__version__} diagnostics",
        f"Collected : {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}",
        f"Host      : {platform.node()}",
        f"Kernel    : {platform.release()}",
        f"Python    : {platform.python_version()}",
        f"Drives    : {len(devices) if devices else 0} detected",
        "",
        "Contents:",
        "  logs/          the application's own logs, newest last",
        "  system/        block devices, mounts, dmesg, kernel command line",
        "  drives/        each drive's firmware state as the drive reports it",
        "  journal/       systemd journal for the Zeroize boot-time services",
        "  config.json    the configuration in force for this run",
        "",
    ]
    if devices:
        summary.append("Detected drives:")
        summary.extend(
            f"  {device.path:16} {device.kind.value:5} {device.model or 'unknown'} "
            f"({device.serial or 'no serial'})"
            for device in devices
        )
    (destination / "README.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")

    # 1. Our own logs.
    logs = destination / "logs"
    logs.mkdir(exist_ok=True)
    copied = 0
    try:
        for source in sorted(_LOG_DIRECTORY.glob("*.log")):
            try:
                shutil.copy2(source, logs / source.name)
                copied += 1
            except OSError as error:
                _log.info("Could not copy %s: %s", source, error)
    except OSError as error:
        (logs / "COLLECTION-FAILED.txt").write_text(str(error), encoding="utf-8")
    _log.info("Copied %d log file(s)", copied)

    # 2. The machine.
    system = destination / "system"
    system.mkdir(exist_ok=True)
    for filename, argv in _SYSTEM_COMMANDS:
        _capture(system / filename, argv)

    # 3. The drives themselves.
    if devices:
        _capture_drive_detail(destination / "drives", devices)

    # 4. The boot-time services.
    journal = destination / "journal"
    journal.mkdir(exist_ok=True)
    for unit in _UNITS:
        _capture(journal / f"{unit}.txt", ["journalctl", "-u", unit, "--no-pager"])
    _capture(journal / "boot.txt", ["journalctl", "-b", "--no-pager"])

    # 5. The configuration actually in force.
    for candidate in (Path("/etc/zeroize/config.json"), Path.home() / ".config/zeroize/config.json"):
        if candidate.is_file():
            try:
                shutil.copy2(candidate, destination / "config.json")
            except OSError:
                pass
            break

    _log.info("Diagnostics written to %s", destination)
    return destination
