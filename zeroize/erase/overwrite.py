"""Multi-pass software overwrite - the universal fallback.

Any block device that can be opened for writing can be overwritten, which makes
this the method of last resort for drives whose firmware offers no usable erase
command, and the method of policy for organisations whose standards still
mandate a pass count.

This is written directly against the device rather than shelling out to ``dd``.
``dd`` would work, but it gives up three things that matter here:

* **Real progress.** ``dd`` reports only when it finishes or when it is sent
  ``SIGUSR1``. Writing the loop here means an exact byte count, a measured
  throughput and an honest ETA on a job that can run for a day.
* **Prompt cancellation.** The loop checks for cancellation between blocks, so
  stopping takes milliseconds. An overwrite is safely interruptible - the drive
  is left partly overwritten, which is never worse than where it started.
* **Verification that means something.** A digest of each block that will later
  be sampled is recorded *as it is written*, so the verification read compares
  against what was actually sent to the platter. Comparing against the expected
  pattern instead would silently pass a drive that ignored the write.

Random passes reuse a buffer that is refreshed periodically rather than drawing
fresh entropy for every block. Drawing ~2 TB from ``getrandom`` would make the
kernel, not the disk, the bottleneck, and for data destruction the statistical
quality of the replacement bytes is irrelevant - what matters is that the
original bytes are gone. The refresh interval is a deliberate, documented
trade-off, not an oversight.
"""

from __future__ import annotations

import hashlib
import os
import random
import time

from ..logging_setup import get_logger
from ..models import JobState, PassResult, PassSpec, format_size
from ..process import is_dry_run
from .context import EraseOutcome, JobContext

_log = get_logger("Overwrite")

#: How often the random buffer is regenerated during a random pass.
_RANDOM_REFRESH_BLOCKS = 64

#: Minimum interval between progress reports, to keep the UI thread free.
_PROGRESS_INTERVAL_SECONDS = 0.5

#: Blocks sampled for verification are capped so a huge drive does not spend
#: an unreasonable amount of memory on digests.
_MAX_VERIFY_SAMPLES = 20000


def _open_device(path: str) -> int:
    """Open a block device for writing, failing loudly if it is not one.

    ``O_EXCL`` on a block device asks the kernel to refuse the open if anything
    else already has it open exclusively - most usefully, if it is mounted.
    That is a second interlock behind the one discovery applies, and it is the
    one the kernel itself enforces.
    """
    flags = os.O_WRONLY | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(path, flags)


def _verification_offsets(total_blocks: int, percent: float) -> set[int]:
    """Choose which block indices to verify.

    The first and last blocks are always included - they hold the partition
    table and the backup GPT header, the two places where a failed wipe is most
    visible and most consequential. The rest are spread evenly with a jittered
    offset, so a drive that zeroes only aligned regions cannot pass by luck.
    """
    if total_blocks <= 0 or percent <= 0:
        return set()
    if percent >= 100:
        return set(range(total_blocks))

    wanted = min(_MAX_VERIFY_SAMPLES, max(2, int(total_blocks * percent / 100)))
    chosen = {0, max(0, total_blocks - 1)}
    if wanted <= len(chosen):
        return chosen

    stride = total_blocks / (wanted - len(chosen))
    # Picks WHICH blocks to sample, not what is written to them. A
    # cryptographic generator would be slower and buy nothing, and the
    # seed is deliberate so a verification pass is reproducible.
    jitter = random.Random(total_blocks).random()  # noqa: S311
    for step in range(wanted - len(chosen)):
        index = int((step + jitter) * stride)
        if 0 <= index < total_blocks:
            chosen.add(index)
    return chosen


def _fill_buffer(pattern: bytes, size: int) -> bytes:
    """Build one block-sized buffer by repeating *pattern*."""
    repeats = size // len(pattern) + 1
    return (pattern * repeats)[:size]


