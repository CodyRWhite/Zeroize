"""NVMe scope and completion semantics.

Both of these cover failure modes that are silent - the tool reports success,
the certificate says the drive was erased, and it was not. They are the two
most dangerous bugs this codebase can have, so they are tested directly rather
than through the engine.
"""

from __future__ import annotations

import pytest

from zeroize.discovery.nvme import (
    SSTAT_COMPLETED,
    SSTAT_COMPLETED_NO_DEALLOCATE,
    SSTAT_FAILED,
    SSTAT_IN_PROGRESS,
    SSTAT_NEVER_SANITIZED,
    SanitizeLog,
    controller_path_for,
    namespace_id_for,
)
from zeroize.erase.nvme_ops import _format_targets
from zeroize.models import Device, DeviceKind, NvmeCapabilities


def _nvme_device(
    *,
    namespaces: list[int],
    format_applies_to_all: bool,
    path: str = "/dev/nvme0n1",
) -> Device:
    return Device(
        path=path,
        name=path.rsplit("/", 1)[-1],
        kind=DeviceKind.NVME,
        size_bytes=1_000_000_000_000,
        nvme=NvmeCapabilities(
            controller_path="/dev/nvme0",
            namespace_id=1,
            format_supported=True,
            format_applies_to_all_namespaces=format_applies_to_all,
            namespace_count=len(namespaces),
            active_namespaces=namespaces,
        ),
    )


class TestPathMapping:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("/dev/nvme0n1", "/dev/nvme0"),
            ("/dev/nvme3n2", "/dev/nvme3"),
            ("/dev/nvme0n1p3", "/dev/nvme0"),
            ("/dev/nvme0", "/dev/nvme0"),
            ("/dev/sda", "/dev/sda"),
        ],
    )
    def test_controller_path(self, given, expected):
        """Sanitize is issued to the controller; format to the namespace."""
        assert controller_path_for(given) == expected

    @pytest.mark.parametrize(
        ("given", "expected"),
        [("/dev/nvme0n1", 1), ("/dev/nvme0n4", 4), ("/dev/nvme0", 1)],
    )
    def test_namespace_id(self, given, expected):
        assert namespace_id_for(given) == expected


class TestFormatScope:
    """A format covers only the namespace it was sent to, unless FNA bit 0 is set.

    Getting this wrong on a multi-namespace drive erases part of the drive and
    reports complete success - the exact failure that makes a certificate a
    liability rather than a record.
    """

    def test_single_namespace_is_one_command(self):
        device = _nvme_device(namespaces=[1], format_applies_to_all=False)
        targets = _format_targets(device)

        assert len(targets) == 1
        path, argument, _description = targets[0]
        assert path == "/dev/nvme0n1"
        assert argument == "--namespace-id=1"

    def test_multi_namespace_with_broadcast_support_is_one_command(self):
        """FNA bit 0 set: one command, addressed to the controller."""
        device = _nvme_device(namespaces=[1, 2, 3], format_applies_to_all=True)
        targets = _format_targets(device)

        assert len(targets) == 1
        path, argument, _description = targets[0]
        assert path == "/dev/nvme0"
        assert argument == "--namespace-id=0xffffffff"

    def test_multi_namespace_without_broadcast_covers_every_namespace(self):
        """FNA bit 0 clear: one command per namespace, or half the drive survives."""
        device = _nvme_device(namespaces=[1, 2, 3], format_applies_to_all=False)
        targets = _format_targets(device)

        assert len(targets) == 3
        assert [argument for _path, argument, _description in targets] == [
            "--namespace-id=1",
            "--namespace-id=2",
            "--namespace-id=3",
        ]
        # Every command goes to the controller, naming its namespace.
        assert all(path == "/dev/nvme0" for path, _argument, _description in targets)

    def test_namespace_numbers_need_not_be_contiguous(self):
        """Namespaces can be created and deleted, leaving gaps."""
        device = _nvme_device(namespaces=[1, 4, 7], format_applies_to_all=False)
        arguments = [argument for _path, argument, _description in _format_targets(device)]
        assert arguments == ["--namespace-id=1", "--namespace-id=4", "--namespace-id=7"]

    def test_missing_capabilities_falls_back_to_the_given_path(self):
        device = Device(path="/dev/nvme0n2", name="nvme0n2", kind=DeviceKind.NVME)
        targets = _format_targets(device)
        assert targets == [("/dev/nvme0n2", "--namespace-id=2", "/dev/nvme0n2")]

    def test_empty_namespace_list_does_not_produce_zero_commands(self):
        """A failed enumeration must not silently erase nothing."""
        device = _nvme_device(namespaces=[], format_applies_to_all=False)
        targets = _format_targets(device)
        assert len(targets) == 1


