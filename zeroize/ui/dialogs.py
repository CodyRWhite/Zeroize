"""Dialogs: the destructive confirmation, and the capability report.

The confirmation dialog is the most important widget in the application. It is
the last thing between an operator and an irreversible act, and it is built
around three ideas:

* **Say exactly what will happen, per drive.** Not "3 drives will be erased"
  but the path, the product, the serial and the method for each one. The serial
  matters most: it is the only field that identifies the physical object, and
  ``/dev/sdb`` can mean a different drive than it did ten minutes ago.
* **Make the confirmation cost a moment's thought.** The operator types a
  phrase. A second "Are you sure?" button teaches people to click twice
  without reading; typing ``ERASE`` does not.
* **Never make it the only interlock.** The engine re-checks everything after
  this dialog closes. This exists to catch the honest mistake, not to be the
  last line of defence.

These are built from plain ``Gtk`` widgets rather than ``Adw.MessageDialog`` or
``Adw.AlertDialog``. Both of those are pleasant and both landed in libadwaita
versions newer than the oldest target distribution ships, and a confirmation
dialog that fails to construct on one of the four supported platforms is worse
than one that looks slightly less native on all of them.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from ..config import Settings, validate_operator_name  # noqa: E402
from ..models import Device, EraseMethod, format_size  # noqa: E402
from .device_row import build_capability_text  # noqa: E402


def _dialog_shell(parent: Gtk.Window, title: str, width: int = 620) -> tuple[Gtk.Window, Gtk.Box]:
    """A modal window with a header bar, returning it and its content box."""
    window = Gtk.Window(transient_for=parent, modal=True, title=title)
    window.set_default_size(width, -1)
    window.set_hide_on_close(False)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    window.set_child(outer)

    header = Adw.HeaderBar()
    header.set_title_widget(Adw.WindowTitle(title=title))
    outer.append(header)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    content.set_margin_top(18)
    content.set_margin_bottom(18)
    content.set_margin_start(18)
    content.set_margin_end(18)
    outer.append(content)

    return window, content


def show_capability_dialog(parent: Gtk.Window, device: Device) -> None:
    """Show every method for *device* and the reason for each verdict."""
    window, content = _dialog_shell(parent, f"Capabilities - {device.path}", width=680)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroller.set_min_content_height(420)
    scroller.set_vexpand(True)

    text_view = Gtk.TextView()
    text_view.set_editable(False)
    text_view.set_monospace(True)
    text_view.set_cursor_visible(False)
    text_view.set_left_margin(10)
    text_view.set_right_margin(10)
    text_view.set_top_margin(10)
    text_view.set_bottom_margin(10)
    text_view.get_buffer().set_text(build_capability_text(device))
    scroller.set_child(text_view)
    content.append(scroller)

    close = Gtk.Button(label="Close")
    close.set_halign(Gtk.Align.END)
    close.connect("clicked", lambda _button: window.close())
    content.append(close)

    window.present()


def show_message(parent: Gtk.Window, heading: str, body: str, *, error: bool = False) -> None:
    """A simple acknowledgement dialog."""
    window, content = _dialog_shell(parent, heading, width=480)

    icon = Gtk.Image.new_from_icon_name(
        "dialog-error-symbolic" if error else "dialog-information-symbolic"
    )
    icon.set_pixel_size(40)
    if error:
        icon.add_css_class("error")
    content.append(icon)

    label = Gtk.Label(label=body)
    label.set_wrap(True)
    label.set_justify(Gtk.Justification.CENTER)
    content.append(label)

    close = Gtk.Button(label="Close")
    close.set_halign(Gtk.Align.CENTER)
    close.connect("clicked", lambda _button: window.close())
    content.append(close)

    window.present()


def confirm_action(
    parent: Gtk.Window,
    heading: str,
    body: str,
    *,
    confirm_label: str = "Continue",
    on_confirm=None,
) -> None:
    """A two-button confirmation for something disruptive but not destructive.

    Distinct from :class:`ConfirmEraseDialog`, which demands a typed phrase
    because it gates data loss. This one gates an *interruption* - the screen
    going blank, the machine suspending - where the operator needs to know what
    is about to happen but the cost of a mistaken click is a few seconds, not a
    drive.
    """
    window, content = _dialog_shell(parent, heading, width=520)

    icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
    icon.set_pixel_size(40)
    content.append(icon)

    label = Gtk.Label(label=body)
    label.set_wrap(True)
    label.set_justify(Gtk.Justification.CENTER)
    content.append(label)

    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    buttons.set_halign(Gtk.Align.CENTER)

    cancel = Gtk.Button(label="Cancel")
    cancel.connect("clicked", lambda _button: window.close())
    buttons.append(cancel)

    proceed = Gtk.Button(label=confirm_label)
    proceed.add_css_class("suggested-action")

    def _go(_button) -> None:
        window.close()
        if on_confirm is not None:
            on_confirm()

    proceed.connect("clicked", _go)
    buttons.append(proceed)

    content.append(buttons)
    window.present()


class ConfirmEraseDialog:
    """The typed-confirmation gate in front of an erase run."""

    def __init__(
        self,
        parent: Gtk.Window,
        assignments: list[tuple[Device, EraseMethod]],
        settings: Settings,
        *,
        default_operator: str = "",
        on_confirmed: Callable[[str], None],
    ) -> None:
        self._assignments = assignments
        self._settings = settings
        self._on_confirmed = on_confirmed
        self._phrase = settings.safety.confirmation_phrase

        drive_count = len(assignments)
        self._window, content = _dialog_shell(
            parent,
            f"Erase {drive_count} drive{'s' if drive_count != 1 else ''}?",
            width=720,
        )

        content.append(self._build_warning(drive_count))
        content.append(self._build_drive_list())

        self._operator_entry = Gtk.Entry()
        self._confirm_entry = Gtk.Entry()
        content.append(self._build_inputs(default_operator))

        content.append(self._build_buttons())
        self._refresh_ready()

    # ------------------------------------------------------------- building

    def _build_warning(self, drive_count: int) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        icon.set_pixel_size(38)
        icon.set_valign(Gtk.Align.START)
        icon.add_css_class("error")
        box.append(icon)

        total_bytes = sum(device.size_bytes for device, _ in self._assignments)
        text = Gtk.Label(xalign=0)
        text.set_wrap(True)
        text.set_markup(
            f"<b>This cannot be undone.</b>\n"
            f"{drive_count} drive{'s' if drive_count != 1 else ''} totalling "
            f"{format_size(total_bytes)} will be erased. Check every serial number "
            f"below against the physical drives before continuing."
        )
        box.append(text)
        return box

    def _build_drive_list(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(min(280, 64 * max(1, len(self._assignments))))
        scroller.set_vexpand(True)

        listing = Gtk.ListBox()
        listing.set_selection_mode(Gtk.SelectionMode.NONE)
        listing.add_css_class("boxed-list")

        for device, method in self._assignments:
            row = Adw.ActionRow()
            row.set_title(f"{device.path} - {device.product_name}")
            row.set_subtitle(
                f"Serial {device.serial or 'not reported'}   "
                f"{format_size(device.size_bytes)}   {method.certificate_name}"
            )
            row.set_subtitle_lines(2)

            if device.is_mounted:
                badge = Gtk.Label(label="will be unmounted")
                badge.add_css_class("caption")
                badge.add_css_class("warning")
                row.add_suffix(badge)

            if method.non_interruptible:
                badge = Gtk.Label(label="cannot be cancelled")
                badge.add_css_class("caption")
                badge.add_css_class("dim-label")
                row.add_suffix(badge)

            listing.append(row)

        scroller.set_child(listing)
        return scroller

    def _build_inputs(self, default_operator: str) -> Gtk.Widget:
        group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        if self._settings.safety.require_operator_name:
            operator_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            label = Gtk.Label(label="Operator name (printed on the certificate)", xalign=0)
            label.add_css_class("caption")
            operator_box.append(label)

            # Deliberately NOT pre-filled. The field used to default to the
            # login name, which on the live image is the appliance account
            # "zeroize" - a value that satisfies the not-empty check without
            # anyone having typed anything, and then prints "zeroize" as the
            # operator on a compliance document. If a name is required, it has
            # to be entered; an accepted default is not a required field.
            self._operator_entry.set_text("")
            self._operator_entry.set_placeholder_text("Your name")
            self._operator_entry.connect("changed", lambda _entry: self._refresh_ready())
            operator_box.append(self._operator_entry)
            group.append(operator_box)
        else:
            self._operator_entry.set_text(default_operator)

        if self._settings.safety.require_typed_confirmation:
            confirm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            label = Gtk.Label(xalign=0)
            label.add_css_class("caption")
            label.set_markup(f"Type <b>{self._phrase}</b> to confirm")
            confirm_box.append(label)

            self._confirm_entry.set_placeholder_text(self._phrase)
            self._confirm_entry.connect("changed", lambda _entry: self._refresh_ready())
            self._confirm_entry.connect("activate", lambda _entry: self._try_confirm())
            confirm_box.append(self._confirm_entry)
            group.append(confirm_box)

        return group

    def _build_buttons(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_halign(Gtk.Align.END)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _button: self._window.close())
        box.append(cancel)

        self._erase_button = Gtk.Button(label=f"Erase {len(self._assignments)} drive(s)")
        self._erase_button.add_css_class("destructive-action")
        self._erase_button.connect("clicked", lambda _button: self._try_confirm())
        box.append(self._erase_button)

        return box

    # -------------------------------------------------------------- signals

    def _is_ready(self) -> str:
        """Empty string when the operator may proceed, else why they may not."""
        if self._settings.safety.require_operator_name:
            problem = validate_operator_name(self._operator_entry.get_text())
            if problem:
                return problem
        if self._settings.safety.require_typed_confirmation:
            typed = self._confirm_entry.get_text().strip()
            if typed != self._phrase:
                return f"Type {self._phrase} to confirm"
        return ""

    def _refresh_ready(self) -> None:
        problem = self._is_ready()
        self._erase_button.set_sensitive(not problem)
        self._erase_button.set_tooltip_text(problem or None)

    def _try_confirm(self) -> None:
        if self._is_ready():
            return
        operator = self._operator_entry.get_text().strip()
        self._window.close()
        self._on_confirmed(operator)

    def present(self) -> None:
        self._window.present()
        if self._settings.safety.require_operator_name and not self._operator_entry.get_text():
            self._operator_entry.grab_focus()
        elif self._settings.safety.require_typed_confirmation:
            self._confirm_entry.grab_focus()
