#!/usr/bin/env python3
"""The single build entrypoint for Zeroize.

    python3 build.py stage      # assemble the install tree under build/stage
    python3 build.py deb        # build the .deb
    python3 build.py rpm        # build the .rpm
    python3 build.py iso        # build the bootable live image
    python3 build.py all        # stage + deb + rpm
    python3 build.py clean

Everything lands in ``build/``, which is never committed.

**Layout.** The application installs as a plain package tree at
``/usr/lib/zeroize`` with a launcher at ``/usr/bin/zeroize``, rather than into
a Python site-packages directory. Those directories differ across the four
target distributions and, on Fedora and RHEL, embed the Python minor version -
so a package built against one is wrong on the next release. A fixed path plus
a two-line launcher is portable, and the ``.deb`` and the ``.rpm`` install
byte-identical trees.

**Dependencies are declared, not vendored.** GTK, libadwaita, PyGObject,
ReportLab, nvme-cli, hdparm and sg3_utils all exist in every target
distribution's archive. Bundling them would produce a package that is larger,
harder to audit, and slower to get security updates - the wrong trade for a
tool that runs as root against raw block devices.

Building a ``.deb`` needs ``dpkg-deb``; an ``.rpm`` needs ``rpmbuild``; the ISO
needs ``live-build`` or ``xorriso`` plus a Debian-family host. Each target
reports what is missing rather than failing obscurely, and ``stage`` works
anywhere - including on Windows, where the tree can be assembled and inspected
even though the packages cannot be built.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from zeroize import __app_name__, __slug__, __version__  # noqa: E402
from zeroize.branding import CHARCOAL, ORANGE, PAPER, mark_svg  # noqa: E402

BUILD_DIR = ROOT / "build"
STAGE_DIR = BUILD_DIR / "stage"
DIST_DIR = BUILD_DIR / "dist"
PACKAGING = ROOT / "packaging"

#: Where the package tree lands inside the staged filesystem.
INSTALL_LIB = "usr/lib/zeroize"
INSTALL_BIN = "usr/bin/zeroize"

#: Runtime dependencies, per packaging format. The names differ between the
#: Debian and RPM worlds, so both lists are spelled out rather than guessed.
DEB_DEPENDS = [
    "python3 (>= 3.11)",
    "python3-gi",
    "python3-gi-cairo",
    "gir1.2-gtk-4.0",
    "gir1.2-adw-1",
    # Not pulled in by the GTK typelibs, and without it every symbolic icon
    # in the interface renders as a broken-image placeholder.
    "adwaita-icon-theme",
    "python3-reportlab",
    "util-linux",
    "nvme-cli",
    "hdparm",
    "sg3-utils",
    # polkit was split and renamed partway through the supported range.
    # Debian 12 and Ubuntu 22.04 ship the combined "policykit-1"; Debian 13 and
    # Ubuntu 24.04 dropped it in favour of "polkitd" for the daemon and
    # "pkexec" for the binary. Both are named, daemon first, because the
    # desktop entry invokes /usr/bin/pkexec and the action needs the daemon to
    # evaluate it - depending on only one of them installs a launcher that
    # cannot escalate.
    "polkitd | policykit-1",
    "pkexec | policykit-1",
]
DEB_RECOMMENDS = ["dmidecode", "smartmontools"]

RPM_REQUIRES = [
    "python3 >= 3.11",
    "python3-gobject",
    "gtk4",
    "libadwaita",
    "adwaita-icon-theme",
    "python3-reportlab",
    "util-linux",
    "nvme-cli",
    "hdparm",
    "sg3_utils",
    "polkit",
]
RPM_RECOMMENDS = ["dmidecode", "smartmontools"]

#: Files and directories never copied into the install tree.
EXCLUDED = {"__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"}

ICON_SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)


def _log(message: str) -> None:
    print(f"==> {message}")


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"    $ {' '.join(argv)}")
    return subprocess.run(argv, check=True, **kwargs)  # noqa: S603


def _tool_missing(name: str) -> bool:
    return shutil.which(name) is None


# --------------------------------------------------------------------------
# Build lock
# --------------------------------------------------------------------------

class _BuildLock:
    """Refuse to run two builds at once.

    Every target shares ``build/stage`` and ``build/dist``, and staging begins
    by deleting the staging tree - so a second build started while the first is
    still running will pull the ground out from under it, and the artifacts
    that come out belong to neither. An ISO takes ten minutes, which is long
    enough to forget one is already going.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._acquired = False

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                owner = self._path.read_text(encoding="utf-8").strip()
            except OSError:
                owner = "unknown"
            if _process_alive(owner):
                raise SystemExit(
                    f"A build is already running ({owner}). "
                    f"Wait for it to finish, or remove {self._path} if it died."
                )
            _log(f"Clearing a stale build lock from {owner}")

        self._path.write_text(_lock_owner(), encoding="utf-8")
        self._acquired = True
        return self

    def __exit__(self, *_exception) -> None:
        if self._acquired:
            try:
                self._path.unlink()
            except OSError:
                pass


