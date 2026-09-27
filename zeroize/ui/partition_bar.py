"""The proportional partition map drawn on each drive row.

A drive is identified in practice by what is on it. An operator handed three
identical 500 GB disks tells them apart by "the one with the Windows partition
and the recovery slice", not by the serial number printed on a label they
cannot read without a torch. So the row shows the layout as a bar, in
proportion, with the labels written into the segments that are wide enough to
hold them.

Segments narrower than :data:`_MIN_SEGMENT_WIDTH` are still drawn at that
minimum width. An EFI system partition is roughly a five-thousandth of a modern
drive and would otherwise be invisible - which is exactly the partition whose
presence tells the operator this is a boot disk.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from ..branding import AMBER, CHARCOAL, MIST, ORANGE, STEEL, VERDANT, rgb  # noqa: E402
from ..models import Partition, format_size  # noqa: E402

#: Colours cycled through for successive partitions.
_SEGMENT_PALETTE = (ORANGE, CHARCOAL, STEEL, AMBER, VERDANT)

#: Narrowest a segment may be drawn, in logical pixels.
_MIN_SEGMENT_WIDTH = 3.0

#: A caption is only drawn when its segment has room for it plus this padding.
_CAPTION_PADDING = 8.0


class PartitionBar(Gtk.DrawingArea):
    """A single-row proportional bar showing a drive's partition layout."""

    def __init__(self, height: int = 22) -> None:
        super().__init__()
        self._partitions: list[Partition] = []
        self._total_bytes = 1
        self._unpartitioned_label = "Unpartitioned"

        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._on_draw)
        self.add_css_class("zeroize-partition-bar")

    def set_layout(self, partitions: list[Partition], total_bytes: int) -> None:
        """Set the partitions to draw and the drive's total capacity."""
        self._partitions = list(partitions)
        self._total_bytes = max(1, total_bytes)
        self.queue_draw()

    def _on_draw(self, _area: Gtk.DrawingArea, context, width: int, height: int) -> None:
        """Paint the bar. Called by GTK with a Cairo context."""
        # Ground.
        context.set_source_rgb(*rgb(MIST))
        context.rectangle(0, 0, width, height)
        context.fill()

        if not self._partitions:
            self._draw_empty(context, width, height)
            self._draw_border(context, width, height)
            return

        cursor = 0.0
        for index, partition in enumerate(self._partitions):
            proportional = width * partition.size_bytes / self._total_bytes
            span = max(_MIN_SEGMENT_WIDTH, proportional)
            span = min(span, width - cursor)
            if span <= 0:
                break

            colour = _SEGMENT_PALETTE[index % len(_SEGMENT_PALETTE)]
            context.set_source_rgb(*rgb(colour))
            context.rectangle(cursor, 0, span, height)
            context.fill()

            self._draw_caption(context, partition, cursor, span, height)

            cursor += span

            # A hairline between segments, so two adjacent segments of similar
            # colour still read as two partitions.
            context.set_source_rgb(1, 1, 1)
            context.set_line_width(1)
            context.move_to(cursor, 0)
            context.line_to(cursor, height)
            context.stroke()

        self._draw_border(context, width, height)

    def _draw_caption(
        self,
        context,
        partition: Partition,
        start: float,
        span: float,
        height: float,
    ) -> None:
        """Write the partition's label into its segment, if it fits."""
        caption = f"{partition.display_label}  {format_size(partition.size_bytes)}"
        context.select_font_face("Sans")
        context.set_font_size(9)
        extents = context.text_extents(caption)
        if extents.width + _CAPTION_PADDING > span:
            # Try the label alone before giving up on a caption entirely.
            caption = partition.display_label
            extents = context.text_extents(caption)
            if extents.width + _CAPTION_PADDING > span:
                return

        context.set_source_rgb(1, 1, 1)
        context.move_to(start + 4, height / 2 + extents.height / 2)
        context.show_text(caption)

    def _draw_empty(self, context, width: int, height: int) -> None:
        """Caption for a drive with no partition table."""
        context.set_source_rgb(*rgb(STEEL))
        context.select_font_face("Sans")
        context.set_font_size(9)
        extents = context.text_extents(self._unpartitioned_label)
        context.move_to((width - extents.width) / 2, height / 2 + extents.height / 2)
        context.show_text(self._unpartitioned_label)

    def _draw_border(self, context, width: int, height: int) -> None:
        context.set_source_rgb(*rgb(STEEL))
        context.set_line_width(1)
        context.rectangle(0.5, 0.5, width - 1, height - 1)
        context.stroke()
