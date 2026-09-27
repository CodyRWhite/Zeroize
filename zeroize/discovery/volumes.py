"""Finding the writable volume that certificates are written to.

On a live USB the filesystem is a tmpfs and the boot medium is a read-only
ISO 9660 image, so a certificate written to either is gone at power off. The
answer is a separate writable volume carrying an agreed label, which the
operator plugs in: either a second partition on the boot stick, or a second
stick.

Finding it is fiddlier than it first appears, which is why this is a module
rather than two lines:

* **Labels are not unique.** Nothing stops two mounted filesystems sharing
  one, and ``/dev/disk/by-label/<name>`` is a single symlink - with a
  duplicate, udev points it at whichever device it processed last, and the
  other becomes invisible. The boot medium is itself labelled, so a collision
  here is not hypothetical. Every mounted filesystem is therefore enumerated
  and matched, not just the one the symlink happens to name.
* **The boot medium must never be chosen**, even if it matches. It is
  read-only, so writing would fail - but failing late, after an erase, is much
  worse than not selecting it in the first place.

``lsblk`` is the source of truth, as it is everywhere else in this package; it
reads the filesystem superblock rather than trusting a symlink.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..logging_setup import get_logger
from ..process import run, tool_available

_log = get_logger("Volumes")

#: Mount points that belong to the live boot medium. A volume mounted at any of
#: these is the medium Zeroize is running from and is never an output target.
_LIVE_MEDIUM_MOUNTPOINTS = frozenset(
    {
        "/run/live/medium",
        "/lib/live/mount/medium",
        "/cdrom",
        "/run/initramfs/live",
        "/run/rootfsbase",
    }
)


def _walk(nodes: list[dict]):
    """Yield every node in an lsblk tree, depth first."""
    for node in nodes or []:
        yield node
        yield from _walk(node.get("children", []) or [])


def _mountpoints(node: dict) -> list[str]:
    """Collect a node's mount points across lsblk's singular/plural fields."""
    found: list[str] = []
    single = node.get("mountpoint")
    if single:
        found.append(str(single))
    plural = node.get("mountpoints")
    if isinstance(plural, list):
        found.extend(str(entry) for entry in plural if entry)
    return found


def _is_read_only(node: dict) -> bool:
    value = node.get("ro")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False


def find_writable_volume(label: str) -> Path | None:
    """Return the mount point of a writable, mounted volume labelled *label*.

    Returns ``None`` when nothing matches, when the only matches are read-only,
    or when every match is the live boot medium. The caller falls back to a
    local directory rather than treating this as an error - a missing output
    stick must never stop a drive being erased.
    """
    if not label:
        return None
    if not tool_available("lsblk"):
        _log.warning("lsblk is not installed; cannot locate the %s volume", label)
        return None

    import json

    result = run(
        [
            "lsblk",
            "--json",
            "--paths",
            "--output",
            "PATH,LABEL,MOUNTPOINT,MOUNTPOINTS,RO,FSTYPE",
        ],
        timeout=30.0,
        log_output=False,
    )
    if not result.ok:
        _log.warning("Could not enumerate volumes: %s", result.failure_summary)
        return None

    try:
        tree = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        _log.warning("Unparseable lsblk output while looking for %s: %s", label, error)
        return None

    wanted = label.strip().casefold()
    rejected: list[str] = []

    for node in _walk(tree.get("blockdevices", [])):
        node_label = str(node.get("label") or "").strip()
        if node_label.casefold() != wanted:
            continue

        path = str(node.get("path") or "unknown")

        for mountpoint in _mountpoints(node):
            normalised = mountpoint.rstrip("/") or "/"
            if normalised in _LIVE_MEDIUM_MOUNTPOINTS:
                rejected.append(f"{path} at {mountpoint} is the live boot medium")
                continue
            if _is_read_only(node):
                rejected.append(f"{path} at {mountpoint} is read-only")
                continue
            if not os.path.isdir(mountpoint) or not os.access(mountpoint, os.W_OK):
                rejected.append(f"{path} at {mountpoint} is not writable")
                continue

            _log.info("Using %s (%s) as the certificate output volume", mountpoint, path)
            return Path(mountpoint)

        if not _mountpoints(node):
            rejected.append(f"{path} is labelled {label} but is not mounted")

    for reason in rejected:
        _log.info("Not using %s as the output volume: %s", label, reason)
    if not rejected:
        _log.info("No volume labelled %s is attached", label)

    return None
