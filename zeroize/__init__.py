"""Zeroize Drive Wiper - purge-grade drive erasure with proof, for Linux.

A GTK4/libadwaita desktop application that discovers attached block devices,
shows their partition layout, erases the selected drives with a method the
hardware will actually accept, and issues a PDF erase certificate for the run.

The identity constants below are the single source of truth for the product
name, version and application id. Logging paths, the certificate footer, the
``.desktop`` entry, the polkit action and the packaging metadata for both the
``.deb`` and the ``.rpm`` all read them from here, so a rename or a version
bump happens in exactly one place.

The product is deliberately standalone: nothing here names the organisation
operating it. *Who* performed an erase is site configuration, held in the
organisation block of :mod:`zeroize.config` and printed on the certificate, so
the same package can be used at more than one site and on customer-owned
drives without rebuilding.
"""

from __future__ import annotations

__app_name__ = "Zeroize Drive Wiper"
__short_name__ = "Zeroize"
__tagline__ = "Purge-grade drive erasure"
__version__ = "1.0.1"

#: Short machine-readable identifier - binary name, log directory, polkit
#: action suffix, and the package name in both the .deb and the .rpm.
__slug__ = "zeroize"

#: Reverse-DNS application id used by GTK/libadwaita, the polkit policy and
#: the AppStream metainfo file.
__app_id__ = "io.zeroize.Zeroize"

#: Publisher shown in package metadata. Deliberately the product itself, not
#: the operating organisation - see the module docstring.
__publisher__ = "Zeroize"

#: Where the tool came from. Printed on the certificate so a reader who was not
#: present at the erase can find the source, read what the method actually did,
#: and check the release hashes against the version that signed their document.
#: A certificate that cannot be traced back to a specific build is an assertion
#: rather than evidence.
__project_url__ = "github.com/CodyRWhite/Zeroize"

__all__ = [
    "__app_id__",
    "__app_name__",
    "__project_url__",
    "__publisher__",
    "__short_name__",
    "__slug__",
    "__tagline__",
    "__version__",
]