def _write_pass(
    context: JobContext,
    file_descriptor: int,
    spec: PassSpec,
    pass_index: int,
    pass_total: int,
    block_size: int,
    verify_offsets: set[int],
) -> tuple[bool, str, dict[int, bytes], int]:
    """Write one pass across the whole device.

    Returns ``(succeeded, detail, digests, bytes_written)``. ``digests`` maps a
    block index to the SHA-256 of what was written there, populated only for
    blocks in *verify_offsets* and only when this pass is the one that will be
    verified.
    """
    device = context.device
    total_bytes = device.size_bytes
    digests: dict[int, bytes] = {}
    collect_digests = spec.verify and bool(verify_offsets)

    if spec.pattern is None:
        buffer = os.urandom(block_size)
    else:
        buffer = _fill_buffer(spec.pattern, block_size)

    try:
        os.lseek(file_descriptor, 0, os.SEEK_SET)
    except OSError as error:
        return False, f"could not seek to the start of {device.path}: {error}", {}, 0

    written = 0
    block_index = 0
    started = time.monotonic()
    last_report = 0.0

    while written < total_bytes:
        if context.cancelled:
            return False, "cancelled by the operator", digests, written

        chunk_size = min(block_size, total_bytes - written)

        if spec.pattern is None and block_index % _RANDOM_REFRESH_BLOCKS == 0 and block_index:
            buffer = os.urandom(block_size)

        chunk = buffer if chunk_size == block_size else buffer[:chunk_size]

        if collect_digests and block_index in verify_offsets:
            digests[block_index] = hashlib.sha256(chunk).digest()

        # os.write can write fewer bytes than asked; loop until the block is out.
        offset_in_chunk = 0
        while offset_in_chunk < chunk_size:
            try:
                sent = os.write(file_descriptor, chunk[offset_in_chunk:])
            except OSError as error:
                detail = (
                    f"write failed at byte {written + offset_in_chunk} "
                    f"({format_size(written + offset_in_chunk)} in): {error}"
                )
                _log.error("%s on %s", detail, device.path)
                return False, detail, digests, written + offset_in_chunk
            if sent == 0:
                detail = f"the device stopped accepting writes at byte {written + offset_in_chunk}"
                return False, detail, digests, written + offset_in_chunk
            offset_in_chunk += sent

        written += chunk_size
        block_index += 1

        elapsed = time.monotonic() - started
        if elapsed - last_report >= _PROGRESS_INTERVAL_SECONDS or written >= total_bytes:
            last_report = elapsed
            rate = written / elapsed if elapsed > 0 else 0.0
            # The fraction spans the whole method, not just this pass, so the
            # bar moves steadily across a seven-pass run instead of resetting.
            pass_fraction = written / total_bytes if total_bytes else 1.0
            overall = ((pass_index - 1) + pass_fraction) / pass_total
            remaining_this_pass = (total_bytes - written) / rate if rate > 0 else None
            eta = None
            if remaining_this_pass is not None:
                remaining_passes = pass_total - pass_index
                eta = remaining_this_pass + remaining_passes * (total_bytes / rate if rate else 0)
            context.report(
                f"{spec.label} - {format_size(written)} of {format_size(total_bytes)} "
                f"at {format_size(int(rate))}/s",
                fraction=overall,
                pass_index=pass_index,
                pass_total=pass_total,
                bytes_done=written,
                bytes_total=total_bytes,
                elapsed_seconds=elapsed,
                eta_seconds=eta,
            )

    # Push the drive's own write cache out before the pass is called done.
    try:
        os.fsync(file_descriptor)
    except OSError as error:
        return False, f"fsync failed after {spec.label}: {error}", digests, written

    _log.info(
        "%s completed on %s: %s in %.0fs",
        spec.label,
        device.path,
        format_size(written),
        time.monotonic() - started,
    )
    return True, "", digests, written


def _verify_pass(
    context: JobContext,
    spec: PassSpec,
    digests: dict[int, bytes],
    block_size: int,
) -> tuple[bool, str, float]:
    """Read the sampled blocks back and compare them against what was written.

    Returns ``(passed, detail, percent_of_device_checked)``.
    """
    device = context.device
    if not digests:
        return True, "verification not requested", 0.0

    total_bytes = device.size_bytes
    checked_bytes = 0
    mismatches: list[int] = []
    started = time.monotonic()

    try:
        read_descriptor = os.open(device.path, os.O_RDONLY)
    except OSError as error:
        return False, f"could not reopen {device.path} to verify: {error}", 0.0

    try:
        for position, (block_index, expected) in enumerate(sorted(digests.items()), start=1):
            if context.cancelled:
                return False, "verification cancelled by the operator", checked_bytes / total_bytes * 100

            offset = block_index * block_size
            length = min(block_size, total_bytes - offset)
            if length <= 0:
                continue

            try:
                os.lseek(read_descriptor, offset, os.SEEK_SET)
                data = os.read(read_descriptor, length)
            except OSError as error:
                return (
                    False,
                    f"read failed at byte {offset} while verifying: {error}",
                    checked_bytes / total_bytes * 100 if total_bytes else 0.0,
                )

            # A short read at the very end of the device is normal; anywhere
            # else it means the drive is failing.
            if len(data) != length:
                if offset + len(data) < total_bytes:
                    mismatches.append(block_index)
                    continue

            if hashlib.sha256(data).digest() != expected:
                mismatches.append(block_index)

            checked_bytes += length

            if position % 64 == 0 or position == len(digests):
                context.report(
                    f"Verifying {spec.label} - {position} of {len(digests)} sampled blocks",
                    state=JobState.VERIFYING,
                    fraction=position / len(digests),
                    bytes_done=checked_bytes,
                    bytes_total=total_bytes,
                    elapsed_seconds=time.monotonic() - started,
                )
    finally:
        os.close(read_descriptor)

    percent = (checked_bytes / total_bytes * 100) if total_bytes else 0.0

    if mismatches:
        shown = ", ".join(str(index) for index in mismatches[:5])
        more = f" and {len(mismatches) - 5} more" if len(mismatches) > 5 else ""
        detail = f"{len(mismatches)} of {len(digests)} sampled blocks did not read back correctly (blocks {shown}{more})"
        _log.error("Verification failed on %s: %s", device.path, detail)
        return False, detail, percent

    _log.info(
        "Verification passed on %s: %d blocks, %s checked (%.1f%% of the device)",
        device.path,
        len(digests),
        format_size(checked_bytes),
        percent,
    )
    return True, f"{len(digests)} sampled blocks matched", percent


