"""The erase engine: schedules drives, enforces the interlocks, collects results.

The engine owns three responsibilities and delegates everything else:

**Refusing to do the wrong thing.** :func:`_preflight` is the last gate before
any command is issued. It re-checks the protection flag, re-checks that the
chosen method is one the drive said it supports, and unmounts filesystems if
the operator allowed that. This runs even though the UI has already checked -
the scan could be minutes old, a drive could have been hot-plugged, and the
consequence of getting it wrong is unrecoverable.

**Running drives concurrently.** Each drive gets a thread. Firmware commands
spend nearly all their time waiting on the controller, so running four drives
at once takes barely longer than one. Software overwrites contend for bus
bandwidth, so the concurrency limit is configurable and defaults low.

**Producing the record.** Every job, whatever its outcome, contributes an
:class:`~zeroize.models.EraseResult`; the engine assembles them into a
:class:`~zeroize.models.RunSummary` with the host and operator details the
certificate needs. A failed erase is still certified - as a failure. Quietly
dropping it would be the one outcome worse than the failure itself.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from ..config import Settings
from ..discovery import collect_hardware_info, collect_system_info, machine_identifier
from ..logging_setup import get_logger
from ..models import Device, EraseMethod, EraseResult, JobState, PassResult, RunSummary
from ..process import is_dry_run, run
from . import ata_ops, nvme_ops, overwrite
from .context import EraseOutcome, JobContext, ProgressUpdate
from .methods import (
    FAMILY_ATA_SANITIZE,
    FAMILY_ATA_SECURE_ERASE,
    FAMILY_DISCARD,
    FAMILY_NVME_FORMAT,
    FAMILY_NVME_SANITIZE,
    FAMILY_OVERWRITE,
    FAMILY_SCSI_SANITIZE,
    availability_for,
)
from .verify import verify_firmware_erase

_log = get_logger("Engine")

#: Which implementation handles each method family.
_IMPLEMENTATIONS: dict[str, Callable[[JobContext], EraseOutcome]] = {
    FAMILY_NVME_FORMAT: nvme_ops.run_format,
    FAMILY_NVME_SANITIZE: nvme_ops.run_sanitize,
    FAMILY_ATA_SECURE_ERASE: ata_ops.run_secure_erase,
    FAMILY_SCSI_SANITIZE: ata_ops.run_scsi_sanitize,
    FAMILY_ATA_SANITIZE: ata_ops.run_ata_sanitize,
    FAMILY_DISCARD: ata_ops.run_discard,
    FAMILY_OVERWRITE: overwrite.run_overwrite,
}

#: Families that verify their own work and must not be verified again.
_SELF_VERIFYING = frozenset({FAMILY_OVERWRITE})


class EraseRun:
    """A batch of drive erases, running together.

    Construct one, call :meth:`start`, and watch it through the progress
    callback. :meth:`wait` blocks until every job is terminal and returns the
    :class:`~zeroize.models.RunSummary` the certificate is built from.
    """

    def __init__(
        self,
        assignments: Iterable[tuple[Device, EraseMethod]],
        settings: Settings,
        *,
        operator: str = "",
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> None:
        self._assignments = list(assignments)
        self._settings = settings
        self._operator = operator or settings.organisation.operator
        self._on_progress = on_progress
        self._cancel_event = threading.Event()
        self._results: dict[str, EraseResult] = {}
        self._results_lock = threading.Lock()
        self._futures: list[Future] = []
        self._executor: ThreadPoolExecutor | None = None
        self._started_at: datetime | None = None

    # ----------------------------------------------------------------- run

    def start(self) -> None:
        """Begin every job, up to the configured concurrency limit."""
        if self._executor is not None:
            raise RuntimeError("this run has already been started")

        self._started_at = datetime.now().astimezone()
        workers = min(self._settings.erase.max_concurrent_jobs, max(1, len(self._assignments)))
        _log.info(
            "Starting %d erase job(s) with a concurrency of %d%s",
            len(self._assignments),
            workers,
            " (DRY RUN)" if is_dry_run() else "",
        )

        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="erase")
        for device, method in self._assignments:
            self._futures.append(self._executor.submit(self._run_one, device, method))

    def cancel(self) -> None:
        """Request cancellation of every job that can honour it.

        Software overwrites stop between blocks. Firmware commands continue -
        they are not interruptible, and the drive would be left unusable. The
        UI is expected to say so rather than implying everything stopped.
        """
        _log.warning("Cancellation requested for the whole run")
        self._cancel_event.set()

    def wait(self) -> RunSummary:
        """Block until every job is terminal, then return the run summary."""
        for future in self._futures:
            future.result()
        if self._executor is not None:
            self._executor.shutdown(wait=True)

        finished_at = datetime.now().astimezone()
        started_at = self._started_at or finished_at

        # Preserve the order the operator selected the drives in, so the
        # certificate reads the same way as the screen they confirmed.
        ordered = [
            self._results[device.path]
            for device, _ in self._assignments
            if device.path in self._results
        ]

        summary = RunSummary(
            results=ordered,
            started_at=started_at,
            finished_at=finished_at,
            operator=self._operator,
            machine=machine_identifier(),
            system_info=collect_system_info(),
            hardware_info=collect_hardware_info(),
        )
        _log.info(
            "Run finished in %.0fs: %d succeeded, %d failed",
            summary.duration_seconds,
            sum(1 for result in ordered if result.succeeded),
            sum(1 for result in ordered if not result.succeeded),
        )
        return summary

    @property
    def is_running(self) -> bool:
        return any(not future.done() for future in self._futures)

    # ------------------------------------------------------------- one job

    def _run_one(self, device: Device, method: EraseMethod) -> None:
        """Run a single drive's erase and record the result, whatever happens."""
        started_at = datetime.now().astimezone()
        context = JobContext(
            device=device,
            method=method,
            settings=self._settings,
            started_at=started_at,
            cancel_event=self._cancel_event,
            on_progress=self._on_progress,
        )

        try:
            outcome = self._execute(context)
        except Exception as error:  # noqa: BLE001 - a crash must still be certified
            _log.exception("Unhandled error erasing %s", device.path)
            outcome = EraseOutcome.failure(f"internal error: {error}")

        finished_at = datetime.now().astimezone()

        if outcome.succeeded:
            state = JobState.SUCCEEDED
        elif self._cancel_event.is_set() and not outcome.passes:
            state = JobState.CANCELLED
        elif any("cancelled" in message.lower() for message in outcome.errors):
            state = JobState.CANCELLED
        else:
            state = JobState.FAILED

        result = EraseResult(
            device=device,
            method=method,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
            passes=outcome.passes,
            verification_percent=outcome.verification_percent,
            verification_passed=outcome.verification_passed,
            error_messages=outcome.errors,
            commands=context.commands,
            uninterrupted=not self._cancel_event.is_set(),
            bytes_written=outcome.bytes_written,
        )

        with self._results_lock:
            self._results[device.path] = result

        context.report(
            result.result_word if outcome.succeeded else (outcome.errors[0] if outcome.errors else "Failed"),
            state=state,
            fraction=1.0 if outcome.succeeded else None,
        )

    def _execute(self, context: JobContext) -> EraseOutcome:
        """Preflight, erase, then verify - the three phases of one job."""
        problem = self._preflight(context)
        if problem:
            _log.error("Refusing to erase %s: %s", context.device.path, problem)
            return EraseOutcome.failure(problem)

        implementation = _IMPLEMENTATIONS.get(context.method.family)
        if implementation is None:
            return EraseOutcome.failure(f"no implementation for method family {context.method.family}")

        outcome = implementation(context)

        if outcome.succeeded:
            _refresh_kernel_view(context)

        needs_verification = (
            outcome.succeeded
            and context.method.supports_verification
            and context.method.family not in _SELF_VERIFYING
            and context.settings.erase.verification_percent > 0
        )
        if needs_verification:
            # Three outcomes, not two. None means the check could not run, and
            # it must not be recorded as either a pass or a failed erase: the
            # firmware reported success, nothing contradicted it, and no
            # sampling happened. The certificate prints "Not performed" for it.
            verdict, detail, percent = verify_firmware_erase(context)
            outcome.verification_passed = verdict
            outcome.verification_percent = percent
            outcome.passes.append(
                PassResult(
                    label="Verification",
                    succeeded=verdict is not False,
                    detail=detail,
                )
            )
            if verdict is False:
                outcome.succeeded = False
                outcome.errors.append(f"verification failed: {detail}")

        return outcome

    # ----------------------------------------------------------- interlocks

    def _preflight(self, context: JobContext) -> str:
        """Last gate before a command is issued. Empty string means proceed."""
        device = context.device
        method = context.method

        if self._cancel_event.is_set():
            return "cancelled before the job started"

        # 1. The protection flag. Checked again here because the scan that set
        #    it may be minutes old.
        if device.is_system:
            return device.protection_reason or "the device carries the running system"
        if device.read_only:
            return "the device is read-only"

        # 2. The method must be one this drive actually said it supports.
        verdict = next(
            (item for item in availability_for(device) if item.method.key == method.key),
            None,
        )
        if verdict is None:
            return f"{method.key} is not a known method"
        if not verdict.supported:
            return f"{method.certificate_name} is not available on this drive: {verdict.reason}"

        # 3. Mounted filesystems. The kernel would refuse the exclusive open
        #    anyway for an overwrite, but a firmware erase would succeed and
        #    leave the system with a live mount over erased media.
        if device.is_mounted:
            if not context.settings.safety.allow_mounted_devices:
                return "the device has mounted filesystems; unmount them or enable allow_mounted_devices"
            unmount_problem = self._unmount_all(context)
            if unmount_problem:
                return unmount_problem

        return ""

    def _unmount_all(self, context: JobContext) -> str:
        """Unmount every filesystem on the device. Empty string means success."""
        device = context.device

        def collect(partitions) -> list[str]:
            found: list[str] = []
            for partition in partitions:
                if partition.mountpoint:
                    found.append(partition.path)
                found.extend(collect(partition.children))
            return found

        # The device itself may be mounted, with no partitions at all - a disk
        # formatted directly rather than partitioned. Unmounting only the
        # partitions would leave a live filesystem over media about to be
        # erased.
        targets = ([device.path] if device.mountpoints else []) + collect(device.partitions)

        for path in targets:
            context.report(f"Unmounting {path}", fraction=None)
            argv = ["umount", path]
            context.record_command(argv)
            result = run(argv, timeout=60.0)
            if not result.ok and not result.skipped:
                return f"could not unmount {path}: {result.failure_summary}"
            _log.info("Unmounted %s", path)

        return ""


