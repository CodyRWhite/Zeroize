"""NVMe erase operations: Format NVM and Sanitize.

Two commands with very different shapes, which is why they are separate
implementations rather than one parameterised function.

**Format NVM** (``nvme format --ses=N``) is synchronous. The command blocks
until the controller has finished, reports no progress while it works, and is
over in seconds for a crypto erase or minutes for a user-data erase. The
implementation therefore runs it with a heartbeat so the interface keeps
showing elapsed time, and reports an indeterminate fraction.

**Sanitize** (``nvme sanitize --sanact=N``) is asynchronous. The command
returns almost immediately and the controller carries on in the background,
publishing its progress in the Sanitize Status log page. That gives a real
percentage, and it also means the operation survives a reboot - the drive picks
up where it left off and refuses most other commands until it is done. The
implementation starts it, then polls the log page until the status register
says it finished.

A note on ``--ses`` values, since they are the crux of the request this tool
was built for:

=======  ==========================================================
``ses``  Meaning
=======  ==========================================================
0        No secure erase - a plain reformat. Not offered; it is not
         a sanitisation operation and must never be certified as one.
1        User Data Erase. The controller erases all user data.
2        Cryptographic Erase. The media encryption key is destroyed,
         making every block unreadable. Offered only when FNA bit 2
         said the controller supports it.
=======  ==========================================================

Two details in here are easy to get wrong and expensive to get wrong quietly:

**Scope.** Sanitize is issued to the *controller* (``/dev/nvme0``) and covers
every namespace. Format is issued to a *namespace* (``/dev/nvme0n1``) and
covers only that one - unless FNA bit 0 says otherwise. On a multi-namespace
drive with that bit clear, a single format erases part of the drive while
reporting complete success, so :func:`_format_targets` enumerates the
namespaces and formats each in turn.

**Completion.** A sanitize that has stopped running has not necessarily
succeeded. SSTAT 1 means completed, SSTAT 3 means failed, and both of them
report "not in progress". Only an explicit success status ends the poll loop
happily; see :class:`zeroize.discovery.nvme.SanitizeLog`.
"""

from __future__ import annotations

import time

from ..discovery.nvme import controller_path_for, namespace_id_for, read_sanitize_status
from ..logging_setup import get_logger
from ..models import Device, JobState, PassResult
from ..process import run, run_with_heartbeat, tool_available
from .context import EraseOutcome, JobContext
from .methods import (
    NVME_FORMAT_CRYPTO,
    NVME_SANITIZE_BLOCK,
    NVME_SANITIZE_CRYPTO,
    NVME_SANITIZE_OVERWRITE,
)

_log = get_logger("Nvme")

#: Secure Erase Settings value for each Format NVM method.
_SES_FOR_METHOD = {
    NVME_FORMAT_CRYPTO.key: 2,
    # Anything else in the format family is a user-data erase.
}
_SES_USER_DATA = 1

#: Sanitize Action (SANACT) codes from the NVMe specification.
_SANACT_BLOCK_ERASE = 2
_SANACT_OVERWRITE = 3
_SANACT_CRYPTO_ERASE = 4

_SANACT_FOR_METHOD = {
    NVME_SANITIZE_BLOCK.key: _SANACT_BLOCK_ERASE,
    NVME_SANITIZE_OVERWRITE.key: _SANACT_OVERWRITE,
    NVME_SANITIZE_CRYPTO.key: _SANACT_CRYPTO_ERASE,
}

#: A format is given a long ceiling because a user-data erase on a large,
#: heavily-worn drive can genuinely take this long. The heartbeat keeps the
#: interface alive meanwhile.
_FORMAT_TIMEOUT_SECONDS = 6 * 60 * 60

#: What nvme-cli's destructive-command prompt expects. It reads a single line
#: and compares it, case-sensitively, to this exact word.
_CONFIRMATION = "YES\n"

#: How often the sanitize status log is polled.
_SANITIZE_POLL_SECONDS = 3.0

