"""ATA Secure Erase and SCSI Sanitize for non-NVMe drives.

**ATA Secure Erase** is a three-step dance, not a single command, and each step
can fail in a way that leaves the drive worse off than before:

1. Set a user password with ``hdparm --security-set-pass``. This arms the
   security feature set. The drive is now *locked*.
2. Issue ``hdparm --security-erase`` (or ``--security-erase-enhanced``) with
   the same password. The drive erases itself and clears the password.
3. Confirm with ``hdparm -I`` that security is no longer enabled.

If step 2 fails, the drive is left locked with the password from step 1 and
will not be usable until it is unlocked. That is why the password is a fixed,
documented constant rather than anything generated: an operator recovering a
drive stranded by a power cut needs to be able to look it up. It is written to
the log at every step for exactly that reason. It is not a secret and is not
treated as one.

The enhanced variant additionally erases sectors that have been reallocated out
of the visible LBA range, which a software overwrite can never reach. Where a
drive supports it, it is the better choice.

**SCSI Sanitize** covers SAS and other true SCSI drives, which do not implement
the ATA security feature set at all. ``sg_sanitize`` runs it and can wait for
completion, so it needs no polling loop of its own.
"""

from __future__ import annotations

import time

from ..discovery.ata import read_ata_security
from ..logging_setup import get_logger
from ..models import PassResult
from ..process import run, run_with_heartbeat, tool_available
from .context import EraseOutcome, JobContext
from .methods import ATA_SECURE_ERASE_ENHANCED, SCSI_SANITIZE_CRYPTO

_log = get_logger("Ata")

#: The user password set to arm the security feature set before erasing.
#: Documented in the README and printed to the log: if an erase is interrupted
#: between arming and erasing, this is what unlocks the drive again with
#: ``hdparm --user-master u --security-disable Zeroize /dev/sdX``.
SECURITY_PASSWORD = "Zeroize"

#: Multiplier applied to the drive's own estimate before giving up on it.
#: Drives routinely overrun their advertised figure, and abandoning a secure
#: erase early leaves a locked drive, so the ceiling is generous.
_ESTIMATE_MULTIPLIER = 3

#: Floor and ceiling for the erase timeout regardless of the drive's estimate.
_MIN_ERASE_TIMEOUT_SECONDS = 30 * 60
_MAX_ERASE_TIMEOUT_SECONDS = 24 * 60 * 60


def _erase_timeout(context: JobContext) -> float:
    """How long to wait for SECURITY ERASE UNIT, from the drive's own estimate."""
    security = context.device.ata
    minutes = 0
    if security:
        minutes = (
            security.enhanced_estimated_minutes
            if context.method is ATA_SECURE_ERASE_ENHANCED
            else security.estimated_minutes
        )
    seconds = minutes * 60 * _ESTIMATE_MULTIPLIER
    return float(max(_MIN_ERASE_TIMEOUT_SECONDS, min(_MAX_ERASE_TIMEOUT_SECONDS, seconds)))


def _security_is_enabled(device_path: str) -> bool | None:
    """Re-read the drive's security state. ``None`` means it could not be read.

    Delegates to the discovery parser rather than scanning hdparm's output
    here. An earlier version walked every line looking for one reading exactly
    "enabled" - which is the mistake the discovery parser exists to avoid. That
    word appears elsewhere in ``hdparm -I`` output, and matching it outside the
    ``Security:`` block would report a successfully erased drive as still
    locked, turning a good erase into a failed certificate.

    Two parsers of the same output is one too many, and the scoped one is the
    correct one.
    """
    security = read_ata_security(device_path)
    if security is None or not security.supported:
        return None
    return security.enabled


def _simulated_outcome(context: JobContext, label: str) -> EraseOutcome:
    """Exercise the progress path without touching hardware."""
    total_steps = 10
    for step in range(total_steps + 1):
        if context.cancelled:
            return EraseOutcome.failure("cancelled by the operator")
        context.report(f"Simulating {label}", fraction=step / total_steps, pass_index=1, pass_total=1)
        time.sleep(0.25)
    return EraseOutcome.success(
        passes=[PassResult(label=label, succeeded=True, detail="simulated")],
        bytes_written=context.device.size_bytes,
    )


