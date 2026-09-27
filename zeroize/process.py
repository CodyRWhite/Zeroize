"""The single choke point for running external commands.

Every ``nvme``, ``hdparm``, ``lsblk``, ``sg_sanitize`` and ``dmidecode`` call in
the application goes through :func:`run`. Centralising it buys three things
that matter for a tool that destroys data:

* **An audit trail.** The full argument vector is logged *before* the command
  runs, and the exit status and stderr are logged after. A disputed erase can
  be reconstructed from the log without trusting the certificate.
* **A single dry-run switch.** :func:`set_dry_run` makes every mutating command
  a no-op that still logs what it would have done, which is how the simulated
  device backend and the test suite exercise the real code paths.
* **Consistent failure handling.** Callers get a :class:`CommandResult` and
  decide; only :func:`run_checked` raises. Discovery code in particular must
  tolerate a missing or unhappy tool rather than aborting the whole scan.

Commands are always passed as a list and never through a shell, so a device
path or serial number containing shell metacharacters cannot be re-interpreted.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from .logging_setup import get_logger

_log = get_logger("Process")

_dry_run = False

#: Commands that only read state. These still run under ``--dry-run``, because
#: a dry run that cannot enumerate drives is useless for rehearsing a job.
_READ_ONLY_COMMANDS = frozenset(
    {
        "lsblk",
        "blkid",
        "dmidecode",
        "smartctl",
        "sg_inq",
        "sg_readcap",
        "findmnt",
        "udevadm",
        "lscpu",
        "uname",
    }
)

#: Sub-commands of ``nvme`` / ``hdparm`` that only read state.
_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "list",
        "id-ctrl",
        "id-ns",
        "list-ns",
        "sanitize-log",
        "smart-log",
        "show-regs",
        "list-subsys",
    }
)


class CommandError(RuntimeError):
    """Raised by :func:`run_checked` when a command fails or is missing."""

    def __init__(self, result: "CommandResult") -> None:
        self.result = result
        super().__init__(result.failure_summary)


@dataclass(slots=True)
class CommandResult:
    """Outcome of one external command."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    not_found: bool = False
    #: True when ``--dry-run`` suppressed a mutating command.
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.not_found

    @property
    def display(self) -> str:
        """The command as a copy-pasteable string, for logs and certificates."""
        return " ".join(self.argv)

    @property
    def failure_summary(self) -> str:
        """One line explaining what went wrong, suitable for the UI."""
        if self.not_found:
            return f"{self.argv[0]} is not installed"
        if self.timed_out:
            return f"{self.display} timed out after {self.duration_seconds:.0f}s"
        detail = (self.stderr or self.stdout or "").strip().splitlines()
        first_line = detail[0] if detail else "no output"
        return f"{self.display} exited {self.returncode}: {first_line}"


def set_dry_run(enabled: bool) -> None:
    """Turn the global dry-run guard on or off.

    Under dry run, read-only commands still execute so the UI shows real
    hardware, while anything that would change a drive is logged and skipped.
    """
    global _dry_run
    _dry_run = enabled
    _log.info("Dry-run mode %s", "enabled" if enabled else "disabled")


def is_dry_run() -> bool:
    return _dry_run


def tool_available(name: str) -> bool:
    """True when *name* is on ``PATH``."""
    return shutil.which(name) is not None


def _is_read_only(argv: list[str]) -> bool:
    """Classify a command so dry run knows whether to suppress it."""
    program = argv[0].rsplit("/", 1)[-1]
    if program in _READ_ONLY_COMMANDS:
        return True
    if program in ("nvme", "hdparm") and len(argv) > 1:
        # hdparm's read-only invocation is `hdparm -I <dev>`; anything with a
        # --security-* or --trim flag mutates.
        if program == "hdparm":
            return all(not arg.startswith("--security") and arg != "--trim-sector-ranges" for arg in argv[1:])
        return argv[1] in _READ_ONLY_SUBCOMMANDS
    return False


