"""One drive in the device list.

Each row carries everything needed to decide whether this is the right drive
and what should be done to it:

* a selection check box, insensitive for protected drives;
* the device path, product name, serial number and capacity;
* the partition map, so the drive is recognisable by its contents;
* a method chooser listing only the methods *this* drive supports;
* an information button explaining what it does not support, and why.

Protected drives are shown, never hidden. An operator who cannot see the system
disk in the list will assume the tool failed to detect it and start looking for
a way to make it appear; one who sees it greyed out with "mounted at /" beside
it understands immediately.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GObject, Gtk  # noqa: E402

from ..erase.methods import availability_for, method_caution, recommended_method  # noqa: E402
from ..models import Device, DeviceKind, EraseMethod, format_size  # noqa: E402
from . import icons  # noqa: E402
from .partition_bar import PartitionBar  # noqa: E402

#: Icon candidates per transport, from the standard icon theme so no image
#: assets ship in the package. Each entry is a preference list resolved against
#: the running theme - see :mod:`zeroize.ui.icons` for why that is necessary.
_ICON_FOR_KIND = {
    DeviceKind.NVME: icons.DRIVE_NVME,
    DeviceKind.ATA: icons.DRIVE_DISK,
    DeviceKind.SCSI: icons.DRIVE_DISK,
    DeviceKind.USB: icons.DRIVE_USB,
    DeviceKind.MMC: icons.DRIVE_FLASH,
    DeviceKind.VIRTUAL: icons.DRIVE_VIRTUAL,
    DeviceKind.UNKNOWN: icons.DRIVE_DISK,
}


class DeviceRow(Gtk.ListBoxRow):
    """A selectable drive, with its layout and its method chooser."""

    __gsignals__: ClassVar[dict] = {
        # Emitted when the check box or the method chooser changes, so the
        # window can update the header count and the Erase button.
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        device: Device,
        *,
        on_show_capabilities: Callable[[Device], None] | None = None,
        on_unfreeze: Callable[[Device], None] | None = None,
        method_size_group: Gtk.SizeGroup | None = None,
    ) -> None:
        super().__init__()
        self.device = device
        self._on_show_capabilities = on_show_capabilities
        self._on_unfreeze = on_unfreeze
        self._method_size_group = method_size_group
        self._methods: list[EraseMethod] = [
            verdict.method for verdict in availability_for(device) if verdict.supported
        ]

        self.set_activatable(False)
        self.add_css_class("zeroize-device-row")

        container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        container.set_margin_top(10)
        container.set_margin_bottom(10)
        container.set_margin_start(12)
        container.set_margin_end(12)
        self.set_child(container)

        self._check = Gtk.CheckButton()
        self._check.set_valign(Gtk.Align.START)
        self._check.set_sensitive(device.can_be_erased and bool(self._methods))
        self._check.connect("toggled", lambda _button: self.emit("selection-changed"))
        container.append(self._check)

        icon = Gtk.Image.new_from_icon_name(
            icons.resolve(*_ICON_FOR_KIND.get(device.kind, icons.DRIVE_DISK))
        )
        icon.set_pixel_size(32)
        icon.set_valign(Gtk.Align.START)
        container.append(icon)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        details.set_hexpand(True)
        container.append(details)

        details.append(self._build_title_line())
        details.append(self._build_subtitle_line())

        self._partition_bar = PartitionBar()
        self._partition_bar.set_layout(device.partitions, device.size_bytes)
        details.append(self._partition_bar)

        status = self._build_status_line()
        if status is not None:
            details.append(status)

        # The caution lives in the details column, full width, rather than
        # beneath the drop-down. It is a note about the drive and the chosen
        # method, so it reads better here - and a wrapping label inside the
        # method column made that column's natural width vary from row to row,
        # which a size group cannot reconcile: the width each row asks for
        # depends on where its particular text happens to wrap.
        self._caution_label = Gtk.Label(xalign=0)
        self._caution_label.add_css_class("caption")
        self._caution_label.add_css_class("warning")
        self._caution_label.set_wrap(True)
        self._caution_label.set_visible(False)
        details.append(self._caution_label)

        method_box = self._build_method_box()
        # Every row's method column is given the same width, so the drop-downs
        # line up down the list. Without this each row sizes independently and
        # the column comes out ragged, because the drive names and product
        # strings to its left are all different lengths.
        if self._method_size_group is not None:
            self._method_size_group.add_widget(method_box)
        container.append(method_box)

    # ------------------------------------------------------------- building

    def _build_title_line(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        path_label = Gtk.Label(label=self.device.path, xalign=0)
        path_label.add_css_class("heading")
        box.append(path_label)

        product = Gtk.Label(label=self.device.product_name, xalign=0)
        product.add_css_class("dim-label")
        product.set_ellipsize(3)  # Pango.EllipsizeMode.END
        product.set_hexpand(True)
        box.append(product)

        size = Gtk.Label(label=format_size(self.device.size_bytes), xalign=1)
        size.add_css_class("heading")
        box.append(size)

        return box

    def _build_subtitle_line(self) -> Gtk.Widget:
        parts = [
            f"Serial {self.device.serial}" if self.device.serial else "No serial reported",
            (self.device.transport or str(self.device.kind)).upper(),
            self.device.partition_table_display,
        ]
        if self.device.rotational:
            parts.append("Rotational")
        if self.device.removable:
            parts.append("Removable")
        if self.device.simulated:
            parts.append("SIMULATED")

        label = Gtk.Label(label="   ".join(parts), xalign=0)
        label.add_css_class("dim-label")
        label.add_css_class("caption")
        return label

    def _build_status_line(self) -> Gtk.Widget | None:
        """The warning strip: protection, mounts, or a flash-overwrite caution."""
        messages: list[tuple[str, str]] = []

        error_icon = icons.resolve(*icons.ERROR)
        warning_icon = icons.resolve(*icons.WARNING)

        if self.device.protection_reason:
            messages.append((error_icon, f"Protected - {self.device.protection_reason}"))
        elif self.device.is_mounted:
            messages.append(
                (warning_icon, "Has mounted filesystems - they will be unmounted first")
            )

        if not self._methods and self.device.can_be_erased:
            messages.append((error_icon, "No erase method is available for this drive"))

        # A drive whose firmware erase is blocked is the one warning an operator
        # can act on from here, so it gets a button rather than a sentence
        # telling them to open a terminal. Never automatic during a scan:
        # clearing it disturbs the bus, or in the NVMe case the whole machine,
        # which a destructive tool should not do unasked.
        #
        # Two different conditions with two different remedies:
        #
        #   ATA  - the firmware issued SECURITY FREEZE LOCK at POST. Detaching
        #          and re-attaching that one drive usually clears it.
        #   NVMe - the controller answers Sanitize with Access Denied although
        #          SANICAP advertises it. A bus reset does not clear this; only
        #          suspending the machine briefly does, and that affects every
        #          drive attached.
        #
        # Computed together because the button is the same button, and kept
        # apart because the sentences beside it must not be.
        ata_frozen = (
            self.device.ata is not None
            and self.device.ata.frozen
            and self.device.can_be_erased
        )
        nvme_blocked = (
            self.device.nvme is not None
            and self.device.nvme.sanitize_blocked
            and self.device.can_be_erased
        )
        frozen = ata_frozen or nvme_blocked

        if ata_frozen:
            messages.append(
                (
                    warning_icon,
                    "Frozen by the system firmware - Secure Erase is blocked until it is cleared",
                )
            )
        elif nvme_blocked:
            messages.append(
                (
                    warning_icon,
                    "The controller is refusing Sanitize - the firmware erase is "
                    "blocked until it is unstuck",
                )
            )

        if not messages and not frozen:
            return None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for icon_name, text in messages:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.add_css_class("warning" if icon_name == warning_icon else "error")
            line.append(icon)
            label = Gtk.Label(label=text, xalign=0)
            label.add_css_class("caption")
            label.set_wrap(True)
            line.append(label)
            box.append(line)

        if frozen and self._on_unfreeze is not None:
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            actions.set_margin_top(4)

            # The label says what it costs. "Try to unfreeze" is honest for the
            # ATA case, where one drive is detached and the rest of the machine
            # carries on; it would not be for the NVMe case, where the machine
            # sleeps. An operator should be able to tell those apart before
            # pressing, not from the dialog that follows.
            if nvme_blocked:
                label = "Unfreeze (suspends this machine)"
                tooltip = (
                    "Suspend this machine for about three seconds and wake it "
                    "again. That is the only thing that clears a controller "
                    "refusing Sanitize - a bus reset does not. Every drive "
                    "attached is affected, and no erase may be running. You "
                    "will be asked to confirm."
                )
            else:
                label = "Try to unfreeze"
                tooltip = (
                    "Detach and re-attach the drive to clear the firmware's "
                    "security freeze, then rescan the bus. Leaves the rest of "
                    "the machine alone. The drive may come back under a "
                    "different name."
                )

            self._unfreeze_button = Gtk.Button(label=label)
            self._unfreeze_button.set_tooltip_text(tooltip)
            self._unfreeze_button.connect(
                "clicked", lambda _button: self._on_unfreeze(self.device)
            )
            actions.append(self._unfreeze_button)
            box.append(actions)

        return box

    def _build_method_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_valign(Gtk.Align.CENTER)
        box.set_size_request(320, -1)
        box.set_hexpand(False)

        caption = Gtk.Label(label="Erase method", xalign=0)
        caption.add_css_class("caption")
        caption.add_css_class("dim-label")
        box.append(caption)

        chooser = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        self._method_model = Gtk.StringList()
        for method in self._methods:
            self._method_model.append(method.certificate_name)
        if not self._methods:
            self._method_model.append("None available")

        self._method_drop_down = Gtk.DropDown(model=self._method_model)
        self._method_drop_down.set_hexpand(True)
        self._method_drop_down.set_sensitive(bool(self._methods) and self.device.can_be_erased)

        preferred = recommended_method(self.device)
        if preferred is not None:
            for index, method in enumerate(self._methods):
                if method.key == preferred.key:
                    self._method_drop_down.set_selected(index)
                    break

        # Connected after the initial selection, so construction does not emit
        # a selection-changed the window would count as an operator action.
        self._method_drop_down.connect("notify::selected", self._on_method_changed)
        chooser.append(self._method_drop_down)

        info = Gtk.Button(icon_name=icons.resolve("help-about-symbolic", "dialog-question-symbolic"))
        info.add_css_class("flat")
        info.set_tooltip_text("Show every method and why the unavailable ones cannot be used")
        info.connect("clicked", self._on_info_clicked)
        chooser.append(info)

        box.append(chooser)
        self._refresh_caution()

        return box

    # -------------------------------------------------------------- signals

    def _on_method_changed(self, *_args) -> None:
        self._refresh_caution()
        self.emit("selection-changed")

    def _on_info_clicked(self, _button: Gtk.Button) -> None:
        if self._on_show_capabilities is not None:
            self._on_show_capabilities(self.device)

    def _refresh_caution(self) -> None:
        """Show whatever caution applies to the currently chosen method."""
        method = self.selected_method
        caution = method_caution(self.device, method) if method is not None else ""
        self._caution_label.set_visible(bool(caution))
        if caution:
            self._caution_label.set_text(caution)

    # --------------------------------------------------------------- public

    @property
    def is_selected(self) -> bool:
        return self._check.get_active()

    def set_selected(self, selected: bool) -> None:
        if self._check.get_sensitive():
            self._check.set_active(selected)

    @property
    def selected_method(self) -> EraseMethod | None:
        if not self._methods:
            return None
        index = self._method_drop_down.get_selected()
        if index == Gtk.INVALID_LIST_POSITION or index >= len(self._methods):
            return None
        return self._methods[index]

    def try_set_method(self, method_key: str) -> bool:
        """Select *method_key* if this drive supports it. Used by Apply to all."""
        for index, method in enumerate(self._methods):
            if method.key == method_key:
                self._method_drop_down.set_selected(index)
                self._refresh_caution()
                return True
        return False

    def set_busy(self, busy: bool) -> None:
        """Lock the row's controls while a run is in progress."""
        self._check.set_sensitive(not busy and self.device.can_be_erased and bool(self._methods))
        self._method_drop_down.set_sensitive(
            not busy and bool(self._methods) and self.device.can_be_erased
        )


