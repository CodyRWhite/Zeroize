"""The erase certificate, rendered as a PDF.

The document follows the structure of the KillDisk certificate this tool was
asked to match, because that is what the disposal process already files and
audits against, but it is set in the Zeroize identity and fixes the two things
the original does badly for multi-drive runs.

Structure, in order:

1. A header band carrying the mark and wordmark, and the certificate id.
2. The title, the seal, and the run block - organisation, operator, machine,
   date and time.
3. **The drive summary table.** Every drive in the run, with its serial number,
   method and result. This is new: the reference certificate simply repeats a
   per-drive block, so a five-drive run gives you five pages and no single
   place that lists what was covered. Here the first page always answers "what
   did this run erase?" in one table.
4. One detail section per drive - Attributes, Disk Information, Results with
   the full pass list, and the partition layout the drive had beforehand.
5. System Information and Hardware Information for the host.
6. Optionally, the command appendix: every command issued, verbatim.

Layout is done with ReportLab's platypus flowables so content reflows across
pages naturally, with the header band, footer and page numbers painted by a
page callback. The label-right/value-left pairs that give the reference its
look are built by :func:`_field_table`.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from .. import __app_name__, __project_url__, __short_name__, __version__
from ..branding import (
    AMBER,
    CHARCOAL,
    MIST,
    ORANGE,
    PAPER,
    SIGNAL,
    STEEL,
    VERDANT,
    draw_mark,
    wordmark_letterspacing,
)
from ..config import Organisation, Settings
from ..models import EraseResult, Partition, RunSummary, format_duration, format_size
from .naming import certificate_id, format_certificate_datetime

PAGE_SIZE = letter
PAGE_WIDTH, PAGE_HEIGHT = PAGE_SIZE

_MARGIN_X = 0.7 * inch
_HEADER_BAND_HEIGHT = 46
_FOOTER_HEIGHT = 46
_CONTENT_TOP_GAP = 22

_BODY_FONT = "Helvetica"
_BOLD_FONT = "Helvetica-Bold"


# --------------------------------------------------------------------------
# Styles
# --------------------------------------------------------------------------

def _styles() -> dict[str, ParagraphStyle]:
    """Build the paragraph styles once per render."""
    return {
        "title": ParagraphStyle(
            "title",
            fontName=_BOLD_FONT,
            fontSize=25,
            leading=29,
            textColor=colors.HexColor(CHARCOAL),
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            fontName=_BODY_FONT,
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor(STEEL),
        ),
        "section": ParagraphStyle(
            "section",
            fontName=_BOLD_FONT,
            fontSize=12.5,
            leading=15,
            textColor=colors.HexColor(CHARCOAL),
            spaceBefore=14,
            spaceAfter=5,
        ),
        "subsection": ParagraphStyle(
            "subsection",
            fontName=_BOLD_FONT,
            fontSize=9.5,
            leading=12,
            textColor=colors.HexColor(ORANGE),
            spaceBefore=9,
            spaceAfter=3,
        ),
        "label": ParagraphStyle(
            "label",
            fontName=_BODY_FONT,
            fontSize=8,
            leading=11,
            alignment=TA_RIGHT,
            textColor=colors.HexColor(STEEL),
        ),
        "value": ParagraphStyle(
            "value",
            fontName=_BOLD_FONT,
            fontSize=8,
            leading=11,
            alignment=TA_LEFT,
            textColor=colors.HexColor(CHARCOAL),
        ),
        "cell": ParagraphStyle(
            "cell",
            fontName=_BODY_FONT,
            fontSize=7.6,
            leading=10,
            textColor=colors.HexColor(CHARCOAL),
        ),
        "cell_head": ParagraphStyle(
            "cell_head",
            fontName=_BOLD_FONT,
            fontSize=7.4,
            leading=10,
            textColor=colors.HexColor(PAPER),
        ),
        "note": ParagraphStyle(
            "note",
            fontName=_BODY_FONT,
            fontSize=7.6,
            leading=10.5,
            textColor=colors.HexColor(STEEL),
        ),
        "mono": ParagraphStyle(
            "mono",
            fontName="Courier",
            fontSize=7,
            leading=9.5,
            textColor=colors.HexColor(CHARCOAL),
        ),
    }


def _outcome_colour(succeeded: bool) -> str:
    return VERDANT if succeeded else SIGNAL


def _escape(text: object) -> str:
    """Escape a value for use inside a ReportLab paragraph."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------

