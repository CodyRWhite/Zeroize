"""Icon name resolution across icon-theme versions.

Icon names are not stable. GNOME renames and retires them between Adwaita
releases, and the four target distributions ship versions years apart - RHEL 9
carries Adwaita 42, where ``emblem-ok-symbolic`` exists, while Debian 13
carries Adwaita 48, where it does not and ``object-select-symbolic`` is the
replacement. Hardcoding either name gives a broken-image placeholder on half
the supported platforms.

:func:`resolve` takes the candidates in order of preference and returns the
first one the running theme actually has, so the same package renders properly
everywhere. The lookup is done at widget construction, when a display exists
and the theme has been loaded.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

from ..logging_setup import get_logger  # noqa: E402

_log = get_logger("Icons")

#: Last resort. Every theme has it, and it renders as a visible placeholder
#: rather than as nothing, so a missing icon is obvious instead of silent.
_FALLBACK = "image-missing"

_cache: dict[tuple[str, ...], str] = {}


def resolve(*candidates: str) -> str:
    """Return the first icon name in *candidates* the current theme provides."""
    if not candidates:
        return _FALLBACK

    cached = _cache.get(candidates)
    if cached is not None:
        return cached

    display = Gdk.Display.get_default()
    if display is None:
        # No display yet - return the preferred name rather than caching a
        # guess, so the real lookup still happens once the display exists.
        return candidates[0]

    theme = Gtk.IconTheme.get_for_display(display)
    for name in candidates:
        if theme.has_icon(name):
            _cache[candidates] = name
            return name

    _log.warning("No icon found for any of: %s", ", ".join(candidates))
    _cache[candidates] = _FALLBACK
    return _FALLBACK


# Named lookups used by more than one widget. Each list runs from the most
# current name to the oldest, so a modern theme is matched first.
SUCCESS = ("object-select-symbolic", "emblem-ok-symbolic", "emblem-default-symbolic")
WARNING = ("dialog-warning-symbolic",)
ERROR = ("dialog-error-symbolic",)
INFORMATION = ("dialog-information-symbolic",)

DRIVE_NVME = ("drive-harddisk-solidstate-symbolic", "drive-harddisk-symbolic")
DRIVE_DISK = ("drive-harddisk-symbolic",)
DRIVE_USB = (
    "drive-removable-media-usb-symbolic",
    "media-removable-symbolic",
    "drive-removable-media-symbolic",
    "drive-harddisk-usb-symbolic",
    "drive-harddisk-symbolic",
)
DRIVE_FLASH = ("media-flash-symbolic", "drive-removable-media-symbolic", "drive-harddisk-symbolic")
DRIVE_VIRTUAL = ("drive-harddisk-system-symbolic", "drive-harddisk-symbolic")