def _lock_owner() -> str:
    """Identify this process in a way the next one can check."""
    return f"pid {os.getpid()} on {sys.platform}"


def _process_alive(owner: str) -> bool:
    """True when the lock's owning process still exists.

    A stale lock and a live one look identical on disk, so the pid is probed.
    Two details matter:

    * On Windows ``os.kill(pid, 0)`` does not probe - it calls
      ``TerminateProcess`` and kills the process with exit code 0. So the pid
      is looked up with ``tasklist`` there instead.
    * The ISO target runs under WSL while the package targets run under
      Windows, and their pid spaces are unrelated. A lock written by the other
      platform cannot be checked, so it is assumed live: refusing a build the
      operator can clear by hand beats silently clobbering a running one.
    """
    parts = owner.split()
    if len(parts) < 2 or parts[0] != "pid" or not parts[1].isdigit():
        return False

    pid = int(parts[1])
    platform = parts[3] if len(parts) > 3 else ""
    if platform and platform != sys.platform:
        return True

    if sys.platform == "win32":
        probe = subprocess.run(  # noqa: S603 - argv list, never shell=True
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in probe.stdout

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------
# Modes are set explicitly rather than inherited from the checkout. Two reasons:
# a stray umask or a git clone can leave source files group-writable, which
# lintian and rpmlint both complain about; and a tree staged on a filesystem
# that cannot represent Unix permissions reports everything as 777, which
# dpkg-deb refuses outright.

#: Paths (relative to the staged root) that must be executable.
_EXECUTABLE_PATHS = frozenset(
    {
        INSTALL_BIN,
        "DEBIAN/postinst",
        "DEBIAN/postrm",
        "DEBIAN/preinst",
        "DEBIAN/prerm",
    }
)


def _supports_permissions(directory: Path) -> bool:
    """True when *directory*'s filesystem can actually store a file mode.

    Windows drives mounted into WSL, and most network filesystems, report a
    fixed mode however the file was chmodded. Probing is the only reliable way
    to find out: ``stat.st_mode`` after a ``chmod`` either agrees or it does
    not.
    """
    probe = directory / ".zeroize-permission-probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.chmod(0o644)
        actual = probe.stat().st_mode & 0o777
        return actual == 0o644
    except OSError:
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _normalise_modes(tree: Path) -> None:
    """Set every mode in the staged tree to what the package should ship."""
    for path in sorted(tree.rglob("*")):
        relative = path.relative_to(tree).as_posix()
        try:
            if path.is_dir():
                path.chmod(0o755)
            elif relative in _EXECUTABLE_PATHS:
                path.chmod(0o755)
            else:
                path.chmod(0o644)
        except OSError as error:  # pragma: no cover - reported, not fatal
            print(f"    ! could not set the mode on {relative}: {error}")
    try:
        tree.chmod(0o755)
    except OSError:
        pass


def _packaging_tree(tree: Path) -> tuple[Path, Path | None]:
    """Return a tree with correct modes, relocating it if it cannot hold them.

    Returns ``(tree_to_package, temporary_root_to_clean_up)``. The second item
    is ``None`` when no relocation was needed.

    Building from a Windows drive under WSL is the case this exists for. The
    source can live there perfectly well - it is only the *packaging* step that
    needs real Unix permissions - so rather than refusing, the tree is copied
    onto a filesystem that has them.
    """
    if _supports_permissions(tree):
        _normalise_modes(tree)
        return tree, None

    _log(
        f"{tree} is on a filesystem that cannot store Unix permissions; "
        f"copying to a native filesystem to package"
    )
    # /var/tmp rather than /tmp: it is real storage on hosts where /tmp is a
    # tmpfs, and it is where large temporary build data belongs.
    # S108 keys on the literal path. What follows is mkdtemp(), which
    # creates a private 0700 directory with an unpredictable name - the
    # thing the rule exists to require.
    parent = Path("/var/tmp") if Path("/var/tmp").is_dir() else None  # noqa: S108
    temporary_root = Path(tempfile.mkdtemp(prefix="zeroize-pkg-", dir=str(parent) if parent else None))
    relocated = temporary_root / "stage"
    shutil.copytree(tree, relocated, symlinks=True)
    _normalise_modes(relocated)

    if not _supports_permissions(relocated):
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise SystemExit(
            f"Could not find a filesystem that stores Unix permissions "
            f"(tried {tree} and {relocated}). Build from a native Linux path."
        )

    return relocated, temporary_root


# --------------------------------------------------------------------------
# Staging
# --------------------------------------------------------------------------

def _copy_package(destination: Path) -> None:
    """Copy the ``zeroize`` package, skipping caches and virtual environments."""
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        ROOT / "zeroize",
        destination / "zeroize",
        ignore=shutil.ignore_patterns(*EXCLUDED, "*.pyc"),
        dirs_exist_ok=True,
    )