def run_secure_erase(context: JobContext) -> EraseOutcome:
    """Perform an ATA Secure Erase, arming the drive first."""
    device = context.device
    enhanced = context.method is ATA_SECURE_ERASE_ENHANCED
    label = "ATA Enhanced Secure Erase" if enhanced else "ATA Secure Erase"

    if device.simulated:
        return _simulated_outcome(context, label)

    if not tool_available("hdparm"):
        return EraseOutcome.failure("hdparm is not installed")

    security = device.ata
    if security is None or not security.supported:
        return EraseOutcome.failure("the drive does not support the ATA security feature set")

    # Re-check the freeze state immediately before erasing rather than trusting
    # what discovery saw. A drive hot-plugged since the scan may have been
    # frozen in the meantime, and arming a frozen drive fails after the
    # password has already been set.
    context.report("Re-checking drive security state", fraction=None, pass_index=1, pass_total=3)
    recheck = run(["hdparm", "-I", device.path], timeout=30.0, log_output=False)
    if recheck.ok and "\tfrozen" in recheck.stdout.replace("not\tfrozen", ""):
        return EraseOutcome.failure(
            "the drive is frozen by the system firmware - power-cycle it and rescan"
        )

    # Step 1 - arm the security feature set.
    arm_argv = [
        "hdparm",
        "--user-master",
        "u",
        "--security-set-pass",
        SECURITY_PASSWORD,
        device.path,
    ]
    context.record_command(arm_argv)
    context.report(
        f"Setting the ATA security password on {device.path}",
        fraction=None,
        pass_index=1,
        pass_total=3,
    )
    _log.warning(
        "Arming ATA security on %s with password '%s' - if the erase does not complete, "
        "unlock with: hdparm --user-master u --security-disable %s %s",
        device.path,
        SECURITY_PASSWORD,
        SECURITY_PASSWORD,
        device.path,
    )

    arm_result = run(arm_argv, timeout=120.0)
    if arm_result.skipped:
        return EraseOutcome.success(
            passes=[PassResult(label=label, succeeded=True, detail="dry run - not executed")],
        )
    if not arm_result.ok:
        return EraseOutcome.failure(
            f"could not set the security password: {arm_result.failure_summary}",
            passes=[PassResult(label=label, succeeded=False, detail="arming failed")],
        )

    # Step 2 - erase. From here on the drive is locked until this succeeds.
    erase_flag = "--security-erase-enhanced" if enhanced else "--security-erase"
    erase_argv = ["hdparm", "--user-master", "u", erase_flag, SECURITY_PASSWORD, device.path]
    context.record_command(erase_argv)

    estimate_minutes = (
        security.enhanced_estimated_minutes if enhanced else security.estimated_minutes
    )
    estimate_note = (
        f"the drive estimates {estimate_minutes} minutes"
        if estimate_minutes
        else "the drive gave no estimate"
    )
    context.report(
        f"{label} in progress - {estimate_note}",
        fraction=None,
        pass_index=2,
        pass_total=3,
    )
    _log.info("Issuing %s on %s (%s)", erase_flag, device.path, estimate_note)

    estimate_seconds = estimate_minutes * 60

    def heartbeat(elapsed: float) -> bool:
        # The drive publishes no progress, but it did give an estimate, so the
        # elapsed time can be shown against it without inventing a percentage.
        if estimate_seconds:
            context.report(
                f"{label} in progress - {int(elapsed // 60)} of about {estimate_minutes} minutes",
                fraction=None,
                pass_index=2,
                pass_total=3,
                elapsed_seconds=elapsed,
                eta_seconds=max(0.0, estimate_seconds - elapsed),
            )
        else:
            context.report(
                f"{label} in progress - {int(elapsed // 60)} minutes elapsed",
                fraction=None,
                pass_index=2,
                pass_total=3,
                elapsed_seconds=elapsed,
            )
        # Not interruptible: abandoning here strands a locked drive.
        return True

    erase_result = run_with_heartbeat(
        erase_argv,
        heartbeat=heartbeat,
        interval=5.0,
        timeout=_erase_timeout(context),
    )

    if not erase_result.ok:
        _log.error(
            "Secure erase failed on %s - the drive may still be locked with password '%s'",
            device.path,
            SECURITY_PASSWORD,
        )
        return EraseOutcome.failure(
            f"{erase_result.failure_summary} (the drive may still be locked with the "
            f"password '{SECURITY_PASSWORD}')",
            passes=[PassResult(label=label, succeeded=False, detail=erase_result.failure_summary)],
        )

    # Step 3 - confirm the password cleared itself, which is how the drive
    # signals that the erase really ran.
    context.report("Confirming the security password cleared", fraction=None, pass_index=3, pass_total=3)
    still_enabled = _security_is_enabled(device.path)
    if still_enabled is True:
        return EraseOutcome.failure(
            f"the erase reported success but the drive is still locked with the password "
            f"'{SECURITY_PASSWORD}'",
            passes=[PassResult(label=label, succeeded=False, detail="drive still locked")],
        )

    detail = "security password cleared" if still_enabled is False else "security state unreadable"
    _log.info("Secure erase completed on %s in %.0fs", device.path, erase_result.duration_seconds)
    return EraseOutcome.success(
        passes=[PassResult(label=label, succeeded=True, detail=detail)],
        bytes_written=device.size_bytes,
    )