#: A sanitize that reports no progress for this long is treated as stalled.
_SANITIZE_STALL_SECONDS = 15 * 60

#: How long the controller is allowed to take before it reflects the command in
#: the status log. Until this elapses, a status identical to the one recorded
#: before the command is read as "not started yet" rather than as a result.
#: How long to let a controller acknowledge a sanitize before concluding it
#: ignored the command. Generous on purpose: a drive that has not updated its
#: log page yet looks exactly like one that discarded the command, and the cost
#: of waiting is a few minutes while the cost of being wrong is a drive
#: reported unerased that is in fact erased - or the reverse. A Samsung
#: MZVL4256 took over half a minute to move off "never sanitized", which is why
#: 30 seconds proved too short.
_SANITIZE_START_GRACE_SECONDS = 300.0


def _pattern_word(method_key: str) -> str:
    """The certificate's pass label for a firmware-driven erase."""
    return {
        NVME_FORMAT_CRYPTO.key: "Cryptographic erase (SES=2)",
        NVME_SANITIZE_CRYPTO.key: "Sanitize - crypto erase (SANACT=4)",
        NVME_SANITIZE_BLOCK.key: "Sanitize - block erase (SANACT=2)",
        NVME_SANITIZE_OVERWRITE.key: "Sanitize - overwrite (SANACT=3)",
    }.get(method_key, "User data erase (SES=1)")


def _preflight(context: JobContext) -> str:
    """Return a blocking problem with the job, or an empty string."""
    if not tool_available("nvme"):
        return "nvme-cli is not installed"
    if context.device.simulated:
        return ""
    capabilities = context.device.nvme
    if capabilities is None:
        return f"{context.device.path} did not report NVMe capabilities"
    return ""


def _simulated_outcome(context: JobContext, label: str) -> EraseOutcome:
    """Walk the progress reporting without touching hardware.

    Used for ``--simulate`` and dry runs. It deliberately takes a few seconds
    rather than returning instantly, because an interface that has never been
    watched doing slow work tends to have bugs that only appear when it does.
    """
    total_steps = 12
    for step in range(total_steps + 1):
        if context.cancelled:
            return EraseOutcome.failure("cancelled by the operator")
        context.report(
            f"Simulating {label}",
            fraction=step / total_steps,
            elapsed_seconds=step * 0.25,
        )
        time.sleep(0.25)
    return EraseOutcome.success(
        passes=[PassResult(label=label, succeeded=True, detail="simulated")],
        bytes_written=context.device.size_bytes,
    )


def run_format(context: JobContext) -> EraseOutcome:
    """Erase an NVMe namespace with the Format NVM command."""
    problem = _preflight(context)
    if problem:
        return EraseOutcome.failure(problem)

    device = context.device
    method = context.method
    ses_value = _SES_FOR_METHOD.get(method.key, _SES_USER_DATA)
    label = _pattern_word(method.key)

    if device.simulated:
        return _simulated_outcome(context, label)

    controller = controller_path_for(device.path)
    targets = _format_targets(device)

    if len(targets) > 1:
        _log.warning(
            "%s exposes %d namespaces and does not format them together "
            "(FNA bit 0 clear) - each namespace will be formatted in turn",
            controller,
            len(targets),
        )

    passes: list[PassResult] = []
    for index, (target_path, namespace_argument, description) in enumerate(targets, start=1):
        context.report(
            f"Issuing {label} to {description}",
            fraction=(index - 1) / len(targets),
            pass_index=index,
            pass_total=len(targets),
        )

        result = _run_format_command(
            context,
            target_path,
            namespace_argument,
            ses_value,
            label,
            index,
            len(targets),
        )

        pass_label = label if len(targets) == 1 else f"{label} - {description}"

        if result.skipped:
            passes.append(PassResult(label=pass_label, succeeded=True, detail="dry run - not executed"))
            continue

        if not result.ok:
            _log.error("Format failed on %s: %s", description, result.failure_summary)
            passes.append(
                PassResult(label=pass_label, succeeded=False, detail=result.failure_summary)
            )
            for remaining in targets[index:]:
                passes.append(
                    PassResult(
                        label=f"{label} - {remaining[2]}",
                        succeeded=False,
                        detail="not attempted",
                    )
                )
            return EraseOutcome(succeeded=False, passes=passes, errors=[result.failure_summary])

        _log.info("Format completed on %s in %.1fs", description, result.duration_seconds)
        passes.append(PassResult(label=pass_label, succeeded=True))

    # Ask the kernel to re-enumerate: after a format the namespace may have a
    # different size or LBA format, and a stale in-kernel view would make the
    # verification read look at the wrong geometry.
    _rescan_namespaces(context, controller)

    return EraseOutcome.success(passes=passes, bytes_written=device.size_bytes)