def _refresh_kernel_view(context: JobContext) -> None:
    """Make the kernel and udev forget the drive that was just erased.

    Delegates to the discovery helper rather than repeating it. Three caches
    hold this - the kernel partition table, blkid's cache, and udev's property
    database that lsblk actually reads - and clearing only one leaves an erased
    drive still listing its old filesystem. Getting that wrong in two places
    independently is how it stayed broken after the first attempt to fix it.

    Runs after the erase has already succeeded, so failures are logged and
    ignored: a cosmetically stale listing is not a reason to fail a job that
    did its work.
    """
    from ..discovery import refresh_stale_caches

    for argv in (["blockdev", "--rereadpt", context.device.path],):
        context.record_command(argv)

    refresh_stale_caches([context.device])
    _log.info("Asked the kernel to re-read %s after the erase", context.device.path)


def erase_devices(
    assignments: Iterable[tuple[Device, EraseMethod]],
    settings: Settings,
    *,
    operator: str = "",
    on_progress: Callable[[ProgressUpdate], None] | None = None,
) -> RunSummary:
    """Run a batch of erases to completion and return the summary.

    The blocking convenience wrapper used by the CLI. The GTK interface drives
    :class:`EraseRun` directly so it can keep its main loop responsive.
    """
    run_handle = EraseRun(assignments, settings, operator=operator, on_progress=on_progress)
    run_handle.start()
    return run_handle.wait()
