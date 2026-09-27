"""The libadwaita application object and the Zeroize stylesheet.

The stylesheet is deliberately small. libadwaita already provides a coherent
light and dark theme, and overriding it wholesale would produce an application
that looks wrong on half the desktops it runs on. What is defined here is only
what carries the brand or encodes meaning:

* the accent colour, applied to progress bars and selections;
* the mode banner, which must be impossible to overlook;
* success and failure colours on finished progress strips.

Everything else - spacing, typography, the header bar, the boxed lists - comes
from the platform theme and adapts to the user's light/dark preference on its
own.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import __app_id__, __app_name__, __version__  # noqa: E402
from ..branding import AMBER, CHARCOAL, ORANGE, SIGNAL, VERDANT  # noqa: E402
from ..config import Settings  # noqa: E402
from ..logging_setup import get_logger  # noqa: E402
from .main_window import MainWindow  # noqa: E402

_log = get_logger("App")

_STYLESHEET = f"""
/* The mode banner. High contrast on purpose: mistaking a simulated run for a
   real one, or the reverse, is the failure this exists to prevent. */
.zeroize-mode-banner {{
    background-color: {AMBER};
    color: {CHARCOAL};
    font-weight: bold;
}}

/* The accent, applied where progress and selection are shown. */
.zeroize-device-row progressbar > trough > progress,
progressbar > trough > progress {{
    background-color: {ORANGE};
}}

.zeroize-success {{
    color: {VERDANT};
    font-weight: bold;
}}

.zeroize-success > trough > progress {{
    background-color: {VERDANT};
}}

.zeroize-failure {{
    color: {SIGNAL};
    font-weight: bold;
}}

.zeroize-failure > trough > progress {{
    background-color: {SIGNAL};
}}

.zeroize-partition-bar {{
    margin-top: 2px;
    margin-bottom: 2px;
}}
"""


class ZeroizeApplication(Adw.Application):
    """The application. One window, no session state worth restoring."""

    def __init__(self, settings: Settings, *, simulate: bool = False) -> None:
        super().__init__(
            application_id=__app_id__,
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._settings = settings
        self._simulate = simulate
        self._window: MainWindow | None = None

        # NON_UNIQUE because the tool is routinely launched more than once on a
        # bench machine - one instance per operator session or per pkexec
        # invocation - and the single-instance behaviour would silently focus
        # somebody else's window instead of starting a run.

        self.connect("activate", self._on_activate)

    def _on_activate(self, _application: Adw.Application) -> None:
        if self._window is None:
            self._install_stylesheet()
            self._install_actions()
            self._window = MainWindow(self, self._settings, simulate=self._simulate)
        self._window.present()

    def _install_stylesheet(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            _log.warning("No display available; skipping the stylesheet")
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(_STYLESHEET.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _install_actions(self) -> None:
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_args: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def _on_about(self, *_args) -> None:
        if self._window is None:
            return
        about = Adw.AboutWindow(
            transient_for=self._window,
            application_name=__app_name__,
            application_icon=__app_id__,
            version=__version__,
            comments=(
                "Purge-grade drive erasure with proof. Discovers attached drives, "
                "erases them with a method the hardware will actually accept, and "
                "issues a PDF certificate for the run."
            ),
            license_type=Gtk.License.MIT_X11,
        )
        about.present()


def run_application(settings: Settings, *, simulate: bool = False) -> int:
    """Start the interface. Returns the process exit code."""
    GLib.set_prgname("zeroize")
    GLib.set_application_name(__app_name__)
    application = ZeroizeApplication(settings, simulate=simulate)
    return application.run([])