def _write_icons(stage: Path) -> None:
    """Generate the icon theme entries from the branding module.

    The mark is vector geometry defined in :mod:`zeroize.branding`, so the
    icons are generated at build time rather than committed as binaries. There
    is then exactly one definition of the logo, and an icon cannot drift out of
    step with the application that ships it.
    """
    scalable = stage / "usr/share/icons/hicolor/scalable/apps"
    scalable.mkdir(parents=True, exist_ok=True)
    (scalable / "io.zeroize.Zeroize.svg").write_text(
        mark_svg(512, background=CHARCOAL, ring_colour=PAPER, slash_colour=ORANGE),
        encoding="utf-8",
    )

    # Fixed-size SVGs as well: several desktops and most icon caches prefer a
    # sized entry, and an SVG at a declared size is valid in the hicolor spec.
    for size in ICON_SIZES:
        sized = stage / f"usr/share/icons/hicolor/{size}x{size}/apps"
        sized.mkdir(parents=True, exist_ok=True)
        (sized / "io.zeroize.Zeroize.svg").write_text(
            mark_svg(size, background=CHARCOAL, ring_colour=PAPER, slash_colour=ORANGE),
            encoding="utf-8",
        )

    # A symbolic variant for the header bar and notifications: no background,
    # single colour, so the theme can recolour it.
    symbolic = stage / "usr/share/icons/hicolor/symbolic/apps"
    symbolic.mkdir(parents=True, exist_ok=True)
    (symbolic / "io.zeroize.Zeroize-symbolic.svg").write_text(
        mark_svg(16, background=None, ring_colour="#000000", slash_colour="#000000"),
        encoding="utf-8",
    )