class _Seal(Flowable):
    """The verification seal printed beside the title.

    Drawn as vector geometry rather than placed as an image, for the same
    reason the mark is: one definition, crisp at any size, no binary asset in
    the package that could drift out of step with the brand module.

    **The brand mark is deliberately not used here.** It is a slashed zero, and
    set directly above the caption it reads as a digit - so a failed run was
    sealed with what looked like "0 ERASE INCOMPLETE", which states the exact
    opposite of what happened. On a document whose entire purpose is to be read
    by someone who was not present at the erase, that is not a cosmetic
    problem. A tick or a cross carries the verdict with no numeric reading.
    """

    def __init__(self, size: float = 78, *, passed: bool = True) -> None:
        super().__init__()
        self.width = size
        self.height = size
        self._passed = passed

    def draw(self) -> None:
        canvas = self.canv
        radius = self.width / 2
        centre = radius
        accent = ORANGE if self._passed else SIGNAL
        inner_radius = radius - 6

        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor(accent))
        canvas.setLineWidth(1.6)
        canvas.circle(centre, centre, radius - 1, stroke=1, fill=0)
        canvas.setLineWidth(0.7)
        canvas.circle(centre, centre, inner_radius, stroke=1, fill=0)

        # The verdict glyph sits above centre, leaving the caption a clear band
        # below. A tick or a cross, never the brand mark - see the class
        # docstring for why.
        glyph = radius * 0.52
        glyph_centre_y = centre + radius * 0.22
        canvas.setStrokeColor(colors.HexColor(accent))
        canvas.setLineWidth(max(1.8, glyph * 0.16))
        canvas.setLineCap(1)
        canvas.setLineJoin(1)

        if self._passed:
            path = canvas.beginPath()
            path.moveTo(centre - glyph * 0.46, glyph_centre_y + glyph * 0.04)
            path.lineTo(centre - glyph * 0.12, glyph_centre_y - glyph * 0.34)
            path.lineTo(centre + glyph * 0.48, glyph_centre_y + glyph * 0.40)
            canvas.drawPath(path, stroke=1, fill=0)
        else:
            arm = glyph * 0.40
            canvas.line(centre - arm, glyph_centre_y - arm, centre + arm, glyph_centre_y + arm)
            canvas.line(centre - arm, glyph_centre_y + arm, centre + arm, glyph_centre_y - arm)

        caption = "VERIFIED ERASE" if self._passed else "ERASE INCOMPLETE"
        caption_baseline = centre - radius * 0.46

        # Size the caption to the chord of the inner circle at its baseline,
        # so the longer "ERASE INCOMPLETE" cannot overrun the ring the way a
        # fixed size would. The two captions then read at different sizes,
        # which is correct: the ring is the constraint, not the type.
        vertical_offset = abs(caption_baseline - centre) + 2
        half_chord = max(6.0, (inner_radius**2 - vertical_offset**2) ** 0.5)
        available = half_chord * 2 * 0.88

        font_size = 6.0
        natural = pdfmetrics.stringWidth(caption, _BOLD_FONT, font_size)
        if natural > available:
            font_size = max(3.6, font_size * available / natural)

        canvas.setFillColor(colors.HexColor(CHARCOAL))
        canvas.setFont(_BOLD_FONT, font_size)
        canvas.drawCentredString(centre, caption_baseline, caption)
        canvas.restoreState()