class TestSanitizeStatus:
    """A sanitize that stopped running has not necessarily succeeded."""

    def test_completed_is_a_success(self):
        log = SanitizeLog(status=SSTAT_COMPLETED, percent=100.0)
        assert log.succeeded
        assert not log.in_progress

    def test_completed_without_deallocate_is_also_a_success(self):
        log = SanitizeLog(status=SSTAT_COMPLETED_NO_DEALLOCATE, percent=100.0)
        assert log.succeeded

    def test_failed_is_not_a_success_despite_not_running(self):
        """The bug this exists to prevent: 'not in progress' read as 'done'."""
        log = SanitizeLog(status=SSTAT_FAILED, percent=0.0)
        assert not log.in_progress
        assert not log.succeeded
        assert "failed" in log.description

    def test_never_sanitized_is_not_a_success(self):
        log = SanitizeLog(status=SSTAT_NEVER_SANITIZED, percent=0.0)
        assert not log.in_progress
        assert not log.succeeded

    def test_in_progress_is_neither(self):
        log = SanitizeLog(status=SSTAT_IN_PROGRESS, percent=42.0)
        assert log.in_progress
        assert not log.succeeded

    def test_global_data_erased_is_carried_through(self):
        log = SanitizeLog(status=SSTAT_COMPLETED, percent=100.0, global_data_erased=True)
        assert log.global_data_erased


class TestSanitizeCommandRecord:
    """SCDW10 separates a deaf controller from a slow one.

    SSTAT alone cannot: "never sanitized" is what a drive reports both when it
    discarded the command and when it has not updated the log page yet. SCDW10
    records Dword 10 of the last Sanitize command the controller accepted, so
    its action bits naming the action just issued proves the command arrived.

    Observed on a Samsung MZVL4256: SSTAT 0x0000 for thirty seconds, declared a
    failure, then SSTAT 0x1 / SPROG 65535 / SCDW10 0x2 twenty-five seconds
    later. The erase had run and was reported as not having started.
    """

    def test_action_bits_are_matched(self):
        from zeroize.discovery.nvme import SanitizeLog

        log = SanitizeLog(status=0, percent=0.0, raw_scdw10=0x2)
        assert log.records_action(2)
        assert not log.records_action(1)
        assert not log.records_action(4)

    def test_upper_bits_are_ignored(self):
        """Only the low three bits are the action; AUSE and friends sit above."""
        from zeroize.discovery.nvme import SanitizeLog

        log = SanitizeLog(status=0, percent=0.0, raw_scdw10=0x0000_0202)
        assert log.records_action(2)

    def test_an_empty_record_confirms_nothing(self):
        """A controller that never took a command must not look like success."""
        from zeroize.discovery.nvme import SanitizeLog

        log = SanitizeLog(status=0, percent=0.0, raw_scdw10=0)
        assert not log.records_action(0)
        assert not log.records_action(2)


class TestSanitizeStatusSourcing:
    """A field that cannot be found must never be reported as zero.

    This is the error that cost the most in this project. Every poll of a
    Samsung MZVL4256 reported "SSTAT 0x0000 - never sanitized" for five minutes
    while the drive's own log page read SSTAT 0x1, SCDW10 0x2, Global Data
    Erased set: the sanitize had completed. The tool issued a Failed report for
    a drive it had successfully erased.

    The cause was not the drive. ``nvme sanitize-log -o json`` returned the
    status under a key this code did not recognise, ``dict.get`` supplied its
    default of 0, and a missing field became an authoritative "never
    sanitized". Nothing distinguished the two.
    """

    def test_a_present_zero_is_not_a_missing_field(self):
        from zeroize.discovery.nvme import _first_present

        assert _first_present({"sstat": 0}, "sstat") == 0
        assert _first_present({"other": 5}, "sstat") is None

    def test_text_output_is_parsed_when_json_has_no_status(self):
        """The real output from the drive this was found on."""
        from zeroize.discovery.nvme import _parse_sanitize_text

        output = (
            "Sanitize Progress                      (SPROG) :  65535\n"
            "Sanitize Status                        (SSTAT) :  0x1\n"
            "\t[2:0]\tMost Recent Sanitize Command Completed Successfully.\n"
            "\t[8]\tGlobal Data Erased set\n"
            "Sanitize Command Dword 10 Information (SCDW10) :  0x2\n"
        )
        assert _parse_sanitize_text(output) == ("0x1", "65535", "0x2")

    def test_unreadable_output_yields_nothing_rather_than_zero(self):
        from zeroize.discovery.nvme import _parse_sanitize_text

        assert _parse_sanitize_text("") is None
        assert _parse_sanitize_text("nvme: command not found") is None

    def test_a_completed_sanitize_is_recognised_from_text_values(self):
        """End to end: the parsed values must decode to success."""
        from zeroize.discovery.nvme import SSTAT_COMPLETED, SanitizeLog, _coerce_register

        sstat, _sprog, scdw10 = ("0x1", "65535", "0x2")
        log = SanitizeLog(
            status=_coerce_register(sstat) & 0x07,
            percent=100.0,
            raw_sstat=_coerce_register(sstat),
            raw_scdw10=_coerce_register(scdw10),
        )
        assert log.status == SSTAT_COMPLETED
        assert log.succeeded
        assert log.records_action(2)