def _write_default_config(stage: Path) -> None:
    """Ship a commented default configuration at /etc/zeroize/config.json."""
    from zeroize.config import write_example_config

    target = stage / "etc" / __slug__ / "config.json"
    write_example_config(target)


def _write_manpage(stage: Path) -> None:
    """A minimal man page, because a root-level CLI without one is rude."""
    directory = stage / "usr/share/man/man1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "zeroize.1").write_text(
        f""".TH ZEROIZE 1 "2026-09-26" "{__version__}" "{__app_name__}"
.SH NAME
zeroize \\- purge-grade drive erasure with proof
.SH SYNOPSIS
.B zeroize
[\\fIOPTIONS\\fR] [\\fICOMMAND\\fR]
.SH DESCRIPTION
Erases attached drives using a method the hardware will accept, and issues a
PDF certificate recording what was destroyed. With no command, the graphical
interface is launched.
.SH COMMANDS
.TP
.B list
List attached drives, their capability registers and the erase methods each
one supports.
.TP
.B erase \\-\\-device \\fIPATH\\fR \\-\\-method \\fIKEY\\fR \\-\\-yes\\-i\\-am\\-sure
Erase from the command line. The confirmation flag is mandatory.
.TP
.B config
Print the merged configuration, including the organisation block printed on
certificates.
.SH OPTIONS
.TP
.B \\-\\-simulate
Use a fixed set of fictitious drives. No hardware is touched.
.TP
.B \\-\\-dry\\-run
Enumerate real drives, but log and skip every command that would change one.
.TP
.B \\-\\-verbose
Log at debug level.
.SH FILES
.TP
.I /etc/zeroize/config.json
Site configuration, including the organisation block.
.TP
.I ~/.config/zeroize/config.json
Per-operator overrides.
.TP
.I /var/log/zeroize/
One timestamped CMTrace-format log per run.
.SH EXIT STATUS
0 on success, 1 on a failed erase or certificate, 2 on a usage error, 13 when
run without the required privileges.
.SH NOTES
Zeroize must run as root. The desktop launcher uses pkexec.
.SH SEE ALSO
.BR nvme (1),
.BR hdparm (8),
.BR sg_sanitize (8)
""",
        encoding="utf-8",
    )


def stage() -> Path:
    """Assemble the complete install tree under ``build/stage``."""
    _log(f"Staging {__app_name__} {__version__}")
    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    STAGE_DIR.mkdir(parents=True)

    _copy_package(STAGE_DIR / INSTALL_LIB)

    launcher = STAGE_DIR / INSTALL_BIN
    launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PACKAGING / "share/bin/zeroize", launcher)
    launcher.chmod(0o755)

    for relative, source in (
        ("usr/share/applications", PACKAGING / "share/applications/io.zeroize.Zeroize.desktop"),
        ("usr/share/polkit-1/actions", PACKAGING / "share/polkit/io.zeroize.Zeroize.policy"),
        ("usr/share/metainfo", PACKAGING / "share/metainfo/io.zeroize.Zeroize.metainfo.xml"),
    ):
        target = STAGE_DIR / relative
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target / source.name)

    _write_icons(STAGE_DIR)
    _write_default_config(STAGE_DIR)
    _write_manpage(STAGE_DIR)

    doc_dir = STAGE_DIR / "usr/share/doc" / __slug__
    doc_dir.mkdir(parents=True, exist_ok=True)
    for name in ("README.md", "LICENSE"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, doc_dir / name)

    file_count = sum(1 for path in STAGE_DIR.rglob("*") if path.is_file())
    _log(f"Staged {file_count} files into {STAGE_DIR}")
    return STAGE_DIR


# --------------------------------------------------------------------------
# .deb
# --------------------------------------------------------------------------