def run(
    argv: list[str],
    *,
    timeout: float | None = 120.0,
    input_text: str | None = None,
    check: bool = False,
    log_output: bool = True,
) -> CommandResult:
    """Run *argv* and return a :class:`CommandResult`.

    Never raises for a non-zero exit unless *check* is set; a missing binary and
    a timeout are likewise reported in the result rather than thrown, so
    discovery can degrade gracefully when an optional tool is absent.
    """
    if _dry_run and not _is_read_only(argv):
        _log.warning("DRY RUN - would execute: %s", " ".join(argv))
        return CommandResult(
            argv=list(argv),
            returncode=0,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            skipped=True,
        )

    _log.info("Executing: %s", " ".join(argv))
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never shell=True
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            # Without this a child with nothing to read inherits OUR stdin.
            # nvme-cli asks "Type 'YES' to proceed:" before a destructive
            # command, and hdparm has prompts of its own. Inheriting a stdin
            # nobody is driving means the prompt blocks until the timeout -
            # six hours for a format - while the heartbeat reports the erase
            # as "in progress". A closed stdin turns that silent hang into an
            # immediate, readable failure.
            stdin=None if input_text is not None else subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        elapsed = time.monotonic() - started
        _log.error("Command not found: %s", argv[0])
        result = CommandResult(
            argv=list(argv),
            returncode=127,
            stdout="",
            stderr=f"{argv[0]}: not found",
            duration_seconds=elapsed,
            not_found=True,
        )
    except subprocess.TimeoutExpired as expired:
        elapsed = time.monotonic() - started
        _log.error("Command timed out after %.0fs: %s", elapsed, " ".join(argv))
        result = CommandResult(
            argv=list(argv),
            returncode=124,
            stdout=_as_text(expired.stdout),
            stderr=_as_text(expired.stderr),
            duration_seconds=elapsed,
            timed_out=True,
        )
    else:
        elapsed = time.monotonic() - started
        result = CommandResult(
            argv=list(argv),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_seconds=elapsed,
        )
        if result.ok:
            _log.debug("Completed in %.2fs: %s", elapsed, " ".join(argv))
        else:
            _log.warning("Exit %d after %.2fs: %s", result.returncode, elapsed, " ".join(argv))

    if log_output:
        _log_streams(result)
    if check and not result.ok:
        raise CommandError(result)
    return result


def run_checked(argv: list[str], **kwargs) -> CommandResult:
    """:func:`run` that raises :class:`CommandError` unless the command succeeds."""
    return run(argv, check=True, **kwargs)


def run_with_heartbeat(
    argv: list[str],
    *,
    heartbeat: "Callable[[float], bool]",
    interval: float = 1.0,
    timeout: float | None = None,
    input_text: str | None = None,
) -> CommandResult:
    """Run *argv* while calling *heartbeat* roughly every *interval* seconds.

    Several erase commands block for minutes or hours with no output at all -
    ``nvme format`` and ``hdparm --security-erase`` both do. Without this the
    interface would freeze on an indeterminate spinner and the operator would
    have no way to tell a working erase from a hung one.

    *heartbeat* receives the elapsed seconds and returns ``True`` to keep
    waiting or ``False`` to abandon the wait. Abandoning does **not** kill the
    command: a half-completed firmware erase is worse than a completed one, and
    several of these commands cannot be interrupted safely at all. The caller
    gets a result flagged ``timed_out`` while the command finishes in its own
    thread, which is the honest representation of what is happening.
    """
    import threading

    outcome: dict[str, CommandResult] = {}

    def worker() -> None:
        outcome["result"] = run(argv, timeout=timeout, input_text=input_text)

    thread = threading.Thread(target=worker, name=f"cmd-{argv[0]}", daemon=True)
    started = time.monotonic()
    thread.start()

    while thread.is_alive():
        thread.join(interval)
        elapsed = time.monotonic() - started
        if not thread.is_alive():
            break
        if not heartbeat(elapsed):
            _log.warning(
                "Stopped waiting for %s after %.0fs; the command is still running "
                "and will not be interrupted",
                " ".join(argv),
                elapsed,
            )
            return CommandResult(
                argv=list(argv),
                returncode=124,
                stdout="",
                stderr="wait abandoned by caller; command still running",
                duration_seconds=elapsed,
                timed_out=True,
            )

    return outcome.get(
        "result",
        CommandResult(
            argv=list(argv),
            returncode=1,
            stdout="",
            stderr="command thread produced no result",
            duration_seconds=time.monotonic() - started,
        ),
    )


def _as_text(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def _log_streams(result: CommandResult) -> None:
    """Log stdout at debug and stderr at warning, both truncated for sanity."""
    for name, text, level in (
        ("stdout", result.stdout, "debug"),
        ("stderr", result.stderr, "warning"),
    ):
        trimmed = (text or "").strip()
        if not trimmed:
            continue
        if len(trimmed) > 4000:
            trimmed = trimmed[:4000] + " ... [truncated]"
        getattr(_log, level)("%s %s: %s", result.argv[0], name, trimmed)
