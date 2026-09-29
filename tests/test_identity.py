"""Drive identity must survive renumbering, or refuse to proceed.

Device node numbers are positions, not identities. A controller reset, a PCI
rescan and an S3 suspend all renumber, and the freed number can be taken by a
different drive - which is the case these tests exist for. A drive that has
merely gone away fails loudly on its own; a drive that has been *replaced* at
the same path does not, and would be erased in place of the one selected.
"""

from __future__ import annotations

import pytest

from zeroize.discovery import identity
from zeroize.models import Device


def _device(path: str, serial: str) -> Device:
    return Device(name=path.rsplit("/", 1)[-1], path=path, serial=serial)


@pytest.fixture
def sysfs(monkeypatch, tmp_path):
    """A fake /sys/class/block and /dev/disk/by-id the tests can populate."""
    block = tmp_path / "sys" / "class" / "block"
    by_id = tmp_path / "dev" / "disk" / "by-id"
    block.mkdir(parents=True)
    by_id.mkdir(parents=True)
    monkeypatch.setattr(identity, "_SYS_BLOCK", block)
    monkeypatch.setattr(identity, "_BY_ID", by_id)

    def add(name: str, serial: str, wwid: str = "") -> None:
        node = block / name
        (node / "device").mkdir(parents=True)
        (node / "device" / "serial").write_text(serial, encoding="utf-8")
        if wwid:
            (node / "wwid").write_text(wwid, encoding="utf-8")

    return add


def test_identity_is_read_from_sysfs(sysfs):
    sysfs("nvme0n1", "SERIAL-A", "eui.0001")
    assert identity.identity_of("/dev/nvme0n1") == ("SERIAL-A", "eui.0001")


def test_an_absent_device_has_no_identity(sysfs):
    assert identity.identity_of("/dev/nvme7n1") == ("", "")


def test_matching_serial_permits_the_erase(sysfs):
    sysfs("nvme0n1", "SERIAL-A")
    assert identity.confirm_identity(_device("/dev/nvme0n1", "SERIAL-A")) == ""


def test_a_renumbered_drive_is_refused(sysfs):
    """The dangerous case: another drive has taken the selected drive's path.

    Nothing else in the preflight would catch this. The path is valid, the
    device is present, it is not mounted and not the system disk - and it is
    the wrong drive.
    """
    sysfs("nvme0n1", "SERIAL-B")
    sysfs("nvme1n1", "SERIAL-A")

    problem = identity.confirm_identity(_device("/dev/nvme0n1", "SERIAL-A"))

    assert problem, "a swapped serial must not be treated as a match"
    assert "SERIAL-B" in problem
    assert "SERIAL-A" in problem
    # It should also say where the drive actually went, so the operator can
    # act on it rather than just being told no.
    assert "/dev/nvme1n1" in problem


def test_a_vanished_drive_is_refused(sysfs):
    sysfs("nvme1n1", "SERIAL-B")
    problem = identity.confirm_identity(_device("/dev/nvme0n1", "SERIAL-A"))
    assert problem
    assert "SERIAL-A" in problem


def test_current_path_follows_the_serial(sysfs):
    sysfs("nvme0n1", "SERIAL-B")
    sysfs("nvme1n1", "SERIAL-A")
    assert identity.current_path_for("SERIAL-A") == "/dev/nvme1n1"


def test_current_path_is_empty_when_nothing_matches(sysfs):
    sysfs("nvme0n1", "SERIAL-B")
    assert identity.current_path_for("SERIAL-A") == ""


def test_no_serial_cannot_be_confirmed_but_does_not_block(sysfs):
    """A drive that reports no serial has nothing to compare against.

    Allowed through rather than blocked: some USB bridges report no serial at
    all, and refusing every such drive would make the tool useless on them.
    The log records that no check was possible, so the gap is visible rather
    than silent.
    """
    sysfs("sda", "")
    assert identity.confirm_identity(_device("/dev/sda", "")) == ""