def _format_targets(device: Device) -> list[tuple[str, str, str]]:
    """Work out what has to be formatted to cover the whole drive.

    Returns ``(device_path, namespace_argument, description)`` per command.

    This is the multi-namespace trap, and it is a quiet one. Format NVM applies
    only to the namespace it was addressed to unless FNA bit 0 is set. Issue a
    single ``nvme format /dev/nvme0n1`` against a two-namespace enterprise
    drive with that bit clear and you have erased half the drive while every
    indication to the operator - and the certificate - says the whole drive was
    wiped. So:

    * FNA bit 0 set, or only one namespace: one command covers everything.
    * Otherwise: one command per active namespace, each certified separately.
    """
    controller = controller_path_for(device.path)
    capabilities = device.nvme

    if capabilities is None:
        return [(device.path, f"--namespace-id={namespace_id_for(device.path)}", device.path)]

    namespaces = capabilities.active_namespaces or [namespace_id_for(device.path)]

    if capabilities.format_applies_to_all_namespaces or len(namespaces) <= 1:
        if capabilities.format_applies_to_all_namespaces and len(namespaces) > 1:
            # Address the controller with the broadcast namespace id so the
            # single command is unambiguous about its scope.
            return [(controller, "--namespace-id=0xffffffff", f"{controller} (all namespaces)")]
        single = namespaces[0]
        return [(device.path, f"--namespace-id={single}", device.path)]

    return [
        (controller, f"--namespace-id={identifier}", f"{controller} namespace {identifier}")
        for identifier in namespaces
    ]


def _run_format_command(
    context: JobContext,
    target_path: str,
    namespace_argument: str,
    ses_value: int,
    label: str,
    index: int,
    total: int,
):
    """Issue one format command, retrying without ``--force`` if it is rejected.

    nvme-cli grew an interactive confirmation for destructive commands, and
    ``--force`` is how it is bypassed. Older builds do not know the flag and
    exit on the unrecognised option, so the first rejection is retried without
    it rather than reported as a failed erase.

    That retry then meets the very prompt ``--force`` existed to skip, so it
    answers it: nvme-cli reads one line and compares it to ``YES``. Without
    that the command blocks on a prompt nobody can see - stdin is closed, so it
    would read EOF and abort, and before stdin was closed it blocked for the
    full six-hour timeout while the heartbeat reported the erase as running.
    """
    argv = ["nvme", "format", target_path, namespace_argument, f"--ses={ses_value}", "--force"]
    context.record_command(argv)
    _log.info("Formatting %s with SES=%d", target_path, ses_value)

    def heartbeat(elapsed: float) -> bool:
        context.report(
            f"{label} in progress - the controller reports no intermediate progress",
            fraction=None,
            pass_index=index,
            pass_total=total,
            elapsed_seconds=elapsed,
        )
        # A format cannot be interrupted safely, so cancellation is not honoured
        # here; the wait continues until the controller answers.
        return True

    result = run_with_heartbeat(argv, heartbeat=heartbeat, interval=1.0, timeout=_FORMAT_TIMEOUT_SECONDS)

    combined = f"{result.stdout}\n{result.stderr}".lower()
    if not result.ok and ("unrecognized option" in combined or "invalid option" in combined):
        retry_argv = [argument for argument in argv if argument != "--force"]
        _log.warning("This nvme-cli does not accept --force; retrying without it")
        context.record_command(retry_argv)
        result = run_with_heartbeat(
            retry_argv,
            heartbeat=heartbeat,
            interval=1.0,
            timeout=_FORMAT_TIMEOUT_SECONDS,
            input_text=_CONFIRMATION,
        )

    # Same reasoning as sanitize: keep whatever nvme-cli printed, at a level
    # that reaches the log file.
    _announce_output(target_path, "format", result)
    return result