_DEB_POSTINST = """#!/bin/sh
set -e

# Refresh the caches that make the launcher and its icon appear without a
# logout. All three are best-effort: a headless install has none of them.
if [ -x /usr/bin/gtk-update-icon-cache ]; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
if [ -x /usr/bin/update-desktop-database ]; then
    update-desktop-database -q || true
fi

install -d -m 0755 /var/log/zeroize
install -d -m 0755 /var/lib/zeroize

exit 0
"""

_DEB_POSTRM = """#!/bin/sh
set -e

if [ "$1" = "purge" ]; then
    # Logs and certificates are the record of destroyed data. They are removed
    # only on an explicit purge, never on a plain remove or an upgrade.
    rm -rf /var/log/zeroize
    rm -rf /etc/zeroize
fi

if [ -x /usr/bin/gtk-update-icon-cache ]; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi

exit 0
"""


def _installed_size_kib(tree: Path) -> int:
    total = sum(path.stat().st_size for path in tree.rglob("*") if path.is_file())
    return max(1, total // 1024)


def build_deb() -> Path:
    """Build the .deb from the staged tree."""
    if _tool_missing("dpkg-deb"):
        raise SystemExit(
            "dpkg-deb is not installed. Build the .deb on a Debian or Ubuntu host, "
            "or in a container: docker run --rm -v \"$PWD\":/src -w /src debian:12 "
            "sh -c 'apt-get update && apt-get install -y dpkg-dev python3 && python3 build.py deb'"
        )

    tree = stage()
    architecture = "all"

    debian = tree / "DEBIAN"
    debian.mkdir(parents=True, exist_ok=True)

    control = [
        f"Package: {__slug__}",
        f"Version: {__version__}",
        f"Architecture: {architecture}",
        "Section: admin",
        "Priority: optional",
        "Maintainer: Zeroize <zeroize@localhost>",
        f"Installed-Size: {_installed_size_kib(tree)}",
        f"Depends: {', '.join(DEB_DEPENDS)}",
        f"Recommends: {', '.join(DEB_RECOMMENDS)}",
        f"Description: {__app_name__} - purge-grade drive erasure with proof",
        "  Erases attached drives using a method the drive's own firmware will",
        "  accept, and issues a PDF certificate recording what was destroyed,",
        "  how, and on which machine.",
        "  .",
        "  Reads each drive's capability registers and offers only the methods it",
        "  supports: NVMe Sanitize and Format NVM, ATA Secure Erase, SCSI Sanitize,",
        "  and multi-pass software overwrite as a universal fallback. The drive",
        "  carrying the running system can never be selected.",
        "",
    ]
    (debian / "control").write_text("\n".join(control), encoding="utf-8")

    # Mark the shipped config as a conffile so a site's organisation block
    # survives an upgrade instead of being silently replaced.
    (debian / "conffiles").write_text("/etc/zeroize/config.json\n", encoding="utf-8")

    for name, body in (("postinst", _DEB_POSTINST), ("postrm", _DEB_POSTRM)):
        script = debian / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    output = DIST_DIR / f"{__slug__}_{__version__}_{architecture}.deb"

    packaged, temporary_root = _packaging_tree(tree)
    try:
        _log(f"Building {output.name}")
        # --root-owner-group avoids needing fakeroot for correct ownership.
        _run(["dpkg-deb", "--root-owner-group", "--build", str(packaged), str(output)])
    finally:
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)

    if not _tool_missing("lintian"):
        _log("Running lintian (advisory)")
        subprocess.run(["lintian", "--no-tag-display-limit", str(output)], check=False)  # noqa: S603

    _log(f"Built {output}")
    return output


# --------------------------------------------------------------------------
# .rpm
# --------------------------------------------------------------------------

