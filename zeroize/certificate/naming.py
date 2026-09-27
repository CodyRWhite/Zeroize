"""Certificate identifiers and filenames.

The filename follows the shape the reference certificate established, because
it is already what the asset-disposal process greps for::

    Certificate-<serials>-<outcome>-<YYYY-MM-DD-HH-MM-SS>.pdf
    Certificate-DD56419883A62-Success-2026-07-30-10-08-22.pdf

When several drives are erased in one run, every serial goes into the name,
joined by underscores::

    Certificate-DD56419883A62_Y0V0A01ATU18-Success-2026-09-26-14-22-05.pdf

Past a configurable count the name would become unusable - some filesystems and
most email clients baulk well before a dozen 20-character serials - so it
collapses to the first few plus a count::

    Certificate-DD56419883A62_Y0V0A01ATU18_BTPY72960ATT_and-5-more-Success-...

The full list is never lost: every serial is printed in the certificate's
summary table, and every serial appears in the JSON sidecar. The filename is a
convenience, and the document is the record.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from ..models import RunSummary

#: Characters allowed in a filename component. Serial numbers from the wild
#: contain spaces, slashes and occasionally control characters.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: Filenames longer than this are shortened even if the configured serial
#: count would have allowed more.
_MAX_FILENAME_LENGTH = 180


def sanitise_component(text: str, *, fallback: str = "unknown") -> str:
    """Make *text* safe for a filename without losing its readability."""
    cleaned = _UNSAFE.sub("-", text.strip()).strip("-.")
    return cleaned or fallback


def certificate_id(summary: RunSummary) -> str:
    """A short, stable identifier for the run, printed on the certificate.

    Derived from the serial numbers and the start time, so the same run always
    produces the same id and two different runs effectively never collide. It
    exists so a certificate can be referred to in a ticket or an asset record
    without quoting the whole filename.
    """
    material = "|".join(
        [summary.started_at.isoformat(), *sorted(summary.serials), summary.machine]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:10].upper()
    return f"ZRO-{digest[:5]}-{digest[5:]}"


def serial_component(summary: RunSummary, *, max_serials: int = 4) -> str:
    """The serial-number portion of the filename."""
    serials = [sanitise_component(serial, fallback="no-serial") for serial in summary.serials]
    if not serials:
        return "no-drives"
    if len(serials) <= max_serials:
        return "_".join(serials)
    shown = "_".join(serials[:max_serials])
    return f"{shown}_and-{len(serials) - max_serials}-more"


def certificate_filename(
    summary: RunSummary,
    *,
    max_serials: int = 4,
    extension: str = "pdf",
) -> str:
    """Build the certificate filename for *summary*."""
    stamp = summary.finished_at.strftime("%Y-%m-%d-%H-%M-%S")
    outcome = sanitise_component(summary.outcome_word, fallback="Unknown")

    # "Certificate" is a claim, and a filename is the part of a document most
    # likely to be read on its own - in a folder listing, in an email, on a
    # ticket. A file called Certificate-<serial>-Failed.pdf gets filed as a
    # certificate by anyone not reading to the end of the name. Runs that did
    # not erase everything are reports.
    prefix = "Certificate" if summary.all_succeeded else "Report"

    serials = serial_component(summary, max_serials=max_serials)
    name = f"{prefix}-{serials}-{outcome}-{stamp}.{extension}"

    # Shorten progressively rather than truncating mid-serial, which would
    # produce a name that looks like a real but wrong serial number.
    attempt = max_serials
    while len(name) > _MAX_FILENAME_LENGTH and attempt > 1:
        attempt -= 1
        serials = serial_component(summary, max_serials=attempt)
        name = f"{prefix}-{serials}-{outcome}-{stamp}.{extension}"

    if len(name) > _MAX_FILENAME_LENGTH:
        digest = hashlib.sha256("_".join(summary.serials).encode("utf-8")).hexdigest()[:8].upper()
        name = f"{prefix}-{len(summary.serials)}-drives-{digest}-{outcome}-{stamp}.{extension}"

    return name


def format_certificate_datetime(moment: datetime) -> tuple[str, str]:
    """Split a moment into the certificate's Date and Time fields.

    The reference prints them separately and spelled out - ``July 30, 2026``
    and ``10:08`` - which reads better on a document than an ISO timestamp.
    The precise machine-readable time appears in the Results block.
    """
    return moment.strftime("%B %d, %Y").replace(" 0", " "), moment.strftime("%H:%M")