def _field_table(
    rows: list[tuple[str, str]],
    styles: dict[str, ParagraphStyle],
    *,
    label_width: float,
    value_width: float,
    value_colours: dict[str, str] | None = None,
) -> Table:
    """The right-aligned-label / left-aligned-value pattern used throughout.

    An empty *rows* renders a placeholder rather than an empty table. ReportLab
    refuses to build a table with no rows, and several of these blocks are fed
    from host discovery, which legitimately returns nothing on a machine with
    no DMI tree. A certificate missing its hardware block is still a valid
    record; a traceback after the drives have already been erased is not.
    """
    value_colours = value_colours or {}

    if not rows:
        rows = [("", "Not available")]

    data = []
    for label, value in rows:
        colour = value_colours.get(label)
        style = styles["value"]
        if colour:
            style = ParagraphStyle(f"value-{label}", parent=style, textColor=colors.HexColor(colour))
        # A blank label marks a continuation row - a second address line, say -
        # and must not render as a bare colon.
        label_text = f"{_escape(label)}:" if label.strip() else ""
        data.append(
            [
                Paragraph(label_text, styles["label"]),
                Paragraph(_escape(value), style),
            ]
        )

    table = Table(data, colWidths=[label_width, value_width], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (0, -1), 6),
                ("RIGHTPADDING", (1, 0), (1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1.2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
            ]
        )
    )
    return table


def _two_column(left: Flowable, right: Flowable, content_width: float, gap: float = 20) -> Table:
    """Place two flowables side by side, as the reference does for its blocks."""
    column = (content_width - gap) / 2
    table = Table([[left, right]], colWidths=[column, gap + column], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _data_table(
    header: list[str],
    rows: list[list[object]],
    styles: dict[str, ParagraphStyle],
    column_widths: list[float],
    *,
    row_colours: list[str | None] | None = None,
) -> Table:
    """A banded table with a charcoal header, used for the drive summary.

    As with :func:`_field_table`, an empty body renders a placeholder row
    rather than letting ReportLab refuse a table with no rows.
    """
    data = [[Paragraph(_escape(cell), styles["cell_head"]) for cell in header]]
    if not rows:
        rows = [["-"] * len(header)]
    for row in rows:
        data.append(
            [
                cell if isinstance(cell, Flowable) else Paragraph(_escape(cell), styles["cell"])
                for cell in row
            ]
        )

    table = Table(data, colWidths=column_widths, hAlign="LEFT", repeatRows=1)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(CHARCOAL)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor(MIST)),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.HexColor(STEEL)),
    ]
    for index in range(1, len(data)):
        if index % 2 == 0:
            commands.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor(MIST)))
    if row_colours:
        for index, colour in enumerate(row_colours, start=1):
            if colour:
                commands.append(("TEXTCOLOR", (-1, index), (-1, index), colors.HexColor(colour)))
                commands.append(("FONTNAME", (-1, index), (-1, index), _BOLD_FONT))

    table.setStyle(TableStyle(commands))
    return table


class _PartitionMap(Flowable):
    """A proportional bar showing the partition layout a drive had.

    A table of offsets is accurate but unreadable; the bar is what an operator
    actually recognises as "yes, that was the drive I handed over". Segments
    narrower than a couple of points are still drawn so that a small EFI
    partition does not silently vanish from the picture.
    """

    def __init__(
        self,
        partitions: list[Partition],
        total_bytes: int,
        width: float,
        *,
        height: float = 17,
    ) -> None:
        super().__init__()
        self.width = width
        self.height = height
        self._partitions = partitions
        self._total = max(1, total_bytes)

    def draw(self) -> None:
        canvas = self.canv
        canvas.saveState()

        canvas.setFillColor(colors.HexColor(MIST))
        canvas.setStrokeColor(colors.HexColor(STEEL))
        canvas.setLineWidth(0.5)
        canvas.rect(0, 0, self.width, self.height, stroke=1, fill=1)

        palette = [ORANGE, CHARCOAL, STEEL, AMBER, VERDANT]
        cursor = 0.0
        for index, partition in enumerate(self._partitions):
            span = max(1.5, self.width * partition.size_bytes / self._total)
            span = min(span, self.width - cursor)
            if span <= 0:
                break

            canvas.setFillColor(colors.HexColor(palette[index % len(palette)]))
            canvas.rect(cursor, 0, span, self.height, stroke=0, fill=1)

            caption = f"{partition.display_label} {format_size(partition.size_bytes)}"
            canvas.setFont(_BODY_FONT, 5.8)
            if pdfmetrics.stringWidth(caption, _BODY_FONT, 5.8) < span - 4:
                canvas.setFillColor(colors.HexColor(PAPER))
                canvas.drawString(cursor + 2, self.height / 2 - 2, caption)

            cursor += span
            canvas.setStrokeColor(colors.HexColor(PAPER))
            canvas.setLineWidth(0.5)
            canvas.line(cursor, 0, cursor, self.height)

        canvas.setStrokeColor(colors.HexColor(STEEL))
        canvas.setLineWidth(0.5)
        canvas.rect(0, 0, self.width, self.height, stroke=1, fill=0)
        canvas.restoreState()