def build_rpm() -> Path:
    """Build the .rpm from the staged tree."""
    if _tool_missing("rpmbuild"):
        raise SystemExit(
            "rpmbuild is not installed. Build the .rpm on a Fedora, RHEL or openSUSE "
            "host, or in a container: docker run --rm -v \"$PWD\":/src -w /src "
            "fedora:40 sh -c 'dnf install -y rpm-build python3 && python3 build.py rpm'"
        )

    tree = stage()
    # DEBIAN/ is Debian-only metadata; it must not reach the RPM payload.
    shutil.rmtree(tree / "DEBIAN", ignore_errors=True)

    # rpmbuild copies this tree into the buildroot and preserves its modes, so
    # it needs the same permission-capable source the .deb does.
    tree, temporary_root = _packaging_tree(tree)

    rpm_root = BUILD_DIR / "rpmbuild"
    for directory in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS", "BUILDROOT"):
        (rpm_root / directory).mkdir(parents=True, exist_ok=True)

    spec_path = rpm_root / "SPECS" / f"{__slug__}.spec"
    spec_path.write_text(_render_spec(), encoding="utf-8")

    DIST_DIR.mkdir(parents=True, exist_ok=True)

    _log(f"Building {__slug__}-{__version__}.noarch.rpm")
    _run(
        [
            "rpmbuild",
            "-bb",
            "--define",
            f"_topdir {rpm_root}",
            "--define",
            f"_zeroize_stage {tree}",
            "--buildroot",
            str(rpm_root / "BUILDROOT" / f"{__slug__}-{__version__}"),
            str(spec_path),
        ]
    )

    if temporary_root is not None:
        shutil.rmtree(temporary_root, ignore_errors=True)

    produced = sorted((rpm_root / "RPMS").rglob("*.rpm"))
    if not produced:
        raise SystemExit("rpmbuild reported success but produced no package")

    output = DIST_DIR / produced[-1].name
    shutil.copy2(produced[-1], output)
    _log(f"Built {output}")
    return output


def _render_spec() -> str:
    """The RPM spec. Installs the same tree the .deb does."""
    return f"""%global __python3 /usr/bin/python3
# The payload is a plain file tree assembled by build.py, not a Python
# distribution, so none of rpmbuild's automatic Python machinery applies.
%global debug_package %{{nil}}
%global _build_id_links none
AutoReqProv: no

Name:           {__slug__}
Version:        {__version__}
Release:        1%{{?dist}}
Summary:        {__app_name__} - purge-grade drive erasure with proof

License:        MIT
URL:            https://github.com/zeroize/zeroize
BuildArch:      noarch

{chr(10).join(f"Requires:       {item}" for item in RPM_REQUIRES)}
{chr(10).join(f"Recommends:     {item}" for item in RPM_RECOMMENDS)}

%description
Erases attached drives using a method the drive's own firmware will accept,
and issues a PDF certificate recording what was destroyed, how, and on which
machine.

Reads each drive's capability registers and offers only the methods it
supports: NVMe Sanitize and Format NVM, ATA Secure Erase, SCSI Sanitize, and
multi-pass software overwrite as a universal fallback. The drive carrying the
running system can never be selected.

%prep
# Nothing to unpack - build.py has already staged the tree.

%build
# Nothing to compile.

%install
rm -rf %{{buildroot}}
mkdir -p %{{buildroot}}
cp -a %{{_zeroize_stage}}/. %{{buildroot}}/

%post
if [ -x %{{_bindir}}/gtk-update-icon-cache ]; then
    %{{_bindir}}/gtk-update-icon-cache -q -t -f %{{_datadir}}/icons/hicolor || :
fi
if [ -x %{{_bindir}}/update-desktop-database ]; then
    %{{_bindir}}/update-desktop-database -q || :
fi
install -d -m 0755 /var/log/zeroize
install -d -m 0755 /var/lib/zeroize

%postun
if [ -x %{{_bindir}}/gtk-update-icon-cache ]; then
    %{{_bindir}}/gtk-update-icon-cache -q -t -f %{{_datadir}}/icons/hicolor || :
fi

%files
%{{_bindir}}/zeroize
/usr/lib/zeroize
%{{_datadir}}/applications/io.zeroize.Zeroize.desktop
%{{_datadir}}/polkit-1/actions/io.zeroize.Zeroize.policy
%{{_datadir}}/metainfo/io.zeroize.Zeroize.metainfo.xml
%{{_datadir}}/icons/hicolor/*/apps/io.zeroize.Zeroize*.svg
%{{_mandir}}/man1/zeroize.1*
%dir %{{_datadir}}/doc/zeroize
%{{_datadir}}/doc/zeroize/*
# Marked noreplace so an upgrade never overwrites a site's organisation block.
%config(noreplace) /etc/zeroize/config.json

%changelog
* Sat Sep 26 2026 Zeroize <zeroize@localhost> - {__version__}-1
- First release.
"""


