"""The live view of a run in progress, one strip per drive.

Two things distinguish this from a generic progress screen, and both come from
the nature of the work:

**Honest indeterminacy.** ``nvme format`` and ``hdparm --security-erase``
report nothing at all until they finish. Rather than animate a bar that implies
knowledge the application does not have, those drives get a pulsing bar and an
elapsed-time counter. A sanitize, which does publish a real percentage, gets a
real bar. The difference is deliberate and visible.

**Honest cancellation.** Cancelling stops a software overwrite between blocks.
It cannot stop a firmware command - the drive is unusable until that finishes,
and no amount of interface design changes it. So the cancel button says what it
will actually achieve, and a drive running a non-interruptible method says so
on its own strip.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..erase.context import ProgressUpdate  # noqa: E402
from ..models import Device, EraseMethod, JobState, format_duration, format_size  # noqa: E402
from . import icons  # noqa: E402


class DeviceProgressStrip(Gtk.Box):
    """The progress readout for a single drive."""

    def __init__(self, device: Device, method: EraseMethod) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.device = device
        self.method = method
        self._pulsing = False
        self._pulse_source: int | None = None

        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(14)
        self.set_margin_end(14)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        title = Gtk.Label(xalign=0)
        title.set_markup(
            f"<b>{GLib.markup_escape_text(device.path)}</b>  "
            f"{GLib.markup_escape_text(device.serial or 'no serial')}  "
            f"{format_size(device.size_bytes)}"
        )
        title.set_hexpand(True)
        header.append(title)

        self._state_label = Gtk.Label(label="Waiting", xalign=1)
        self._state_label.add_css_class("dim-label")
        header.append(self._state_label)
        self.append(header)

        method_label = Gtk.Label(label=method.certificate_name, xalign=0)
        method_label.add_css_class("caption")
        method_label.add_css_class("dim-label")
        self.append(method_label)

        self._bar = Gtk.ProgressBar()
        self._bar.set_show_text(False)
        self.append(self._bar)

        self._message = Gtk.Label(label="Queued", xalign=0)
        self._message.add_css_class("caption")
        self._message.set_wrap(True)
        self.append(self._message)

        if method.non_interruptible:
            note = Gtk.Label(
                label="This method cannot be cancelled once the drive has started.",
                xalign=0,
            )
            note.add_css_class("caption")
            note.add_css_class("dim-label")
            self.append(note)

    # ------------------------------------------------------------- updating

    def apply(self, update: ProgressUpdate) -> None:
        """Apply a progress update. Must be called on the main thread."""
        self._message.set_text(update.message)
        self._state_label.set_text(self._state_text(update))

        if update.state.is_terminal:
            self._stop_pulsing()
            self._bar.set_fraction(1.0 if update.state is JobState.SUCCEEDED else 0.0)
            self._apply_terminal_style(update.state)
            return

        if update.fraction is None:
            self._start_pulsing()
        else:
            self._stop_pulsing()
            self._bar.set_fraction(max(0.0, min(1.0, update.fraction)))

    def _state_text(self, update: ProgressUpdate) -> str:
        """The right-hand status: percentage, pass count, elapsed and ETA."""
        if update.state is JobState.SUCCEEDED:
            return "Erased"
        if update.state is JobState.FAILED:
            return "Failed"
        if update.state is JobState.CANCELLED:
            return "Cancelled"

        parts: list[str] = []
        if update.state is JobState.VERIFYING:
            parts.append("Verifying")
        if update.pass_total > 1:
            parts.append(f"Pass {update.pass_index} of {update.pass_total}")
        if update.percent is not None:
            parts.append(f"{update.percent:.0f}%")
        if update.elapsed_seconds:
            parts.append(format_duration(update.elapsed_seconds))
        if update.eta_seconds is not None:
            parts.append(f"~{format_duration(update.eta_seconds)} left")
        return "   ".join(parts) or "Working"

    def _apply_terminal_style(self, state: JobState) -> None:
        for css_class in ("zeroize-success", "zeroize-failure"):
            self._bar.remove_css_class(css_class)
            self._state_label.remove_css_class(css_class)
        css_class = "zeroize-success" if state is JobState.SUCCEEDED else "zeroize-failure"
        self._bar.add_css_class(css_class)
        self._state_label.add_css_class(css_class)

    def _start_pulsing(self) -> None:
        if self._pulsing:
            return
        self._pulsing = True
        self._bar.pulse()
        self._pulse_source = GLib.timeout_add(120, self._on_pulse)

    def _on_pulse(self) -> bool:
        if not self._pulsing:
            return GLib.SOURCE_REMOVE
        self._bar.pulse()
        return GLib.SOURCE_CONTINUE

    def _stop_pulsing(self) -> None:
        self._pulsing = False
        if self._pulse_source is not None:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = None

    def dispose_timers(self) -> None:
        """Release the pulse timer. Called when the view is torn down."""
        self._stop_pulsing()


class ProgressView(Gtk.Box):
    """The whole run: a strip per drive, a summary line, and a cancel button."""

    def __init__(self, *, on_cancel: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._strips: dict[str, DeviceProgressStrip] = {}
        self._on_cancel = on_cancel

        banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        banner.set_margin_top(14)
        banner.set_margin_bottom(6)
        banner.set_margin_start(16)
        banner.set_margin_end(16)

        self._headline = Gtk.Label(xalign=0)
        self._headline.add_css_class("title-4")
        self._headline.set_hexpand(True)
        banner.append(self._headline)

        self._cancel_button = Gtk.Button(label="Stop what can be stopped")
        self._cancel_button.add_css_class("destructive-action")
        self._cancel_button.set_tooltip_text(
            "Software overwrites stop between blocks. Firmware commands already "
            "issued cannot be interrupted and will run to completion."
        )
        self._cancel_button.connect("clicked", lambda _button: self._on_cancel())
        banner.append(self._cancel_button)
        self.append(banner)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_margin_start(16)
        self._list.set_margin_end(16)
        self._list.set_margin_bottom(16)
        scroller.set_child(self._list)
        self.append(scroller)

    def begin(self, assignments: list[tuple[Device, EraseMethod]]) -> None:
        """Populate one strip per drive and reset the view."""
        self.clear()
        for device, method in assignments:
            strip = DeviceProgressStrip(device, method)
            self._strips[device.path] = strip
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_child(strip)
            self._list.append(row)
        self._cancel_button.set_sensitive(True)
        self.set_headline(f"Erasing {len(assignments)} drive(s)")

    def apply(self, update: ProgressUpdate) -> None:
        """Route an update to the right strip. Main thread only."""
        strip = self._strips.get(update.device_path)
        if strip is not None:
            strip.apply(update)

    def set_headline(self, text: str) -> None:
        self._headline.set_text(text)

    def finish(self, headline: str) -> None:
        """Mark the run finished; the cancel button no longer applies."""
        self.set_headline(headline)
        self._cancel_button.set_sensitive(False)
        for strip in self._strips.values():
            strip.dispose_timers()

    def clear(self) -> None:
        for strip in self._strips.values():
            strip.dispose_timers()
        self._strips.clear()
        child = self._list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._list.remove(child)
            child = following


class ResultView(Gtk.Box):
    """Shown once a run has finished: the outcome and the certificate."""

    def __init__(
        self,
        *,
        on_open_certificate: Callable[[], None],
        on_open_folder: Callable[[], None],
        on_back: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.set_valign(Gtk.Align.CENTER)
        self.set_margin_start(32)
        self.set_margin_end(32)

        self._status = Adw.StatusPage()
        self._status.set_vexpand(True)
        self.append(self._status)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_bottom(28)

        back = Gtk.Button(label="Back to drives")
        back.connect("clicked", lambda _button: on_back())
        buttons.append(back)

        self._folder_button = Gtk.Button(label="Open certificate folder")
        self._folder_button.connect("clicked", lambda _button: on_open_folder())
        buttons.append(self._folder_button)

        self._open_button = Gtk.Button(label="Open certificate")
        self._open_button.add_css_class("suggested-action")
        self._open_button.connect("clicked", lambda _button: on_open_certificate())
        buttons.append(self._open_button)

        self.append(buttons)

    def show_result(
        self,
        *,
        succeeded: int,
        failed: int,
        certificate_path: str,
        serials: list[str],
    ) -> None:
        all_good = failed == 0
        self._status.set_icon_name(
            icons.resolve(*(icons.SUCCESS if all_good else icons.WARNING))
        )
        self._status.set_title(
            f"{succeeded} drive(s) erased" if all_good else f"{succeeded} erased, {failed} failed"
        )

        serial_text = ", ".join(serials) if serials else "none"
        self._status.set_description(
            f"Serial numbers in this run: {serial_text}\n\nCertificate: {certificate_path}"
        )
        has_certificate = bool(certificate_path)
        self._open_button.set_sensitive(has_certificate)
        self._folder_button.set_sensitive(has_certificate)