def _announce_output(controller: str, what: str, result) -> None:
    """Record whatever a destructive command printed, at a level that persists."""
    for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        cleaned = (text or "").strip()
        if cleaned:
            _log.info("%s %s %s: %s", controller, what, stream, cleaned)
    if not (result.stdout or "").strip() and not (result.stderr or "").strip():
        _log.info(
            "%s %s exited %d and printed nothing",
            controller,
            what,
            result.returncode,
        )


def _alternative_hint(device: Device) -> str:
    """Name a method this drive does support, for a method it would not run.

    A controller that advertises Sanitize in SANICAP and then ignores the
    command is not a rare fault - consumer NVMe firmware does it - and the
    operator is left holding a drive the tool has just refused to certify.
    Format NVM is a different opcode and is very often accepted where Sanitize
    is not, so if the drive claims it, say so.
    """
    capabilities = device.nvme
    if capabilities is None or not capabilities.format_supported:
        return ""
    method = (
        "NVMe Format (Cryptographic Erase)"
        if capabilities.crypto_erase_supported
        else "NVMe Format (User Data Erase)"
    )
    return (
        f". This drive also reports Format NVM support, so try {method} "
        f"instead - it is a different command and is often accepted where "
        f"Sanitize is ignored"
    )


def _rescan_namespaces(context: JobContext, controller: str) -> None:
    """Re-enumerate namespaces after a format. Failure here is not fatal."""
    argv = ["nvme", "ns-rescan", controller]
    context.record_command(argv)
    result = run(argv, timeout=60.0)
    if not result.ok and not result.skipped:
        _log.info("Namespace rescan on %s reported: %s", controller, result.failure_summary)


