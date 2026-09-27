"""Certificate generation - the record a run leaves behind.

:func:`issue_certificate` is the whole public surface: hand it a finished run
and the settings, and it writes the PDF (and, if configured, the JSON sidecar)
into the configured directory, returning the paths.

The PDF is produced for every run, including failed ones. A failed erase is a
thing that happened to a drive that may be about to leave the building, and the
record of the attempt is exactly as important as the record of a success - more
so, because it is the one somebody has to act on.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..config import Settings
from ..discovery.volumes import find_writable_volume
from ..logging_setup import current_log_path, get_logger
from ..models import RunSummary
from ..paths import certificate_dir, hand_back_to_invoking_user
from .naming import certificate_filename, certificate_id

_log = get_logger("Certificate")

__all__ = ["certificate_filename", "certificate_id", "issue_certificate"]


def issue_certificate(
    summary: RunSummary,
    settings: Settings,
    *,
    output_directory: Path | None = None,
) -> tuple[Path, Path | None]:
    """Write the certificate for *summary*; return ``(pdf_path, json_path)``.

    ``json_path`` is ``None`` when the sidecar is disabled in configuration.
    The ReportLab import is deferred to here so that the rest of the
    application - discovery, the erase engine, the CLI - keeps working on a
    system where ReportLab is missing. An erase must not be blocked by a
    reporting dependency; it is the certificate that fails, loudly, and the
    erase that still happened.
    """
    from .pdf import render_certificate, write_json_sidecar

    directory = output_directory
    if directory is None:
        configured = settings.certificate.output_directory.strip()
        if configured:
            directory = Path(configured)
        else:
            label = settings.certificate.output_volume_label.strip()
            directory = certificate_dir(
                output_volume=find_writable_volume(label) if label else None
            )
    directory.mkdir(parents=True, exist_ok=True)

    filename = certificate_filename(
        summary,
        max_serials=settings.certificate.max_serials_in_filename,
    )
    pdf_path = directory / filename

    _log.info(
        "Issuing certificate %s for %d drive(s): %s",
        certificate_id(summary),
        len(summary.results),
        ", ".join(summary.serials) or "none",
    )

    render_certificate(summary, settings, pdf_path)
    hand_back_to_invoking_user(pdf_path)
    _log.info("Certificate written to %s", pdf_path)

    json_path: Path | None = None
    if settings.certificate.write_json_sidecar:
        json_path = pdf_path.with_suffix(".json")
        write_json_sidecar(summary, settings, json_path)
        hand_back_to_invoking_user(json_path)
        _log.info("Certificate data written to %s", json_path)

    if settings.certificate.copy_log_beside_certificate:
        _copy_log_beside(pdf_path)

    hand_back_to_invoking_user(directory)
    return pdf_path, json_path


def _copy_log_beside(pdf_path: Path) -> None:
    """Copy this run's log next to the certificate, named to match it.

    On a live session the log lives in a tmpfs and is gone at power off, taking
    with it the only record of which commands were actually issued to each
    drive and what they returned. Copying it out beside the PDF means the
    evidence leaves the machine with the certificate rather than dying with it.

    The log is still being written when this runs, so the copy captures
    everything up to the moment the certificate was issued - which is the part
    that matters. Failure is logged and swallowed: the certificate is the
    deliverable and must not be lost to a copy error.
    """
    source = current_log_path()
    if source is None or not source.exists():
        _log.warning("No log file to copy beside the certificate")
        return

    destination = pdf_path.with_suffix(".log")
    try:
        # Flush the handlers first so the copy is not missing the last lines.
        for handler in logging.getLogger("zeroize").handlers:
            handler.flush()
        shutil.copy2(source, destination)
    except OSError as error:
        _log.error("Could not copy the log beside the certificate: %s", error)
        return

    hand_back_to_invoking_user(destination)
    _log.info("Run log copied to %s", destination)