# --------------------------------------------------------------------------
# Page furniture
# --------------------------------------------------------------------------

def _draw_tracked_string(
    canvas,
    x_position: float,
    y_position: float,
    text: str,
    *,
    font: str,
    size: float,
    tracking: float,
) -> float:
    """Draw *text* with letterspacing, returning the width it occupied.

    ReportLab's canvas has no character-spacing setter; only a text object
    does. The wordmark is set wide, so it goes through one of those.

    The save/restore is load-bearing, not defensive tidiness: the character
    spacing a text object sets persists in the canvas graphics state after the
    text is drawn, so without it every later ``drawString`` on the page comes
    out tracked - and every ``drawRightString`` overruns its margin, because
    ``stringWidth`` does not account for the spacing it no longer knows about.
    """
    canvas.saveState()
    text_object = canvas.beginText(x_position, y_position)
    text_object.setFont(font, size)
    text_object.setCharSpace(tracking)
    text_object.textOut(text)
    canvas.drawText(text_object)
    canvas.restoreState()
    return pdfmetrics.stringWidth(text, font, size) + tracking * len(text)


def _draw_page_furniture(canvas, document, *, identifier: str) -> None:
    """Paint the header band and footer on every page."""
    canvas.saveState()

    band_bottom = PAGE_HEIGHT - _HEADER_BAND_HEIGHT
    canvas.setFillColor(colors.HexColor(CHARCOAL))
    canvas.rect(0, band_bottom, PAGE_WIDTH, _HEADER_BAND_HEIGHT, stroke=0, fill=1)

    mark_size = 24
    draw_mark(
        canvas,
        _MARGIN_X,
        band_bottom + (_HEADER_BAND_HEIGHT - mark_size) / 2,
        mark_size,
        ring_colour=PAPER,
        slash_colour=ORANGE,
    )

    canvas.setFillColor(colors.HexColor(PAPER))
    _draw_tracked_string(
        canvas,
        _MARGIN_X + mark_size + 11,
        band_bottom + 15,
        __short_name__.upper(),
        font=_BOLD_FONT,
        size=12,
        tracking=wordmark_letterspacing(12),
    )

    canvas.setFillColor(colors.HexColor(STEEL))
    canvas.setFont(_BODY_FONT, 7.4)
    canvas.drawRightString(PAGE_WIDTH - _MARGIN_X, band_bottom + 19, f"Certificate {identifier}")

    # Footer.
    canvas.setStrokeColor(colors.HexColor(MIST))
    canvas.setLineWidth(0.6)
    canvas.line(_MARGIN_X, _FOOTER_HEIGHT, PAGE_WIDTH - _MARGIN_X, _FOOTER_HEIGHT)

    canvas.setFillColor(colors.HexColor(STEEL))
    canvas.setFont(_BODY_FONT, 6.8)
    canvas.drawString(
        _MARGIN_X,
        _FOOTER_HEIGHT - 13,
        f"Produced by {__app_name__} v{__version__}  -  {__project_url__}",
    )
    canvas.drawRightString(
        PAGE_WIDTH - _MARGIN_X,
        _FOOTER_HEIGHT - 13,
        f"Page {canvas.getPageNumber()}",
    )
    canvas.restoreState()


# --------------------------------------------------------------------------
# Content sections
# --------------------------------------------------------------------------

def _organisation_rows(organisation: Organisation) -> list[tuple[str, str]]:
    """The organisation block, omitting every field the site left blank."""
    rows: list[tuple[str, str]] = []
    if organisation.name:
        rows.append(("Organisation", organisation.name))
    if organisation.department:
        rows.append(("Department", organisation.department))
    for index, line in enumerate(organisation.address_lines):
        rows.append(("Address" if index == 0 else "", line))
    if organisation.contact_email:
        rows.append(("Contact", organisation.contact_email))
    if organisation.contact_phone:
        rows.append(("Telephone", organisation.contact_phone))
    if organisation.customer:
        rows.append(("Customer", organisation.customer))
    if organisation.reference:
        rows.append(("Reference", organisation.reference))
    return rows


