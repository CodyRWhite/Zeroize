"""The main window: the drive list, the run, and the result.

Three states in a ``Gtk.Stack`` - the drive list, the run in progress, and the
result - with the header bar adapting to each. The window owns the erase run
and is responsible for the one piece of threading discipline the application
needs: the engine reports progress from worker threads, and every one of those
reports is marshalled onto the main loop with ``GLib.idle_add`` before it
touches a widget. Touching GTK from a worker thread produces crashes that
appear minutes into a long job, which on this particular tool means in the
middle of an irreversible operation.

The window also carries the banner that states, at all times, what mode the
application is in. Simulated devices and dry runs must never be mistakable for
the real thing, and "I thought it was still in test mode" is the failure that
this banner exists to prevent.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import __app_name__, __short_name__, __tagline__  # noqa: E402
from ..certificate import issue_certificate  # noqa: E402
from ..config import Settings  # noqa: E402
from ..discovery import discover_devices  # noqa: E402
from ..erase.context import ProgressUpdate  # noqa: E402
from ..erase.engine import EraseRun  # noqa: E402
from ..logging_setup import get_logger  # noqa: E402
from ..models import Device, EraseMethod, RunSummary, format_size  # noqa: E402
from ..paths import invoking_user_name, invoking_user_uid  # noqa: E402
from ..process import is_dry_run  # noqa: E402
from .device_row import DeviceRow  # noqa: E402
from .dialogs import (  # noqa: E402
    ConfirmEraseDialog,
    confirm_action,
    show_capability_dialog,
    show_message,
)
from .progress_view import ProgressView, ResultView  # noqa: E402

_log = get_logger("Window")

_PAGE_DRIVES = "drives"
_PAGE_EMPTY = "empty"
_PAGE_PROGRESS = "progress"
_PAGE_RESULT = "result"


class MainWindow(Adw.ApplicationWindow):
    """The application's only window."""

    def __init__(
        self,
        application: Adw.Application,
        settings: Settings,
        *,
        simulate: bool = False,
    ) -> None:
        super().__init__(application=application)
        self._settings = settings
        self._simulate = simulate
        self._devices: list[Device] = []
        self._rows: list[DeviceRow] = []
        self._run: EraseRun | None = None
        self._certificate_path: Path | None = None
        #: Cleared after the first scan. Detaching frozen drives is worth doing
        #: once, before the operator has seen anything; repeating it on every
        #: manual rescan would fight an operator who just detached something
        #: deliberately.
        self._first_scan = True

        self.set_title(__app_name__)
        self.set_default_size(1100, 760)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        root.append(self._build_header())

        banner = self._build_mode_banner()
        if banner is not None:
            root.append(banner)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_vexpand(True)
        root.append(self._stack)

        self._build_pages()
        self.refresh_devices()

    # ------------------------------------------------------------- building

    def _build_header(self) -> Gtk.Widget:
        self._header = Adw.HeaderBar()

        self._window_title = Adw.WindowTitle(title=__short_name__, subtitle=__tagline__)
        self._header.set_title_widget(self._window_title)

        self._rescan_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self._rescan_button.set_tooltip_text("Rescan for attached drives")
        self._rescan_button.connect("clicked", lambda _button: self.refresh_devices())
        self._header.pack_start(self._rescan_button)

        self._diagnostics_button = Gtk.Button(icon_name="folder-download-symbolic")
        self._diagnostics_button.set_tooltip_text(
            "Export logs and system state to the certificate volume, for "
            "reviewing a problem on another machine"
        )
        self._diagnostics_button.connect("clicked", lambda _button: self._on_export_diagnostics())
        self._header.pack_start(self._diagnostics_button)

        self._select_all_button = Gtk.Button(label="Select all erasable")
        self._select_all_button.connect("clicked", self._on_select_all)
        self._header.pack_start(self._select_all_button)

        self._erase_button = Gtk.Button(label="Erase selected")
        self._erase_button.add_css_class("destructive-action")
        self._erase_button.set_sensitive(False)
        self._erase_button.connect("clicked", lambda _button: self._on_erase_clicked())
        self._header.pack_end(self._erase_button)

        return self._header

    def _build_mode_banner(self) -> Gtk.Widget | None:
        """State the operating mode, permanently, when it is not "for real"."""
        messages: list[str] = []
        if self._simulate:
            messages.append(
                "SIMULATION MODE - the drives listed are fictitious and no hardware will be touched."
            )
        if is_dry_run():
            messages.append(
                "DRY RUN - drives are real, but every command that would change one is logged and skipped."
            )

        if not messages:
            return None

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.add_css_class("zeroize-mode-banner")
        box.set_margin_top(0)

        icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        icon.set_margin_start(14)
        icon.set_margin_top(10)
        icon.set_margin_bottom(10)
        box.append(icon)

        label = Gtk.Label(label="  ".join(messages), xalign=0)
        label.set_wrap(True)
        label.set_hexpand(True)
        label.set_margin_end(14)
        label.set_margin_top(10)
        label.set_margin_bottom(10)
        box.append(label)

        return box

    def _build_pages(self) -> None:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        self._device_list = Gtk.ListBox()
        self._device_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._device_list.add_css_class("boxed-list")
        self._device_list.set_margin_top(16)
        self._device_list.set_margin_bottom(16)
        self._device_list.set_margin_start(16)
        self._device_list.set_margin_end(16)
        scroller.set_child(self._device_list)
        self._stack.add_named(scroller, _PAGE_DRIVES)

        empty = Adw.StatusPage()
        empty.set_icon_name("drive-harddisk-symbolic")
        empty.set_title("No drives found")
        empty.set_description(
            "Nothing erasable is attached, or the application is not running with "
            "the privileges needed to enumerate block devices."
        )
        self._stack.add_named(empty, _PAGE_EMPTY)

        self._progress_view = ProgressView(on_cancel=self._on_cancel_run)
        self._stack.add_named(self._progress_view, _PAGE_PROGRESS)

        self._result_view = ResultView(
            on_open_certificate=self._on_open_certificate,
            on_open_folder=self._on_open_certificate_folder,
            on_back=lambda: self.refresh_devices(),
        )
        self._stack.add_named(self._result_view, _PAGE_RESULT)

    def _show_page(self, name: str) -> None:
        """Switch pages and bring the header bar with it.

        The drive-list actions are meaningless anywhere else - an "Erase 5
        drives" button still lit on the results page invites a second run that
        the operator did not intend - so they are hidden rather than merely
        disabled, along with the selection summary they describe.
        """
        self._stack.set_visible_child_name(name)

        on_drives = name == _PAGE_DRIVES
        self._select_all_button.set_visible(on_drives)
        self._erase_button.set_visible(on_drives)
        self._rescan_button.set_visible(on_drives or name == _PAGE_EMPTY)

        if name == _PAGE_PROGRESS:
            self._window_title.set_subtitle("Erase in progress")
        elif name == _PAGE_RESULT:
            self._window_title.set_subtitle("Run complete")

    # ------------------------------------------------------------ discovery

    def refresh_devices(self) -> None:
        """Rescan and rebuild the drive list."""
        auto_unfreeze = (
            self._first_scan
            and not self._simulate
            and self._settings.safety.auto_unfreeze_on_scan
        )
        self._first_scan = False

        self._set_controls_busy(True)
        self._window_title.set_subtitle(
            "Scanning for drives and clearing security freezes..."
            if auto_unfreeze
            else "Scanning for drives..."
        )

        def worker() -> None:
            try:
                devices = discover_devices(
                    simulate=self._simulate, auto_unfreeze=auto_unfreeze
                )
            except Exception as error:  # noqa: BLE001 - surfaced, never swallowed
                _log.exception("Device discovery failed")
                GLib.idle_add(self._on_discovery_failed, str(error))
                return
            GLib.idle_add(self._on_devices_discovered, devices)

        threading.Thread(target=worker, name="discovery", daemon=True).start()

    def _on_devices_discovered(self, devices: list[Device]) -> bool:
        self._devices = devices
        self._rebuild_device_list()
        self._set_controls_busy(False)
        self._show_page(_PAGE_DRIVES if devices else _PAGE_EMPTY)
        self._update_summary()
        return GLib.SOURCE_REMOVE

    def _on_discovery_failed(self, message: str) -> bool:
        self._set_controls_busy(False)
        self._show_page(_PAGE_EMPTY)
        show_message(self, "Could not list drives", message, error=True)
        return GLib.SOURCE_REMOVE

    def _rebuild_device_list(self) -> None:
        child = self._device_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._device_list.remove(child)
            child = following

        self._rows = []
        # Rebuilt per scan: a size group holds references to its widgets, so
        # reusing one across scans would keep every previous row alive.
        method_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for device in self._devices:
            row = DeviceRow(
                device,
                on_show_capabilities=self._on_show_capabilities,
                on_unfreeze=self._on_unfreeze_clicked,
                method_size_group=method_size_group,
            )
            row.connect("selection-changed", lambda _row: self._update_summary())
            self._device_list.append(row)
            self._rows.append(row)

    # --------------------------------------------------------------- state

    def _selected_assignments(self) -> list[tuple[Device, EraseMethod]]:
        """The (drive, method) pairs the operator has chosen."""
        assignments: list[tuple[Device, EraseMethod]] = []
        for row in self._rows:
            if not row.is_selected:
                continue
            method = row.selected_method
            if method is not None:
                assignments.append((row.device, method))
        return assignments

    def _update_summary(self) -> None:
        assignments = self._selected_assignments()
        erasable = sum(1 for device in self._devices if device.can_be_erased)

        if assignments:
            total = sum(device.size_bytes for device, _ in assignments)
            self._window_title.set_subtitle(
                f"{len(assignments)} of {erasable} erasable selected - {format_size(total)}"
            )
            self._erase_button.set_label(f"Erase {len(assignments)} drive(s)")
        else:
            self._window_title.set_subtitle(
                f"{len(self._devices)} drive(s) detected, {erasable} erasable"
            )
            self._erase_button.set_label("Erase selected")

        self._erase_button.set_sensitive(bool(assignments))

    def _set_controls_busy(self, busy: bool) -> None:
        self._rescan_button.set_sensitive(not busy)
        self._select_all_button.set_sensitive(not busy)
        self._erase_button.set_sensitive(not busy and bool(self._selected_assignments()))
        for row in self._rows:
            row.set_busy(busy)

    # -------------------------------------------------------------- signals

    def _on_select_all(self, _button: Gtk.Button) -> None:
        """Select every erasable drive, or clear the selection if all are on."""
        selectable = [row for row in self._rows if row.device.can_be_erased and row.selected_method]
        everything_selected = bool(selectable) and all(row.is_selected for row in selectable)
        for row in selectable:
            row.set_selected(not everything_selected)
        self._update_summary()

    def _on_show_capabilities(self, device: Device) -> None:
        show_capability_dialog(self, device)

    def _on_unfreeze_clicked(self, device: Device) -> None:
        """Clear a drive's ATA security freeze, then rescan.

        Two stages. The bus reset is tried first because it disturbs nothing
        the operator can see; only if that fails is the suspend offered, and
        only after saying plainly what it will do. See
        :mod:`zeroize.erase.unfreeze` for why the order matters.

        Runs on a worker thread: the detach-and-rescan takes a few seconds, and
        blocking the main loop for that long makes the window look crashed.
        """
        from ..erase.unfreeze import can_attempt

        problem = can_attempt(device)
        if problem:
            show_message(self, "Cannot unfreeze this drive", problem, error=True)
            return

        _log.info("Operator requested an unfreeze of %s", device.path)

        # An NVMe controller refusing Sanitize has only one remedy, so ask for
        # it directly rather than spending a stage on a bus reset that is known
        # not to clear this.
        from ..erase.unfreeze import needs_suspend

        if needs_suspend(device):
            self._prompt_suspend(
                device,
                (
                    f"{device.path} reports that it supports Sanitize, and the "
                    f"controller is refusing to run it.\n\n"
                    f"Suspending the machine for about three seconds clears "
                    f"that. The screen will go blank and the machine will look "
                    f"as though it is off. It wakes itself - nothing needs "
                    f"pressing."
                ),
            )
            return

        self._start_unfreeze(device, allow_suspend=False)

    def _start_unfreeze(self, device: Device, *, allow_suspend: bool) -> None:
        """Run one unfreeze attempt on a worker thread."""
        self._set_controls_busy(True)
        self._window_title.set_subtitle(
            f"Suspending briefly to clear the freeze on {device.path}..."
            if allow_suspend
            else f"Clearing the security freeze on {device.path}..."
        )

        def worker() -> None:
            from ..erase.unfreeze import unfreeze

            try:
                cleared, detail = unfreeze(device, allow_suspend=allow_suspend)
            except Exception as error:  # noqa: BLE001 - surfaced, never swallowed
                _log.exception("Unfreeze failed on %s", device.path)
                cleared, detail = False, f"internal error: {error}"
            GLib.idle_add(self._on_unfreeze_finished, device, cleared, detail, allow_suspend)

        threading.Thread(target=worker, name="unfreeze", daemon=True).start()

    def _on_unfreeze_finished(
        self,
        device: Device,
        cleared: bool,
        detail: str,
        suspended: bool = False,
    ) -> bool:
        """Report the outcome and rescan - the drive may have been renamed."""
        if cleared:
            _log.info("Unfreeze of %s reported: %s", device.path, detail)
            # Always rescan: after a detach and re-attach the kernel may have
            # given the drive a different name, so the row on screen is stale
            # either way.
            self.refresh_devices()
            return GLib.SOURCE_REMOVE

        self._set_controls_busy(False)
        self._update_summary()

        if suspended:
            # Both stages have now been tried; there is nothing left to offer.
            show_message(
                self,
                f"Could not unfreeze {device.path}",
                f"{detail}\n\nThe drive can still be erased with a software "
                f"overwrite, which the firmware freeze does not affect.",
                error=True,
            )
            return GLib.SOURCE_REMOVE

        # Stage two, on request only.
        self._prompt_suspend(
            device,
            (
                f"{detail}\n\n"
                f"The next thing to try is suspending the machine for about "
                f"three seconds. That removes power from the drive, which "
                f"clears the freeze on controllers where a bus reset does not."
            ),
        )
        return GLib.SOURCE_REMOVE

    def _prompt_suspend(self, device: Device, reason: str) -> None:
        """Ask before suspending the machine, and say what will happen.

        This is the one action that reaches past the selected drive. Everything
        else the tool does is confined to one device; this puts the whole
        machine to sleep, and on a bench with several drives in flight the
        operator needs to know that beforehand rather than while the screen is
        already dark.

        Shared by both callers so the ATA fallback and the NVMe path describe
        the same event in the same words.
        """
        from ..erase.unfreeze import suspend_warning

        body = (
            f"{reason}\n\n"
            f"The screen will go blank and the machine will appear to be off. "
            f"It wakes itself - nothing needs pressing.\n\n"
            f"Every drive in this machine is affected, not only this one, and "
            f"no erase may be running."
        )

        # Shown only when it applies: running from the USB medium is the case
        # where a suspend can take the whole session down with it.
        warning = suspend_warning()
        if warning:
            body += f"\n\n{warning}"

        confirm_action(
            self,
            f"Suspend this machine to unfreeze {device.path}?",
            body,
            confirm_label="Suspend and retry",
            on_confirm=lambda: self._start_unfreeze(device, allow_suspend=True),
        )

    def _on_export_diagnostics(self) -> None:
        """Gather logs and system state onto the certificate volume.

        Runs on a worker thread: it queries every attached drive's firmware,
        which on a bench full of drives takes long enough to freeze the window.

        The live filesystem is a tmpfs, so everything under /var/log is gone at
        power off - which is exactly when a problem is worth reviewing. This
        writes to the labelled volume instead, where it survives and can be
        read on another machine.
        """
        self._set_controls_busy(True)
        self._window_title.set_subtitle("Collecting diagnostics...")

        def worker() -> None:
            try:
                from ..diagnostics import collect_diagnostics

                destination = collect_diagnostics(
                    self._devices,
                    output_label=self._settings.certificate.output_volume_label,
                )
            except Exception as error:  # noqa: BLE001 - surfaced, never swallowed
                _log.exception("Diagnostics export failed")
                GLib.idle_add(self._on_diagnostics_finished, None, str(error))
                return
            GLib.idle_add(self._on_diagnostics_finished, destination, "")

        threading.Thread(target=worker, name="diagnostics", daemon=True).start()

    def _on_diagnostics_finished(self, destination, error: str) -> bool:
        self._set_controls_busy(False)
        self._update_summary()

        if destination is None:
            show_message(
                self,
                "Could not export diagnostics",
                error,
                error=True,
            )
            return GLib.SOURCE_REMOVE

        _log.info("Diagnostics exported to %s", destination)
        show_message(
            self,
            "Diagnostics exported",
            f"Logs and system state were written to:\n\n{destination}\n\n"
            f"If this is on the certificate volume, it will still be there "
            f"after a power cycle and can be read on another machine.",
        )
        return GLib.SOURCE_REMOVE

    def _on_erase_clicked(self) -> None:
        assignments = self._selected_assignments()
        if not assignments:
            return

        dialog = ConfirmEraseDialog(
            self,
            assignments,
            self._settings,
            # Only meaningful when the name is NOT required - an unattended
            # bench run can carry a configured site operator. When it is
            # required, the dialog ignores this and starts empty.
            default_operator=self._settings.organisation.operator,
            on_confirmed=lambda operator: self._start_run(assignments, operator),
        )
        dialog.present()

    # ------------------------------------------------------------- the run

    def _start_run(self, assignments: list[tuple[Device, EraseMethod]], operator: str) -> None:
        _log.info(
            "Operator %s confirmed a run over %d drive(s): %s",
            operator or "unnamed",
            len(assignments),
            ", ".join(f"{device.path}={method.key}" for device, method in assignments),
        )

        self._set_controls_busy(True)
        self._erase_button.set_sensitive(False)
        self._progress_view.begin(assignments)
        self._show_page(_PAGE_PROGRESS)

        self._run = EraseRun(
            assignments,
            self._settings,
            operator=operator,
            on_progress=self._on_progress_from_worker,
        )
        self._run.start()

        def waiter() -> None:
            summary = self._run.wait() if self._run else None
            GLib.idle_add(self._on_run_finished, summary)

        threading.Thread(target=waiter, name="run-waiter", daemon=True).start()

    def _on_progress_from_worker(self, update: ProgressUpdate) -> None:
        """Called from an erase worker thread - marshal onto the main loop.

        Every widget touch has to happen on the main thread. This is the one
        boundary where that is enforced, and it is enforced here rather than in
        each caller so an implementation cannot forget.
        """
        GLib.idle_add(self._apply_progress, update)

    def _apply_progress(self, update: ProgressUpdate) -> bool:
        self._progress_view.apply(update)
        return GLib.SOURCE_REMOVE

    def _on_cancel_run(self) -> None:
        if self._run is not None:
            self._run.cancel()
            self._progress_view.set_headline("Stopping what can be stopped...")

    def _on_run_finished(self, summary: RunSummary | None) -> bool:
        self._run = None

        if summary is None:
            self._progress_view.finish("The run produced no result")
            self._set_controls_busy(False)
            return GLib.SOURCE_REMOVE

        succeeded = sum(1 for result in summary.results if result.succeeded)
        failed = len(summary.results) - succeeded
        self._progress_view.finish(
            f"{succeeded} erased, {failed} failed" if failed else f"{succeeded} drive(s) erased"
        )

        certificate_path = ""
        try:
            pdf_path, _json_path = issue_certificate(summary, self._settings)
            self._certificate_path = pdf_path
            certificate_path = str(pdf_path)
        except Exception as error:  # noqa: BLE001 - the erase already happened
            _log.exception("Certificate generation failed")
            self._certificate_path = None
            show_message(
                self,
                "Certificate could not be generated",
                f"The drives were processed and the results are in the log, but the "
                f"certificate could not be written:\n\n{error}",
                error=True,
            )

        self._result_view.show_result(
            succeeded=succeeded,
            failed=failed,
            certificate_path=certificate_path,
            serials=summary.serials,
        )
        self._show_page(_PAGE_RESULT)
        self._set_controls_busy(False)
        return GLib.SOURCE_REMOVE

    # -------------------------------------------------------- certificates

    def _open_with_desktop_handler(self, target: Path) -> None:
        """Hand a path to the desktop's opener and report it when that fails.

        The application runs as root via ``pkexec``. Launching a viewer as root
        would put a full-privilege process in front of the operator and, on
        most desktops, fail anyway for want of a session bus - so the invoking
        user is tried first, with the session environment the child needs.

        Each candidate is *waited on* rather than fired and forgotten. The
        earlier version used ``Popen``, which succeeds as long as the binary
        exists: with no PDF viewer installed, ``xdg-open`` would start, fail to
        find a handler, exit non-zero, and the button would appear to do
        nothing at all. ``xdg-open`` returns once it has handed off, so its exit
        code is both meaningful and quick.
        """
        user = invoking_user_name()
        uid = invoking_user_uid()

        # xdg-open needs to reach the operator's session. Running as root, the
        # display is inherited but the runtime directory is not.
        environment = dict(os.environ)
        environment.setdefault("DISPLAY", ":0")
        environment["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

        candidates: list[list[str]] = []
        if uid != 0:
            candidates.append(["runuser", "-u", user, "--", "xdg-open", str(target)])
            candidates.append(["runuser", "-u", user, "--", "gio", "open", str(target)])
        candidates.append(["xdg-open", str(target)])
        candidates.append(["gio", "open", str(target)])

        failures: list[str] = []
        for argv in candidates:
            try:
                completed = subprocess.run(  # noqa: S603
                    argv,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            except (OSError, FileNotFoundError):
                failures.append(f"{argv[0]}: not installed")
                continue
            except subprocess.TimeoutExpired:
                # The handler is taking its time but was launched; that counts.
                _log.info("Opened %s with: %s (still starting)", target, " ".join(argv))
                return

            if completed.returncode == 0:
                _log.info("Opened %s with: %s", target, " ".join(argv))
                return

            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            failures.append(f"{argv[0]}: exit {completed.returncode} {detail[0] if detail else ''}".strip())

        _log.error("Could not open %s. Tried: %s", target, "; ".join(failures))
        show_message(
            self,
            "Could not open it",
            f"No application on this system could open:\n\n{target}\n\n"
            f"Tried: {', '.join(failures)}",
            error=True,
        )

    def _on_open_certificate(self) -> None:
        if self._certificate_path is not None:
            self._open_with_desktop_handler(self._certificate_path)

    def _on_open_certificate_folder(self) -> None:
        if self._certificate_path is not None:
            self._open_with_desktop_handler(self._certificate_path.parent)