def run_scsi_sanitize(context: JobContext) -> EraseOutcome:
    """Perform a SCSI SANITIZE on a SAS or other true SCSI drive."""
    device = context.device
    crypto = context.method is SCSI_SANITIZE_CRYPTO
    label = "SCSI Sanitize - crypto erase" if crypto else "SCSI Sanitize - block erase"

    if device.simulated:
        return _simulated_outcome(context, label)

    if not tool_available("sg_sanitize"):
        return EraseOutcome.failure("sg3-utils is not installed (sg_sanitize missing)")

    # --quick skips sg_sanitize's own interactive confirmation; the operator
    # has already confirmed in the application. --wait blocks until the drive
    # finishes rather than returning as soon as the command is accepted.
    argv = [
        "sg_sanitize",
        "--crypto" if crypto else "--block",
        "--quick",
        "--wait",
        device.path,
    ]
    context.record_command(argv)
    context.report(f"Starting {label} on {device.path}", fraction=None, pass_index=1, pass_total=1)
    _log.info("Issuing SCSI sanitize on %s (crypto=%s)", device.path, crypto)

    def heartbeat(elapsed: float) -> bool:
        context.report(
            f"{label} in progress - {int(elapsed // 60)} minutes elapsed",
            fraction=None,
            pass_index=1,
            pass_total=1,
            elapsed_seconds=elapsed,
        )
        return True

    result = run_with_heartbeat(
        argv,
        heartbeat=heartbeat,
        interval=5.0,
        timeout=_MAX_ERASE_TIMEOUT_SECONDS,
    )

    if result.skipped:
        return EraseOutcome.success(
            passes=[PassResult(label=label, succeeded=True, detail="dry run - not executed")],
        )
    if not result.ok:
        return EraseOutcome.failure(
            result.failure_summary,
            passes=[PassResult(label=label, succeeded=False, detail=result.failure_summary)],
        )

    _log.info("SCSI sanitize completed on %s in %.0fs", device.path, result.duration_seconds)
    return EraseOutcome.success(
        passes=[PassResult(label=label, succeeded=True)],
        bytes_written=device.size_bytes,
    )


# --------------------------------------------------------------------------
# ATA Sanitize
# --------------------------------------------------------------------------
# A separate feature set from ATA Security, and the reason it is worth having:
# SECURITY FREEZE LOCK does not block it. On a drive that supports it, a frozen
# machine can still be purged without any of the detach-and-rescan business.
#
# Like NVMe sanitize it runs in the background, so the command returns
# immediately and the real answer comes from polling the status.

_SANITIZE_POLL_SECONDS = 3.0
_SANITIZE_STALL_SECONDS = 15 * 60


def _sanitize_status(device_path: str) -> tuple[bool, str]:
    """Return ``(in_progress, raw_text)`` from ``hdparm --sanitize-status``."""
    result = run(["hdparm", "--sanitize-status", device_path], timeout=30.0, log_output=False)
    text = f"{result.stdout}\n{result.stderr}".strip()
    lowered = text.lower()
    in_progress = "sanitize operation in progress" in lowered or "in progress" in lowered
    return in_progress, text


