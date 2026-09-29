"""Controller state the capability registers do not tell you.

Two things bit this tool on real hardware, and both look like the same mistake
from a distance: a value that could not be read being treated as a value of
zero, and a capability that is advertised being treated as a capability that
works.
"""

from __future__ import annotations

import pytest

from zeroize.discovery import nvme
from zeroize.erase import methods
from zeroize.models import Device, DeviceKind, NvmeCapabilities

# ---------------------------------------------------------------------------
# The sanitize log, as nvme-cli 2.x actually emits it
# ---------------------------------------------------------------------------
_NESTED_LOG = {
    "nvme0": {
        "sprog": 65535,
        "sstat": {
            "global_erased": 1,
            "no_cmplted_passes": 0,
            "status": "(1) Most Recent Sanitize Command Completed Successfully.",
        },
        "cdw10_info": 2,
    }
}


def test_sanitize_log_is_read_from_the_device_keyed_envelope(monkeypatch):
    """nvme-cli nests the log under the device name, and SSTAT under itself.

    Looking at the top level finds nothing and falls back to parsing the text
    output - whose printed SSTAT has bit 8 masked off, so Global Data Erased
    decodes false on every drive. A purged drive was certified as one whose
    controller had not set the bit.
    """
    monkeypatch.setattr(nvme, "_nvme_json", lambda _: _NESTED_LOG)

    log = nvme.read_sanitize_status("/dev/nvme0")

    assert log is not None
    assert log.status == nvme.SSTAT_COMPLETED
    assert log.global_data_erased is True, "the nested global_erased field was ignored"
    assert log.raw_scdw10 == 2, "cdw10_info was hidden by the device envelope"
    assert log.percent == 100.0


def test_flat_sanitize_log_still_works(monkeypatch):
    """Older nvme-cli put the fields at the top level; both shapes must parse."""
    monkeypatch.setattr(
        nvme, "_nvme_json", lambda _: {"sstat": 0x101, "sprog": 65535, "cdw10_info": 2}
    )

    log = nvme.read_sanitize_status("/dev/nvme0")

    assert log is not None
    assert log.status == nvme.SSTAT_COMPLETED
    assert log.global_data_erased is True


def test_an_unreadable_status_is_not_reported_as_never_sanitized(monkeypatch):
    """Absent is not zero. Zero means "never sanitized", which is a claim."""
    monkeypatch.setattr(nvme, "_nvme_json", lambda _: {"nvme0": {"sprog": 0}})
    monkeypatch.setattr(
        nvme, "run", lambda *a, **k: type("R", (), {"ok": False, "stdout": "", "stderr": ""})()
    )

    assert nvme.read_sanitize_status("/dev/nvme0") is None


# ---------------------------------------------------------------------------
# Advertised is not accepted
# ---------------------------------------------------------------------------
class _Result:
    def __init__(self, ok: bool, stdout: str = "", stderr: str = "") -> None:
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("NVMe status: Access Denied: ...(0x6286)", False),
        ("NVMe status: ACCESS_DENIED: ...(0x6286)", False),
        # Invalid Field refuses the ARGUMENT, not the command: Exit Failure
        # Mode on a drive with no failed sanitize has nothing to do. Treating
        # it as a refusal would report a perfectly usable drive as blocked.
        ("NVMe status: Invalid Field in Command: ...(0x2002)", True),
        ("", True),
    ],
)
def test_sanitize_probe_classifies_on_the_message(monkeypatch, stderr, expected):
    monkeypatch.setattr(nvme, "tool_available", lambda _: True)
    monkeypatch.setattr(nvme, "run", lambda *a, **k: _Result(not stderr, "", stderr))

    assert nvme.probe_sanitize_reachable("/dev/nvme0") is expected


def test_a_probe_that_could_not_run_is_unknown_not_blocked(monkeypatch):
    """Unknown must stay distinct from blocked, or "we did not check" reads
    as "we checked and it is fine" - or worse, the reverse."""
    monkeypatch.setattr(nvme, "tool_available", lambda _: False)
    assert nvme.probe_sanitize_reachable("/dev/nvme0") is None


def test_blocked_sanitize_is_surfaced_as_unavailable():
    capabilities = NvmeCapabilities(
        sanitize_block_supported=True,
        sanitize_reachable=False,
    )
    assert capabilities.sanitize_blocked is True

    capabilities.sanitize_reachable = True
    assert capabilities.sanitize_blocked is False

    # Never probed is not blocked.
    capabilities.sanitize_reachable = None
    assert capabilities.sanitize_blocked is False


# ---------------------------------------------------------------------------
# Discard is gated on DLFEAT for NVMe, not on ATA fields
# ---------------------------------------------------------------------------
def _nvme_device(dlfeat_behaviour: int) -> Device:
    return Device(
        name="nvme0n1",
        path="/dev/nvme0n1",
        kind=DeviceKind.NVME,
        rotational=False,
        nvme=NvmeCapabilities(deallocated_read_behaviour=dlfeat_behaviour),
    )


def test_nvme_discard_is_refused_without_a_guarantee():
    """DLFEAT 0 means the controller promises nothing about a deallocated read.

    A clean read-back after a discard then proves nothing: the drive may return
    the old contents after a power cycle or a garbage-collection pass.
    """
    reason = methods._discard_reason(_nvme_device(0), None)
    assert "DLFEAT 0" in reason
    # And it must not blame TRIM support, which is an ATA field that NVMe
    # discovery never populates - the previous wording said the drive did not
    # report TRIM on hardware whose ONCS said otherwise.
    assert "does not report TRIM support" not in reason


def test_nvme_discard_is_allowed_when_zeros_are_guaranteed():
    assert methods._discard_reason(_nvme_device(1), None) == ""


def test_nvme_discard_is_refused_when_deallocated_blocks_read_ones():
    reason = methods._discard_reason(_nvme_device(2), None)
    assert reason
    assert "zeros" in reason