def _header_section(
    summary: RunSummary,
    settings: Settings,
    styles: dict[str, ParagraphStyle],
    content_width: float,
    identifier: str,
) -> list[Flowable]:
    """Title, seal, and the run identification block."""
    date_text, time_text = format_certificate_datetime(summary.finished_at)

    run_rows: list[tuple[str, str]] = [("Certificate", identifier)]
    run_rows += _organisation_rows(settings.organisation)
    if summary.operator:
        run_rows.append(("Operator", summary.operator))
    run_rows += [
        ("Computer", summary.machine),
        ("Date", date_text),
        ("Time", time_text),
    ]

    # A run that erased nothing must not be headed "ERASE CERTIFICATE" over the
    # words "1 drive(s) sanitised". Both were printed regardless of outcome,
    # with only the seal and the results table dissenting - so a failed run
    # produced a document whose headline, subtitle, drive count and page footer
    # all asserted an erasure that never happened. Filed or skimmed, it read as
    # proof of exactly the opposite of what occurred.
    #
    # The word "certificate" attests to something. It is reserved for runs
    # where every drive succeeded; anything else is a report.
    erased = sum(1 for result in summary.results if result.succeeded)
    total = len(summary.results)
    failed = total - erased

    if failed == 0:
        heading = "ERASE CERTIFICATE"
        summary_line = (
            f"{total} drive(s) sanitised - "
            f"run completed in {format_duration(summary.duration_seconds)}"
        )
    elif erased == 0:
        heading = "ERASE REPORT - NOT CERTIFIED"
        summary_line = (
            f"No drive was erased. {total} drive(s) attempted, {failed} failed - "
            f"run ended after {format_duration(summary.duration_seconds)}. "
            f"This document is not evidence of erasure."
        )
    else:
        heading = "ERASE REPORT - PARTIALLY CERTIFIED"
        summary_line = (
            f"{erased} of {total} drive(s) sanitised, {failed} failed - "
            f"run ended after {format_duration(summary.duration_seconds)}. "
            f"Only the drives marked Succeeded below were erased."
        )

    run_rows.append(("Drives erased", f"{erased} of {total}"))

    left = [
        Paragraph(heading, styles["title"]),
        Paragraph(summary_line, styles["subtitle"]),
        Spacer(1, 10),
        _field_table(run_rows, styles, label_width=78, value_width=content_width * 0.52 - 78),
    ]

    left_cell = Table([[item] for item in left], colWidths=[content_width * 0.62], hAlign="LEFT")
    left_cell.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    seal_cell = Table(
        [[_Seal(passed=summary.all_succeeded)]],
        colWidths=[content_width * 0.38],
        hAlign="RIGHT",
    )
    seal_cell.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    header = Table([[left_cell, seal_cell]], colWidths=[content_width * 0.62, content_width * 0.38])
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [header]


def _summary_table(
    summary: RunSummary,
    styles: dict[str, ParagraphStyle],
    content_width: float,
) -> list[Flowable]:
    """The table listing every drive in the run - the multi-drive answer."""
    header = ["#", "Device", "Product", "Serial number", "Capacity", "Method", "Result"]
    widths = [
        content_width * 0.04,
        content_width * 0.12,
        content_width * 0.21,
        content_width * 0.19,
        content_width * 0.10,
        content_width * 0.22,
        content_width * 0.12,
    ]

    rows = []
    colours = []
    for index, result in enumerate(summary.results, start=1):
        device = result.device
        rows.append(
            [
                str(index),
                device.path,
                device.product_name,
                device.serial or "not reported",
                format_size(device.size_bytes),
                result.method.certificate_name,
                result.result_word,
            ]
        )
        colours.append(_outcome_colour(result.succeeded))

    return [
        Paragraph("Drives in this run", styles["section"]),
        _data_table(header, rows, styles, widths, row_colours=colours),
        Spacer(1, 4),
        Paragraph(
            "Every drive listed above was processed in a single run. The detail "
            "sections that follow carry the full record for each one.",
            styles["note"],
        ),
    ]