def build_capability_text(device: Device) -> str:
    """A plain-text report of every method and its verdict for *device*.

    Shown in the row's information dialog. It deliberately quotes the raw
    capability registers for NVMe drives: when a drive refuses a command it
    claimed to support, those four hex values are the first thing worth having.
    """
    lines: list[str] = [f"{device.path} - {device.product_name}"]
    if device.serial:
        lines.append(f"Serial {device.serial}")

    if device.nvme is not None:
        capabilities = device.nvme
        lines += [
            "",
            "NVMe capability registers",
            f"  OACS    0x{capabilities.raw_oacs:04x}   Format NVM supported: {capabilities.format_supported}",
            f"  FNA     0x{capabilities.raw_fna:02x}     Crypto erase supported: {capabilities.crypto_erase_supported}",
            f"  SANICAP 0x{capabilities.raw_sanicap:08x}",
            f"          crypto={capabilities.sanitize_crypto_supported} "
            f"block={capabilities.sanitize_block_supported} "
            f"overwrite={capabilities.sanitize_overwrite_supported}",
        ]

    if device.ata is not None:
        security = device.ata
        lines += [
            "",
            "ATA security feature set",
            f"  supported={security.supported} enabled={security.enabled} "
            f"frozen={security.frozen} locked={security.locked}",
            f"  enhanced erase={security.enhanced_erase_supported} "
            f"estimate={security.estimated_minutes} min",
        ]

    lines += ["", "Methods"]
    for verdict in availability_for(device):
        if verdict.supported:
            lines.append(f"  [available]   {verdict.method.certificate_name}")
        else:
            lines.append(f"  [unavailable] {verdict.method.certificate_name}")
            lines.append(f"                {verdict.reason}")

    return "\n".join(lines)
