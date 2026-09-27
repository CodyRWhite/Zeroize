"""The GTK4 / libadwaita interface.

Importing this package pulls in PyGObject and the GTK and libadwaita typelibs.
Nothing outside the interface imports it, so the discovery, erase and
certificate layers stay usable on a machine with no desktop stack installed -
which is what makes ``zeroize list`` and ``zeroize erase`` work over SSH.
"""

from __future__ import annotations

__all__ = ["run_application"]


def run_application(*args, **kwargs):
    """Lazy re-export so importing the package does not require GTK."""
    from .app import run_application as implementation

    return implementation(*args, **kwargs)