def _attributes_rows(result: EraseResult, settings: Settings) -> list[tuple[str, str]]:
    """The certificate's Attributes block for one drive."""
    method = result.method
    if result.verification_passed is None:
        verification = "Not performed"
    elif result.verification_passed is False:
        # Never silently fold a failed verification into a percentage. This
        # line is the only place on the certificate that says the sampling
        # found surviving data.
        verification = f"FAILED - recoverable data found ({result.verification_percent:.1f}% sampled)"
    elif result.verification_percent >= 100:
        verification = "100% (whole device)"
    else:
        verification = f"{result.verification_percent:.1f}% of the device, sampled"

    return [
        ("Erase Method", method.certificate_name),
        ("Standard", method.standard or "Vendor command"),
        ("Passes", str(method.pass_count) if method.pass_count else "1 (firmware command)"),
        ("Verification", verification),
        ("Process Integrity", "Uninterrupted erase" if result.uninterrupted else "Interrupted"),
        ("Verification depth", f"{settings.erase.verification_percent:.0f}% requested"),
    ]


def _disk_rows(result: EraseResult) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The certificate's Disk Information block, split into two columns."""
    device = result.device
    left = [
        ("Name", device.name),
        ("Product Name", device.product_name),
        ("Serial Number", device.serial or "not reported"),
        ("Firmware", device.firmware or "not reported"),
        ("Platform Name", device.path),
    ]
    right = [
        ("Partitioning", device.partition_table_display),
        ("Size", f"{format_size(device.size_bytes)} ({device.size_bytes:,} bytes)"),
        ("Total Sectors", f"{device.total_sectors:,}"),
        ("Bytes per Sector", str(device.logical_sector_size)),
        ("Transport", (device.transport or device.kind).upper()),
    ]
    return left, right


def _results_rows(result: EraseResult) -> list[tuple[str, str]]:
    """The certificate's Results block for one drive."""
    return [
        ("Erase Range", "Whole disk"),
        ("Started at", result.started_at.strftime("%d/%m/%Y %H:%M:%S")),
        ("Finished at", result.finished_at.strftime("%d/%m/%Y %H:%M:%S")),
        ("Duration", format_duration(result.duration_seconds)),
        ("Errors", result.errors_word),
        ("Result", result.result_word),
    ]