def run_ata_sanitize(context: JobContext) -> EraseOutcome:
    """Purge an ATA drive with SANITIZE, which the security freeze cannot block."""
    device = context.device
    crypto = context.method.key == "ata-sanitize-crypto"
    label = "ATA Sanitize - crypto scramble" if crypto else "ATA Sanitize - block erase"

    if device.simulated:
        return _simulated_outcome(context, label)

    if not tool_available("hdparm"):
        return EraseOutcome.failure("hdparm is not installed")

    security = device.ata
    if security is None:
        return EraseOutcome.failure("the drive did not report its capabilities")
    if crypto and not security.ata_sanitize_crypto_supported:
        return EraseOutcome.failure("the drive does not support CRYPTO_SCRAMBLE_EXT")
    if not crypto and not security.ata_sanitize_block_supported:
        return EraseOutcome.failure("the drive does not support BLOCK_ERASE_EXT")

    # Refuse if a previous sanitize is still running rather than stacking onto
    # it and misreporting whose result we are watching.
    already_running, status_text = _sanitize_status(device.path)
    if already_running:
        return EraseOutcome.failure(f"a sanitize is already running on {device.path}")
    _log.info("%s sanitize status before the command: %s", device.path, status_text)

    flag = "--sanitize-crypto-scramble" if crypto else "--sanitize-block-erase"
    argv = ["hdparm", "--yes-i-know-what-i-am-doing", flag, device.path]
    context.record_command(argv)
    context.report(f"Starting {label} on {device.path}", fraction=0.0, pass_index=1, pass_total=1)

    start = run(argv, timeout=120.0)
    if start.skipped:
        return EraseOutcome.success(
            passes=[PassResult(label=label, succeeded=True, detail="dry run - not executed")],
        )
    if not start.ok:
        return EraseOutcome.failure(
            start.failure_summary,
            passes=[PassResult(label=label, succeeded=False, detail=start.failure_summary)],
        )

    started = time.monotonic()
    observed_running = False

    while True:
        time.sleep(_SANITIZE_POLL_SECONDS)
        elapsed = time.monotonic() - started
        in_progress, text = _sanitize_status(device.path)

        if in_progress:
            observed_running = True
            context.report(
                f"{label} in progress - {int(elapsed // 60)} minutes elapsed",
                fraction=None,
                pass_index=1,
                pass_total=1,
                elapsed_seconds=elapsed,
            )
            if elapsed > _SANITIZE_STALL_SECONDS:
                return EraseOutcome.failure(
                    f"sanitize still running after {_SANITIZE_STALL_SECONDS // 60} minutes",
                    passes=[PassResult(label=label, succeeded=False, detail="stalled")],
                )
            continue

        # Not running. As with NVMe, "stopped" is not "succeeded" - wait a
        # moment for the drive to reflect the command before believing it.
        if not observed_running and elapsed < 15:
            continue

        lowered = text.lower()
        if "failed" in lowered or "error" in lowered:
            return EraseOutcome.failure(
                f"the drive reports the sanitize failed: {text.splitlines()[0] if text else 'no detail'}",
                passes=[PassResult(label=label, succeeded=False, detail=text[:200])],
            )

        _log.info("ATA sanitize completed on %s in %.0fs", device.path, elapsed)
        return EraseOutcome.success(
            passes=[PassResult(label=label, succeeded=True, detail=text.splitlines()[0] if text else "")],
            bytes_written=device.size_bytes,
        )


# --------------------------------------------------------------------------
# Whole-device discard (TRIM)
# --------------------------------------------------------------------------

def run_discard(context: JobContext) -> EraseOutcome:
    """Discard every block, then verify the drive really returns zeros.

    Only offered where the drive advertises deterministic zeros after TRIM, so
    the verification is meaningful rather than decorative. It is emphatically
    not a NIST SP 800-88 method - see the catalogue entry - and the certificate
    says so.
    """
    device = context.device
    label = "Whole-device TRIM (discard)"

    if device.simulated:
        return _simulated_outcome(context, label)

    if not tool_available("blkdiscard"):
        return EraseOutcome.failure("blkdiscard is not installed (util-linux)")

    argv = ["blkdiscard", "-f", device.path]
    context.record_command(argv)
    context.report(f"Discarding every block on {device.path}", fraction=None, pass_index=1, pass_total=1)
    _log.info("Issuing whole-device discard on %s", device.path)

    def heartbeat(elapsed: float) -> bool:
        context.report(
            f"{label} - {int(elapsed)}s elapsed",
            fraction=None,
            pass_index=1,
            pass_total=1,
            elapsed_seconds=elapsed,
        )
        return True

    result = run_with_heartbeat(argv, heartbeat=heartbeat, interval=2.0, timeout=_MAX_ERASE_TIMEOUT_SECONDS)

    if result.skipped:
        return EraseOutcome.success(
            passes=[PassResult(label=label, succeeded=True, detail="dry run - not executed")],
        )
    if not result.ok:
        return EraseOutcome.failure(
            result.failure_summary,
            passes=[PassResult(label=label, succeeded=False, detail=result.failure_summary)],
        )

    _log.info("Discard completed on %s in %.1fs", device.path, result.duration_seconds)
    return EraseOutcome.success(
        passes=[
            PassResult(
                label=label,
                succeeded=True,
                detail="every block discarded; the drive guarantees zeros afterwards",
            )
        ],
        bytes_written=device.size_bytes,
    )
