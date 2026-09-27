"""Clearing an ATA security freeze, so Secure Erase becomes possible.

Most system firmware issues ``SECURITY FREEZE LOCK`` to every ATA drive during
POST. It is a sensible thing for a BIOS to do - it stops malware locking or
wiping a disk - but it also blocks the entire ATA security feature set,
including the Secure Erase this tool wants to use. The ATA specification
provides no unfreeze command: the state clears only on a power-on reset.

So the job is to *cause* a reset without opening the machine. Writing to
``delete`` in sysfs removes the device, and writing to every host's ``scan``
brings it back; on a good many controllers the re-attach performs a COMRESET
that clears the freeze. It is quick, it disturbs nothing else, and it is what
worked on the HP hardware this was developed against.

**Suspend-to-RAM is the second stage, and only on request.** A true S3 cycle
removes power from the drive and clears the freeze where a bus reset will not.
It is never automatic, because S3 also cuts power to the USB controller: on
resume the bus re-enumerates, the boot stick can come back under a different
device node while the live filesystem is still a loop mount over the old one,
and every read then fails with EIO. The session dies by inches - the terminal
will not start, ``modprobe`` cannot load a module, ``nvme`` cannot be executed -
and none of that looks like a suspend problem.

The duration matters more than it ought to. A twenty-second suspend reliably
took the live session down with it; three seconds cleared the freeze on the
same hardware and left the session intact, which is why that is the default
here. Long enough for the drive to lose power, short enough that USB does not
fully tear down.

So the operator is asked first, in as many words, and the automatic path at
startup never reaches this.

**The device name can change.** After a detach and rescan the kernel assigns
whatever name is next free, so a drive that was ``/dev/sda`` may return as
``/dev/sdd``. Callers must re-run discovery and match on the serial number
rather than assuming the path survived.
"""

from __future__ import annotations

import glob
import time
from pathlib import Path

from ..logging_setup import get_logger
from ..models import Device
from ..process import run

_log = get_logger("Unfreeze")

#: How long to wait after detaching before rescanning, and after rescanning
#: before the device is expected back.
_SETTLE_SECONDS = 2.0


def can_attempt(device: Device) -> str:
    """Return why *device* must not be unfrozen, or an empty string.

    Detaching a block device that something is using is a good way to lose
    data or hang the machine, and detaching the medium we booted from would
    take the tool down with it.
    """
    if device.is_system:
        return device.protection_reason or "the device carries the running system"
    if device.is_mounted:
        return "the device has mounted filesystems; unmount them first"
    if device.ata is None or not device.ata.frozen:
        return "the drive is not frozen"
    return ""


def _read_frozen_state(device_path: str) -> bool | None:
    """Re-read just the frozen flag. ``None`` when it cannot be determined."""
    result = run(["hdparm", "-I", device_path], timeout=30.0, log_output=False)
    if not result.ok or "Security:" not in result.stdout:
        return None

    for line in result.stdout.splitlines():
        stripped = line.strip().lower()
        if stripped.endswith("frozen"):
            # "not\tfrozen" is unfrozen; a bare "frozen" is not.
            return not stripped.startswith("not")
    return None


def detach_and_rescan(device: Device) -> tuple[bool, str]:
    """Detach the device and rescan every SCSI host.

    Returns ``(attempted, detail)``. ``attempted`` says the sequence ran, not
    that the freeze cleared - the caller re-scans and checks, because the
    device may come back under a different name.
    """
    delete_path = Path(f"/sys/block/{device.name}/device/delete")
    if not delete_path.exists():
        return False, f"{delete_path} does not exist; this is not a SCSI/SATA device"

    _log.warning("Detaching %s to clear the ATA security freeze", device.path)
    try:
        delete_path.write_text("1\n", encoding="ascii")
    except OSError as error:
        return False, f"could not detach {device.path}: {error}"

    time.sleep(_SETTLE_SECONDS)

    # Rescan every host rather than working out which one owned the device.
    # The device is gone by now, so its host is no longer discoverable from it,
    # and scanning a host that has nothing new on it is harmless.
    hosts = sorted(glob.glob("/sys/class/scsi_host/host*/scan"))
    if not hosts:
        return False, "no SCSI hosts to rescan; the drive may not come back without a reboot"

    for host in hosts:
        try:
            Path(host).write_text("- - -\n", encoding="ascii")
        except OSError as error:
            _log.info("Rescan of %s failed: %s", host, error)

    run(["udevadm", "settle"], timeout=30.0, log_output=False)
    time.sleep(_SETTLE_SECONDS)

    return True, f"detached and rescanned {len(hosts)} SCSI host(s)"


#: How long to suspend for. Three seconds, established on real hardware: long
#: enough to drop power to the drive and clear the freeze, short enough that
#: the USB bus does not tear down and take the live filesystem with it. A
#: twenty-second suspend cleared the freeze too - and destroyed the session.
_SUSPEND_SECONDS = 3