def _pass_list(result: EraseResult, styles: dict[str, ParagraphStyle], width: float) -> Table:
    """The Erase Passes column, with each outcome coloured independently."""
    data = [[Paragraph("Erase Passes", styles["subsection"])]]
    if not result.passes:
        data.append([Paragraph("No passes recorded", styles["cell"])])
    for entry in result.passes:
        label, outcome = entry.certificate_line
        colour = _outcome_colour(entry.succeeded)
        detail = f" <font color='{STEEL}'>- {_escape(entry.detail)}</font>" if entry.detail else ""
        data.append(
            [
                Paragraph(
                    f"{_escape(label)} - <font color='{colour}'><b>{outcome}</b></font>{detail}",
                    styles["cell"],
                )
            ]
        )

    table = Table(data, colWidths=[width], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    return table


def _drive_section(
    result: EraseResult,
    index: int,
    total: int,
    settings: Settings,
    styles: dict[str, ParagraphStyle],
    content_width: float,
) -> list[Flowable]:
    """One drive's full detail block."""
    device = result.device
    half = (content_width - 20) / 2
    label_width = 82
    value_width = half - label_width

    heading = Paragraph(
        f"Drive {index} of {total} - {_escape(device.path)} "
        f"({_escape(device.serial or 'no serial')})",
        styles["section"],
    )

    # The heading, the attributes and the disk information are kept on one page
    # together: a certificate that splits a drive's identity from its heading
    # is easy to misread when the pages are separated.
    story: list[Flowable] = [
        KeepTogether(
            [
                heading,
                Paragraph("Attributes", styles["subsection"]),
                _field_table(
                    _attributes_rows(result, settings),
                    styles,
                    label_width=label_width + 20,
                    value_width=content_width - label_width - 20,
                ),
            ]
        )
    ]

    disk_left, disk_right = _disk_rows(result)
    story.append(
        KeepTogether(
            [
                Paragraph("Disk Information", styles["subsection"]),
                _two_column(
                    _field_table(disk_left, styles, label_width=label_width, value_width=value_width),
                    _field_table(disk_right, styles, label_width=label_width, value_width=value_width),
                    content_width,
                ),
            ]
        )
    )

    results_column = _field_table(
        _results_rows(result),
        styles,
        label_width=label_width,
        value_width=value_width,
        value_colours={"Result": _outcome_colour(result.succeeded)},
    )
    # The heading travels with its table; a "Results" heading stranded at the
    # foot of a page is the classic way a certificate reads as truncated.
    story.append(
        KeepTogether(
            [
                Paragraph("Results", styles["subsection"]),
                _two_column(results_column, _pass_list(result, styles, half), content_width),
            ]
        )
    )

    if result.error_messages:
        story.append(Spacer(1, 4))
        for message in result.error_messages:
            story.append(
                Paragraph(
                    f"<font color='{SIGNAL}'><b>Error:</b></font> {_escape(message)}",
                    styles["cell"],
                )
            )

    if settings.certificate.include_prior_layout and device.partitions:
        story += [
            Paragraph("Partition layout before erasure", styles["subsection"]),
            _PartitionMap(device.partitions, device.size_bytes, content_width),
            Spacer(1, 5),
            _data_table(
                ["Partition", "Type", "Filesystem", "Label", "Size"],
                [
                    [
                        partition.path,
                        partition.part_type_name or "-",
                        partition.fstype or "-",
                        partition.label or "-",
                        format_size(partition.size_bytes),
                    ]
                    for partition in device.partitions
                ],
                styles,
                [
                    content_width * 0.22,
                    content_width * 0.26,
                    content_width * 0.16,
                    content_width * 0.20,
                    content_width * 0.16,
                ],
            ),
        ]

    return story


def _host_section(
    summary: RunSummary,
    styles: dict[str, ParagraphStyle],
    content_width: float,
) -> list[Flowable]:
    """System Information and Hardware Information, side by side."""
    half = (content_width - 20) / 2
    label_width = 92
    value_width = half - label_width

    system = Table(
        [
            [Paragraph("System Information", styles["subsection"])],
            [
                _field_table(
                    list(summary.system_info.items()),
                    styles,
                    label_width=label_width,
                    value_width=value_width,
                )
            ],
        ],
        colWidths=[half],
        hAlign="LEFT",
    )
    hardware = Table(
        [
            [Paragraph("Hardware Information", styles["subsection"])],
            [
                _field_table(
                    list(summary.hardware_info.items()),
                    styles,
                    label_width=label_width,
                    value_width=value_width,
                )
            ],
        ],
        colWidths=[half],
        hAlign="LEFT",
    )
    for table in (system, hardware):
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )

    return [
        Paragraph("Host on which the erasure was performed", styles["section"]),
        _two_column(system, hardware, content_width),
    ]