def run_sanitize(context: JobContext) -> EraseOutcome:
    """Erase an NVMe drive with the Sanitize command, polling for progress."""
    problem = _preflight(context)
    if problem:
        return EraseOutcome.failure(problem)

    device = context.device
    method = context.method
    action = _SANACT_FOR_METHOD.get(method.key)
    label = _pattern_word(method.key)

    if action is None:
        return EraseOutcome.failure(f"{method.key} is not a sanitize method")

    if device.simulated:
        return _simulated_outcome(context, label)

    controller = controller_path_for(device.path)

    argv = ["nvme", "sanitize", controller, f"--sanact={action}"]
    if action == _SANACT_OVERWRITE:
        # The overwrite action needs a pattern and a pass count. One pass of
        # zeros is the specification's own default behaviour and is what the
        # certificate will record.
        argv += ["--ovrpat=0", "--owpass=1"]

    # Read the status log BEFORE issuing the command. Without a baseline there
    # is no way to tell a freshly completed sanitize from one that completed
    # last week - SSTAT reads 1 for both - and a drive that never started would
    # be certified as erased. This is the single most dangerous thing this file
    # does, so it is established first.
    baseline = read_sanitize_status(controller)
    baseline_status = baseline.status if baseline is not None else None
    if baseline is not None:
        _log.info(
            "%s sanitize status before the command: %s (SSTAT 0x%04x)",
            controller,
            baseline.description,
            baseline.raw_sstat,
        )
        if baseline.in_progress:
            return EraseOutcome.failure(
                f"a sanitize is already running on {controller} "
                f"({baseline.percent:.0f}% complete); wait for it to finish"
            )

    context.record_command(argv)
    context.report(f"Starting {label} on {controller}", fraction=0.0, pass_index=1, pass_total=1)
    _log.info("Starting sanitize on %s with SANACT=%d", controller, action)

    start_result = run(argv, timeout=120.0)
    if start_result.skipped:
        return EraseOutcome.success(
            passes=[PassResult(label=label, succeeded=True, detail="dry run - not executed")],
        )
    if not start_result.ok:
        _log.error("Sanitize refused on %s: %s", controller, start_result.failure_summary)
        return EraseOutcome.failure(
            start_result.failure_summary,
            passes=[PassResult(label=label, succeeded=False, detail=start_result.failure_summary)],
        )

    # Logged at INFO even on success. run() logs stdout at debug, which does
    # not reach the file, and a controller that accepts this command and then
    # does nothing is indistinguishable from one that ran it - unless whatever
    # nvme-cli printed on the way through was kept. Observed on a Samsung NVMe
    # that exited 0 and left SSTAT at "never sanitized".
    _announce_output(controller, "sanitize", start_result)

    # The command has returned but the controller is still working. Poll the
    # sanitize status log page until it stops reporting "in progress".
    #
    # The controller does not necessarily set SSTAT to "in progress" the
    # instant the command is accepted, so the first few reads can still show
    # the *previous* state. Two mistakes are possible in that window and both
    # matter: reading a stale 0 and declaring failure on a sanitize that is
    # about to start, or - far worse - reading a stale 1 left over from an
    # earlier run and certifying a drive that was never touched. So a terminal
    # status is only believed once there is evidence this operation actually
    # ran: either it was seen in progress, or the status changed from what it
    # was before the command.
    started = time.monotonic()
    last_percent = -1.0
    last_movement = started
    observed_running = False

    while True:
        time.sleep(_SANITIZE_POLL_SECONDS)
        elapsed = time.monotonic() - started
        sanitize_log = read_sanitize_status(controller)

        if sanitize_log is None:
            # Losing the log page mid-operation usually means the controller is
            # refusing commands while it works, which is permitted. Keep
            # waiting, but say so honestly.
            context.report(
                f"{label} in progress - the controller is not answering log queries",
                fraction=None,
                pass_index=1,
                pass_total=1,
                elapsed_seconds=elapsed,
            )
            if time.monotonic() - last_movement > _SANITIZE_STALL_SECONDS:
                return EraseOutcome.failure(
                    f"sanitize status could not be read for {_SANITIZE_STALL_SECONDS // 60} minutes",
                    passes=[PassResult(label=label, succeeded=False, detail="status unreadable")],
                )
            continue

        percent = sanitize_log.percent
        if percent > last_percent:
            last_percent = percent
            last_movement = time.monotonic()

        if sanitize_log.in_progress:
            observed_running = True

        if not sanitize_log.in_progress:
            # Is this status evidence of *this* operation, or left over from a
            # previous one? Believe it only if we watched it run, or if it
            # differs from the state recorded before the command was issued.
            status_changed = baseline_status is None or sanitize_log.status != baseline_status

            # SCDW10 records Dword 10 of the last Sanitize command the
            # controller accepted, so its action bits naming the action we just
            # sent is direct evidence this operation reached the drive - much
            # stronger than inferring from SSTAT alone, which cannot separate
            # "never received it" from "has not updated the log page yet".
            #
            # This matters on real hardware: a Samsung MZVL4256 reported SSTAT
            # 0x0000 for thirty seconds and was declared a failure, then read
            # back SSTAT 0x1 with SPROG 65535 and SCDW10 0x2 - a completed
            # block erase - twenty-five seconds later. The sanitize had run;
            # only the log page was slow.
            records_our_command = sanitize_log.records_action(action)
            confirmed = observed_running or status_changed or records_our_command

            if not confirmed and elapsed < _SANITIZE_START_GRACE_SECONDS:
                _log.info(
                    "%s has not acknowledged the sanitize yet "
                    "(SSTAT 0x%04x, SCDW10 0x%08x, %.0fs elapsed)",
                    controller,
                    sanitize_log.raw_sstat,
                    sanitize_log.raw_scdw10,
                    elapsed,
                )
                # Same status as before the command and never seen running -
                # the controller has most likely not started yet.
                context.report(
                    f"{label} - waiting for the controller to begin",
                    fraction=0.0,
                    pass_index=1,
                    pass_total=1,
                    elapsed_seconds=elapsed,
                )
                continue

            if not confirmed:
                detail = (
                    f"the controller still reports '{sanitize_log.description}' "
                    f"(SSTAT 0x{sanitize_log.raw_sstat:04x}) "
                    f"{int(elapsed)}s after the command, unchanged from before it. "
                    f"The drive accepted the command and did not act on it"
                    f"{_alternative_hint(context.device)}"
                )
                _log.error("Sanitize never started on %s: %s", controller, detail)
                return EraseOutcome.failure(
                    detail,
                    passes=[PassResult(label=label, succeeded=False, detail="never started")],
                )

            # "Finished" is not "succeeded". SSTAT 3 means the operation failed
            # and SSTAT 0 means it never started, and both of them stop
            # reporting in-progress. Only an explicit completion status counts.
            if not sanitize_log.succeeded:
                detail = (
                    f"the controller reports sanitize {sanitize_log.description} "
                    f"(SSTAT 0x{sanitize_log.raw_sstat:04x})"
                )
                _log.error("Sanitize did not succeed on %s: %s", controller, detail)
                return EraseOutcome.failure(
                    detail,
                    passes=[PassResult(label=label, succeeded=False, detail=detail)],
                )

            attestation = (
                "controller reports Global Data Erased"
                if sanitize_log.global_data_erased
                else "controller did not set the Global Data Erased bit"
            )
            _log.info("Sanitize completed on %s in %.0fs (%s)", controller, elapsed, attestation)
            context.report(
                f"{label} complete",
                fraction=1.0,
                pass_index=1,
                pass_total=1,
                elapsed_seconds=elapsed,
            )
            _rescan_namespaces(context, controller)

            return EraseOutcome.success(
                passes=[
                    PassResult(
                        label=label,
                        succeeded=True,
                        detail=(
                            f"{sanitize_log.description}; {attestation}; "
                            f"status before the command was "
                            f"{baseline.description if baseline else 'unreadable'}"
                        ),
                    )
                ],
                bytes_written=device.size_bytes,
            )

        eta = None
        if percent > 0:
            eta = max(0.0, elapsed * (100.0 - percent) / percent)

        context.report(
            f"{label} - {percent:.0f}% complete",
            fraction=percent / 100.0,
            pass_index=1,
            pass_total=1,
            elapsed_seconds=elapsed,
            eta_seconds=eta,
        )

        if time.monotonic() - last_movement > _SANITIZE_STALL_SECONDS:
            _log.error("Sanitize on %s stalled at %.0f%%", controller, percent)
            return EraseOutcome.failure(
                f"sanitize stalled at {percent:.0f}% for {_SANITIZE_STALL_SECONDS // 60} minutes",
                passes=[PassResult(label=label, succeeded=False, detail=f"stalled at {percent:.0f}%")],
            )

        if context.cancelled:
            # Sanitize cannot be aborted; the drive is unusable until it
            # finishes. Say exactly that rather than pretending to stop.
            _log.warning("Cancel requested during sanitize on %s - not interruptible", controller)
            context.report(
                "Sanitize cannot be cancelled - the drive must finish before it can be used",
                state=JobState.RUNNING,
                fraction=percent / 100.0,
                elapsed_seconds=elapsed,
            )
