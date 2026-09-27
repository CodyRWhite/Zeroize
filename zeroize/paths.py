"""Filesystem locations used by the application.

The Logging Standard puts logs under the Windows per-user application data
directory (``%APPDATA%`` / publisher / product). This is a Linux-only tool that
normally runs as root (via ``pkexec``), so the equivalent locations are
resolved here instead of being hard-coded at each call site:

* **Logs** - ``/var/log/zeroize/`` when writable (the normal, privileged
  case), otherwise ``$XDG_STATE_HOME/zeroize/logs`` for unprivileged runs
  such as ``--simulate``.
* **Certificates** - the *invoking* user's home, not root's. Under ``pkexec`` /
  ``sudo`` the process euid is 0 but ``PKEXEC_UID`` / ``SUDO_UID`` still names
  the human who launched it, so certificates land somewhere they can actually
  find them and are chowned back to that user.
* **Configuration** - ``/etc/zeroize/config.json`` (site defaults),
  overlaid by ``$XDG_CONFIG_HOME/zeroize/config.json`` (per-user).

Every accessor creates the directory it returns, and every one of them falls
back to a writable location rather than raising, so a missing ``/var`` mount
degrades the run instead of killing it.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # pragma: no cover - present on every supported target
    import pwd
except ImportError:  # pragma: no cover - lets the module import off-Linux
    # Zeroize only ever runs on Linux, but the certificate renderer and the
    # test suite are developed and reviewed on other platforms. Guarding the
    # import keeps those importable; every accessor below degrades to an
    # environment-variable or cwd fallback when pwd is absent.
    pwd = None  # type: ignore[assignment]

from . import __slug__

_SYSTEM_LOG_DIR = Path("/var/log") / __slug__
_SYSTEM_STATE_DIR = Path("/var/lib") / __slug__
_SYSTEM_CONFIG_FILE = Path("/etc") / __slug__ / "config.json"


def _is_writable_dir(candidate: Path) -> bool:
    """True when *candidate* exists (or can be created) and accepts writes."""
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(candidate, os.W_OK)


def _xdg_dir(env_var: str, default_relative: str) -> Path:
    """Resolve an XDG base directory for the *invoking* user."""
    configured = os.environ.get(env_var)
    if configured:
        return Path(configured)
    return invoking_user_home() / default_relative


def invoking_user_uid() -> int:
    """UID of the human who launched the app, even when running as root.

    ``pkexec`` exports ``PKEXEC_UID``; ``sudo`` exports ``SUDO_UID``. When
    neither is present the process was started directly, so the real uid is
    already the right answer.
    """
    for env_var in ("PKEXEC_UID", "SUDO_UID"):
        raw = os.environ.get(env_var)
        if raw and raw.isdigit():
            return int(raw)
    return os.getuid() if hasattr(os, "getuid") else 0


def invoking_user_name() -> str:
    """Login name matching :func:`invoking_user_uid`, or the uid as a string."""
    if pwd is None:
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    try:
        return pwd.getpwuid(invoking_user_uid()).pw_name
    except KeyError:
        return str(invoking_user_uid())


def invoking_user_home() -> Path:
    """Home directory of the invoking user, falling back to ``/root``."""
    if pwd is None:
        return Path.home()
    try:
        return Path(pwd.getpwuid(invoking_user_uid()).pw_dir)
    except KeyError:
        return Path(os.environ.get("HOME", "/root"))


def log_dir() -> Path:
    """Directory that receives one timestamped CMTrace log per run."""
    if _is_writable_dir(_SYSTEM_LOG_DIR):
        return _SYSTEM_LOG_DIR
    fallback = _xdg_dir("XDG_STATE_HOME", ".local/state") / __slug__ / "logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def certificate_dir(*, output_volume: Path | None = None) -> Path:
    """Directory that receives generated erase certificates.

    Preference order:

    1. *output_volume*, when the caller found a labelled removable volume -
       see :func:`zeroize.discovery.volumes.find_writable_volume`. This is the
       live-USB case, and the only one that survives a power off.
    2. The invoking user's home, so a certificate is not stranded in root's.
    3. The system state directory.
    4. The working directory.
    """
    if output_volume is not None:
        target = output_volume / "Zeroize Certificates"
        if _is_writable_dir(target):
            return target

    preferred = invoking_user_home() / "Zeroize Certificates"
    if _is_writable_dir(preferred):
        return preferred
    system = _SYSTEM_STATE_DIR / "certificates"
    if _is_writable_dir(system):
        return system
    fallback = Path.cwd() / "certificates"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def config_files() -> list[Path]:
    """Config files in increasing precedence order (later overrides earlier)."""
    user_config = _xdg_dir("XDG_CONFIG_HOME", ".config") / __slug__ / "config.json"
    return [_SYSTEM_CONFIG_FILE, user_config]


def hand_back_to_invoking_user(target: Path) -> None:
    """Chown *target* to the invoking user when we created it as root.

    Certificates written by a ``pkexec`` session would otherwise be owned by
    root inside the operator's home directory, where they could neither be
    moved nor deleted without escalating again. Failures are deliberately
    swallowed: the file exists either way, and ownership is a convenience.
    """
    if pwd is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    uid = invoking_user_uid()
    if uid == 0:
        return
    try:
        gid = pwd.getpwuid(uid).pw_gid
    except KeyError:
        return
    try:
        os.chown(target, uid, gid)
    except OSError:
        pass