def run_overwrite(context: JobContext) -> EraseOutcome:
    """Run every pass of an overwrite method across the whole device."""
    device = context.device
    method = context.method
    block_size = context.settings.erase.overwrite_block_size
    total_blocks = (device.size_bytes + block_size - 1) // block_size

    if device.simulated or is_dry_run():
        return _simulated_overwrite(context)

    if device.size_bytes <= 0:
        return EraseOutcome.failure("the device reports a size of zero")

    verify_offsets = (
        _verification_offsets(total_blocks, context.settings.erase.verification_percent)
        if method.supports_verification
        else set()
    )

    context.record_command(
        [
            f"# internal writer: {method.pass_count} pass(es) over {device.path}",
            f"block_size={block_size}",
            f"verify_samples={len(verify_offsets)}",
        ]
    )

    try:
        file_descriptor = _open_device(device.path)
    except OSError as error:
        hint = ""
        if getattr(error, "errno", None) == 16:  # EBUSY
            hint = " - it is in use, most likely mounted"
        return EraseOutcome.failure(f"could not open {device.path} for writing: {error}{hint}")

    results: list[PassResult] = []
    total_written = 0
    verification_percent = 0.0
    verification_passed: bool | None = None

    try:
        for index, spec in enumerate(method.passes, start=1):
            context.report(
                f"Starting {spec.label}",
                fraction=(index - 1) / method.pass_count,
                pass_index=index,
                pass_total=method.pass_count,
            )

            succeeded, detail, digests, written = _write_pass(
                context,
                file_descriptor,
                spec,
                index,
                method.pass_count,
                block_size,
                verify_offsets,
            )
            total_written += written
            results.append(PassResult(label=spec.label, succeeded=succeeded, detail=detail))

            if not succeeded:
                # Record the passes that never ran, so the certificate shows the
                # full sequence with an honest outcome for each.
                for remaining in method.passes[index:]:
                    results.append(
                        PassResult(label=remaining.label, succeeded=False, detail="not attempted")
                    )
                return EraseOutcome(
                    succeeded=False,
                    passes=results,
                    errors=[detail],
                    bytes_written=total_written,
                )

            if spec.verify and verify_offsets:
                passed, verify_detail, percent = _verify_pass(context, spec, digests, block_size)
                verification_passed = passed
                verification_percent = percent
                results.append(
                    PassResult(
                        label="Verification",
                        succeeded=passed,
                        detail=verify_detail,
                    )
                )
                if not passed:
                    return EraseOutcome(
                        succeeded=False,
                        passes=results,
                        errors=[f"verification failed: {verify_detail}"],
                        bytes_written=total_written,
                        verification_percent=percent,
                        verification_passed=False,
                    )
    finally:
        try:
            os.fsync(file_descriptor)
        except OSError:
            pass
        os.close(file_descriptor)

    return EraseOutcome(
        succeeded=True,
        passes=results,
        bytes_written=total_written,
        verification_percent=verification_percent,
        verification_passed=verification_passed,
    )


def _simulated_overwrite(context: JobContext) -> EraseOutcome:
    """Walk every pass of the method without opening the device.

    Each pass is stepped through quickly but visibly, so a simulated run
    exercises the same progress reporting, pass accounting and certificate
    shape that a real one produces.
    """
    method = context.method
    results: list[PassResult] = []
    total_bytes = context.device.size_bytes
    steps_per_pass = 8

    for index, spec in enumerate(method.passes, start=1):
        for step in range(steps_per_pass + 1):
            if context.cancelled:
                results.append(PassResult(label=spec.label, succeeded=False, detail="cancelled"))
                return EraseOutcome(succeeded=False, passes=results, errors=["cancelled by the operator"])
            pass_fraction = step / steps_per_pass
            context.report(
                f"Simulating {spec.label}",
                fraction=((index - 1) + pass_fraction) / method.pass_count,
                pass_index=index,
                pass_total=method.pass_count,
                bytes_done=int(total_bytes * pass_fraction),
                bytes_total=total_bytes,
            )
            time.sleep(0.12)
        results.append(PassResult(label=spec.label, succeeded=True, detail="simulated"))

    if method.supports_verification:
        context.report("Simulating verification", state=JobState.VERIFYING, fraction=1.0)
        time.sleep(0.4)
        results.append(PassResult(label="Verification", succeeded=True, detail="simulated"))

    return EraseOutcome(
        succeeded=True,
        passes=results,
        bytes_written=total_bytes * method.pass_count,
        verification_percent=context.settings.erase.verification_percent,
        verification_passed=True if method.supports_verification else None,
    )