# --------------------------------------------------------------------------
# Live ISO
# --------------------------------------------------------------------------

def build_iso() -> Path:
    """Build the bootable live image by delegating to the ISO build script."""
    script = PACKAGING / "iso" / "build-iso.sh"
    if not script.exists():
        raise SystemExit(f"{script} is missing")

    # A CR in a shell script is not cosmetic: bash reads it as part of the
    # command and the script dies on its first line with "$'\r': command not
    # found". This has already cost one build, and the failure message is
    # obscure enough to be worth catching here with a clear one instead.
    if b"\r\n" in script.read_bytes():
        raise SystemExit(
            f"{script} has Windows line endings, which bash cannot run.\n"
            f"Convert it to LF:  sed -i 's/\\r$//' {script}\n"
            f"The repository's .gitattributes pins *.sh to LF; an editor has overridden it."
        )

    if os.name != "posix":
        raise SystemExit(
            "The live ISO must be built on a Debian or Ubuntu host (or in a container).\n"
            f"See {PACKAGING / 'iso' / 'README.md'} for the container one-liner."
        )

    deb = build_deb()

    # Run a *copy* of the script, not the file in the tree.
    #
    # bash reads a script incrementally, by byte offset. Editing the file while
    # it is executing makes bash resume at its saved offset in the new content,
    # where it will happily interpret the middle of a comment as a command -
    # producing "command not found" at a line number that matches nothing, half
    # an hour into a build. An ISO takes long enough that editing the source
    # meanwhile is a natural thing to do, so the hazard is removed rather than
    # documented.
    snapshot = Path(tempfile.mkdtemp(prefix="zeroize-iso-script-")) / script.name
    shutil.copy2(script, snapshot)

    # The snapshot cannot locate the source tree relative to itself any more,
    # so it is passed explicitly. Without this the script silently skips the
    # boot artwork, which is generated from zeroize/branding.py.
    environment = dict(os.environ)
    environment["ZEROIZE_SOURCE_ROOT"] = str(ROOT)

    _log("Building the live ISO (this takes a while and needs network access)")
    try:
        _run(["bash", str(snapshot), str(deb), str(DIST_DIR)], env=environment)
    finally:
        shutil.rmtree(snapshot.parent, ignore_errors=True)

    images = sorted(DIST_DIR.glob("*.iso"))
    if not images:
        raise SystemExit("the ISO build reported success but produced no image")
    _log(f"Built {images[-1]}")
    return images[-1]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def clean() -> None:
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
        _log(f"Removed {BUILD_DIR}")
    else:
        _log("Nothing to clean")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Build {__app_name__} {__version__}")
    parser.add_argument(
        "target",
        nargs="?",
        default="all",
        choices=["stage", "deb", "rpm", "iso", "all", "clean"],
    )
    arguments = parser.parse_args(argv)

    if arguments.target == "clean":
        clean()
        return 0

    with _BuildLock(BUILD_DIR / ".build.lock"):
        if arguments.target == "stage":
            stage()
        elif arguments.target == "deb":
            build_deb()
        elif arguments.target == "rpm":
            build_rpm()
        elif arguments.target == "iso":
            build_iso()
        else:
            build_deb()
            build_rpm()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
