"""The version is declared in three places, and they must agree.

`zeroize.__version__` is the source of truth: the release workflow checks the
git tag against it and refuses to publish a mismatch. But nothing checked the
*other* declarations, and each one is load-bearing somewhere different:

* ``pyproject.toml`` is what pip and any build backend read;
* the AppStream metainfo is what a software centre shows as the installed
  version and the changelog.

Drift between them is quiet and lopsided. The tag gate would still pass, the
release would still publish, and the .deb would install announcing one version
while the desktop reported another - the sort of thing nobody notices until
they are trying to work out which build produced a certificate.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import zeroize

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_matches_the_package() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == zeroize.__version__


def test_appstream_metainfo_has_an_entry_for_this_version() -> None:
    """The newest <release> must be the version being shipped."""
    metainfo = ROOT / "packaging/share/metainfo/io.zeroize.Zeroize.metainfo.xml"
    versions = re.findall(r'<release version="([^"]+)"', metainfo.read_text(encoding="utf-8"))

    assert versions, "the metainfo declares no releases at all"
    assert versions[0] == zeroize.__version__, (
        f"newest metainfo release is {versions[0]}, "
        f"but the package is {zeroize.__version__}"
    )


def test_releases_are_listed_newest_first() -> None:
    """AppStream renders them in document order, so the order is the changelog."""
    metainfo = ROOT / "packaging/share/metainfo/io.zeroize.Zeroize.metainfo.xml"
    versions = re.findall(r'<release version="([^"]+)"', metainfo.read_text(encoding="utf-8"))

    def parts(version: str) -> tuple[int, ...]:
        return tuple(int(piece) for piece in version.split("."))

    assert versions == sorted(versions, key=parts, reverse=True), versions


def test_the_project_url_is_not_a_bare_scheme() -> None:
    """It is printed on the certificate, so it has to be readable as it stands.

    No scheme and no trailing slash: it is set in 7pt type in a footer, next to
    a version number, and read by someone deciding whether to trust the
    document - not clicked.
    """
    url = zeroize.__project_url__
    assert url
    assert not url.startswith(("http://", "https://")), url
    assert not url.endswith("/"), url
    assert "/" in url, "the URL should name the repository, not just the host"