def _command_appendix(
    summary: RunSummary,
    styles: dict[str, ParagraphStyle],
    content_width: float,
) -> list[Flowable]:
    """Every command issued, verbatim - the audit appendix."""
    if not any(result.commands for result in summary.results):
        return []

    story: list[Flowable] = [
        PageBreak(),
        Paragraph("Appendix - commands issued", styles["section"]),
        Paragraph(
            "Every command this run issued against a drive, in the order it was issued. "
            "The application log holds the same record with timestamps and exit codes.",
            styles["note"],
        ),
        Spacer(1, 6),
    ]

    for result in summary.results:
        if not result.commands:
            continue
        story.append(
            Paragraph(
                f"{_escape(result.device.path)} - {_escape(result.device.serial or 'no serial')}",
                styles["subsection"],
            )
        )
        for command in result.commands:
            story.append(Paragraph(_escape(command), styles["mono"]))
        story.append(Spacer(1, 5))

    return story


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def render_certificate(
    summary: RunSummary,
    settings: Settings,
    output_path: Path,
) -> Path:
    """Render *summary* to a PDF at *output_path* and return the path."""
    styles = _styles()
    identifier = certificate_id(summary)
    content_width = PAGE_WIDTH - 2 * _MARGIN_X

    output_path.parent.mkdir(parents=True, exist_ok=True)

    document = BaseDocTemplate(
        str(output_path),
        pagesize=PAGE_SIZE,
        title=f"Erase Certificate {identifier}",
        author=__app_name__,
        subject=(
            f"{sum(1 for item in summary.results if item.succeeded)} of "
            f"{len(summary.results)} drive(s) sanitised on {summary.machine}"
        ),
        creator=f"{__app_name__} {__version__} ({__project_url__})",
        leftMargin=_MARGIN_X,
        rightMargin=_MARGIN_X,
        topMargin=_HEADER_BAND_HEIGHT + _CONTENT_TOP_GAP,
        bottomMargin=_FOOTER_HEIGHT + 10,
    )
    frame = Frame(
        _MARGIN_X,
        _FOOTER_HEIGHT + 10,
        content_width,
        PAGE_HEIGHT - (_HEADER_BAND_HEIGHT + _CONTENT_TOP_GAP) - (_FOOTER_HEIGHT + 10),
        id="body",
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    document.addPageTemplates(
        [
            PageTemplate(
                id="certificate",
                frames=[frame],
                onPage=lambda canvas, doc: _draw_page_furniture(canvas, doc, identifier=identifier),
            )
        ]
    )

    story: list[Flowable] = []
    story += _header_section(summary, settings, styles, content_width, identifier)
    story += _summary_table(summary, styles, content_width)

    total = len(summary.results)
    for index, result in enumerate(summary.results, start=1):
        story += _drive_section(result, index, total, settings, styles, content_width)

    story += _host_section(summary, styles, content_width)

    if settings.certificate.include_command_log:
        story += _command_appendix(summary, styles, content_width)

    document.build(story)
    return output_path


def write_json_sidecar(summary: RunSummary, settings: Settings, output_path: Path) -> Path:
    """Write the machine-readable twin of the certificate.

    The PDF is the record a person files; this is what an asset-management
    system ingests. Every field on the certificate appears here, including the
    complete serial list that the filename may have abbreviated.
    """
    payload = {
        "certificate_id": certificate_id(summary),
        "generator": {"name": __app_name__, "version": __version__},
        # The organisation dataclass uses __slots__, so asdict is the way in.
        # Blank fields are dropped rather than emitted empty, matching how the
        # PDF omits them.
        "organisation": {
            key: value
            for key, value in asdict(settings.organisation).items()
            if value not in ("", [], None)
        },
        "operator": summary.operator,
        "machine": summary.machine,
        "started_at": summary.started_at.isoformat(),
        "finished_at": summary.finished_at.isoformat(),
        "duration_seconds": round(summary.duration_seconds, 3),
        "outcome": summary.outcome_word,
        "serials": summary.serials,
        "system_info": summary.system_info,
        "hardware_info": summary.hardware_info,
        "drives": [
            {
                "path": result.device.path,
                "product": result.device.product_name,
                "serial": result.device.serial,
                "firmware": result.device.firmware,
                "size_bytes": result.device.size_bytes,
                "logical_sector_size": result.device.logical_sector_size,
                "transport": result.device.transport or str(result.device.kind),
                "partition_table": result.device.partition_table_display,
                "method": {
                    "key": result.method.key,
                    "name": result.method.certificate_name,
                    "standard": result.method.standard,
                    "passes": result.method.pass_count,
                },
                "state": str(result.state),
                "result": result.result_word,
                "started_at": result.started_at.isoformat(),
                "finished_at": result.finished_at.isoformat(),
                "duration_seconds": round(result.duration_seconds, 3),
                "bytes_written": result.bytes_written,
                "verification": {
                    "passed": result.verification_passed,
                    "percent_of_device": round(result.verification_percent, 3),
                },
                "passes": [
                    {"label": entry.label, "succeeded": entry.succeeded, "detail": entry.detail}
                    for entry in result.passes
                ],
                "errors": result.error_messages,
                "commands": result.commands,
            }
            for result in summary.results
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output_path
