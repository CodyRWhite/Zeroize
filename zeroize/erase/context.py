"""The contract between the erase engine and each erase implementation.

Every method family - NVMe format, NVMe sanitize, ATA secure erase, SCSI
sanitize, software overwrite - is a function taking a :class:`JobContext` and
returning an :class:`EraseOutcome`. Keeping that interface narrow means the
engine does not need to know how any of them work, and a new method family can
be added without touching the scheduler, the UI or the certificate.

Progress is reported by calling :meth:`JobContext.report`. Some methods can
give an exact fraction (a software overwrite knows its byte count; an NVMe
sanitize has a progress register), and some genuinely cannot (``nvme format``
returns only when it is finished). The latter pass ``fraction=None``, which the
UI renders as an indeterminate bar with elapsed time rather than inventing a
number.

Cancellation is cooperative and, deliberately, not universally honoured.
:meth:`JobContext.cancelled` is checked between passes of a software overwrite,
where stopping leaves the drive in a merely-partly-overwritten state that is no
worse than where it started. Firmware commands - sanitize, secure erase - are
not interruptible by design: the drive is unusable until they complete, so
"cancel" there means "stop waiting and report", never "stop erasing".
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from ..config import Settings
from ..models import Device, EraseMethod, JobState, PassResult


@dataclass(slots=True)
class ProgressUpdate:
    """One progress report from a running job.

    ``fraction`` is ``None`` for genuinely indeterminate work. ``eta_seconds``
    is only set when it can be estimated honestly; a guess dressed up as an ETA
    is worse than no ETA on a job that runs for hours.
    """

    device_path: str
    state: JobState
    message: str
    fraction: float | None = None
    pass_index: int = 0
    pass_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    elapsed_seconds: float = 0.0
    eta_seconds: float | None = None

    @property
    def percent(self) -> float | None:
        return None if self.fraction is None else round(self.fraction * 100, 1)


#: Signature of the callback the engine hands to each job.
ProgressCallback = Callable[[ProgressUpdate], None]


@dataclass(slots=True)
class JobContext:
    """Everything one erase implementation needs, and nothing more."""

    device: Device
    method: EraseMethod
    settings: Settings
    started_at: datetime
    cancel_event: threading.Event = field(default_factory=threading.Event)
    on_progress: ProgressCallback | None = None
    #: Commands issued so far, accumulated for the certificate appendix.
    commands: list[str] = field(default_factory=list)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def report(
        self,
        message: str,
        *,
        state: JobState = JobState.RUNNING,
        fraction: float | None = None,
        pass_index: int = 0,
        pass_total: int = 0,
        bytes_done: int = 0,
        bytes_total: int = 0,
        elapsed_seconds: float = 0.0,
        eta_seconds: float | None = None,
    ) -> None:
        """Emit a progress update, if anyone is listening."""
        if self.on_progress is None:
            return
        self.on_progress(
            ProgressUpdate(
                device_path=self.device.path,
                state=state,
                message=message,
                fraction=fraction,
                pass_index=pass_index,
                pass_total=pass_total,
                bytes_done=bytes_done,
                bytes_total=bytes_total,
                elapsed_seconds=elapsed_seconds,
                eta_seconds=eta_seconds,
            )
        )

    def record_command(self, argv: list[str]) -> None:
        """Note a command in the certificate's audit appendix."""
        self.commands.append(" ".join(argv))


@dataclass(slots=True)
class EraseOutcome:
    """What one erase implementation reports back to the engine."""

    succeeded: bool
    passes: list[PassResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    bytes_written: int = 0
    #: Set by implementations that verified their own work, so the engine does
    #: not run a redundant verification pass over the same device.
    verification_percent: float = 0.0
    verification_passed: bool | None = None

    @classmethod
    def failure(cls, message: str, *, passes: list[PassResult] | None = None) -> "EraseOutcome":
        return cls(succeeded=False, errors=[message], passes=passes or [])

    @classmethod
    def success(cls, passes: list[PassResult] | None = None, **kwargs) -> "EraseOutcome":
        return cls(succeeded=True, passes=passes or [], **kwargs)