def suspend_to_ram() -> tuple[bool, str]:
    """Suspend briefly to power-cycle the drives. Returns ``(ok, detail)``.

    Switches ``/sys/power/mem_sleep`` to ``deep`` first. Without that, machines
    defaulting to ``s2idle`` (Modern Standby) suspend without ever removing
    power from SATA: the freeze survives and the operator has paid the
    interruption for nothing. That is the usual reason this trick gets reported
    as not working.
    """
    mem_sleep = Path("/sys/power/mem_sleep")
    if mem_sleep.exists():
        try:
            available = mem_sleep.read_text(encoding="ascii")
        except OSError:
            available = ""

        if "deep" not in available:
            return False, (
                "this firmware offers no S3 'deep' sleep state, only s2idle, "
                "which does not remove power from the drive"
            )

        if "[deep]" not in available:
            _log.info("Switching mem_sleep from %s to deep", available.strip())
            try:
                mem_sleep.write_text("deep\n", encoding="ascii")
            except OSError as error:
                return False, f"could not select deep sleep: {error}"

    _log.warning("Suspending for %ds to power-cycle the drives", _SUSPEND_SECONDS)
    result = run(["rtcwake", "-m", "mem", "-s", str(_SUSPEND_SECONDS)], timeout=180.0)
    if not result.ok:
        return False, f"suspend failed: {result.failure_summary}"

    time.sleep(_SETTLE_SECONDS)
    return True, f"suspended for {_SUSPEND_SECONDS}s"


def suspend_warning() -> str:
    """What to tell the operator before suspending, or an empty string.

    Running from the live medium is not a refusal - a three-second suspend has
    been shown to survive it - but it is worth stating plainly, because the
    failure mode is the whole session rather than just this drive.
    """
    medium = Path("/run/live/medium")
    if not medium.is_mount():
        return ""
    try:
        cmdline = Path("/proc/cmdline").read_text(encoding="ascii", errors="replace")
    except OSError:
        cmdline = ""
    if "toram" in cmdline.split():
        return ""
    return (
        "This session is running from the USB medium. Suspending re-enumerates "
        "the USB bus, and if the stick returns under a different name the live "
        "filesystem stops responding and the machine has to be rebooted. A "
        "short suspend usually survives it."
    )


def unfreeze(device: Device, *, allow_suspend: bool = False) -> tuple[bool, str]:
    """Try to clear the freeze on *device*. Returns ``(cleared, detail)``.

    Detaches the device and rescans the bus. When *allow_suspend* is set the
    caller has already put the question to the operator, and a brief S3 cycle
    is used instead; see the module docstring for why that is opt-in.

    The device may come back under a different name, so the caller should
    re-run discovery and match on serial number rather than reusing the path.
    """
    problem = can_attempt(device)
    if problem:
        return False, problem

    if allow_suspend:
        # The bus reset has already been tried and failed, so go straight to
        # the thing the operator has just agreed to.
        suspended, suspend_detail = suspend_to_ram()
        if not suspended:
            return False, suspend_detail

        state = _read_frozen_state(device.path)
        if state is False:
            _log.info("Freeze cleared on %s by a brief suspend", device.path)
            return True, f"{suspend_detail}; the freeze cleared"
        if state is None:
            return True, (
                f"{suspend_detail}; the drive did not answer at {device.path} "
                f"afterwards - rescan for drives"
            )
        return False, (
            f"{suspend_detail}, but the drive is still frozen. What remains: "
            f"the machine's own BIOS secure-erase utility, a whole-device "
            f"discard, or a software overwrite - the freeze affects none of them"
        )

    attempted, detail = detach_and_rescan(device)
    if not attempted:
        return False, detail

    state = _read_frozen_state(device.path)
    if state is False:
        _log.info("Freeze cleared on %s by detach and rescan", device.path)
        return True, f"{detail}; the freeze cleared"
    if state is None:
        return True, (
            f"{detail}; the drive did not answer at {device.path} afterwards - "
            f"rescan for drives, it may have come back under another name"
        )

    _log.info("Detach and rescan did not clear the freeze on %s", device.path)
    return False, (
        f"{detail}, but the drive is still frozen. This controller does not "
        f"reset the drive on re-attach."
    )


def clear_freezes(devices: list[Device]) -> list[str]:
    """Detach every eligible frozen drive, then rescan the bus once.

    Returns the paths that were detached, so the caller knows whether a
    re-discovery is worth doing.

    This is the automatic path, run at startup before the drive list is shown.
    It takes a few seconds and disturbs nothing the operator can see, which is
    what makes it reasonable to do without asking.

    All the deletes happen before the single rescan. Detaching four drives and
    rescanning four times would disturb the bus four times for no benefit.
    """
    candidates = [device for device in devices if not can_attempt(device)]
    if not candidates:
        return []

    detached: list[str] = []
    for device in candidates:
        delete_path = Path(f"/sys/block/{device.name}/device/delete")
        if not delete_path.exists():
            continue
        _log.warning(
            "Detaching %s at startup to clear its ATA security freeze", device.path
        )
        try:
            delete_path.write_text("1\n", encoding="ascii")
            detached.append(device.path)
        except OSError as error:
            _log.info("Could not detach %s: %s", device.path, error)

    if not detached:
        return []

    time.sleep(_SETTLE_SECONDS)

    hosts = sorted(glob.glob("/sys/class/scsi_host/host*/scan"))
    for host in hosts:
        try:
            Path(host).write_text("- - -\n", encoding="ascii")
        except OSError as error:
            _log.info("Rescan of %s failed: %s", host, error)

    run(["udevadm", "settle"], timeout=30.0, log_output=False)
    time.sleep(_SETTLE_SECONDS)

    _log.info(
        "Re-attached after detaching %d frozen drive(s): %s",
        len(detached),
        ", ".join(detached),
    )
    return detached
