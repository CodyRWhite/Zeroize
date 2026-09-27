"""Certificate naming, content and the multi-drive case.

The specific requirement these cover: when more than one drive is erased in a
run, every serial number must appear. The filename abbreviates past a
configured count, so the tests check that the *document* never does.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from zeroize.certificate.naming import (
    certificate_filename,
    certificate_id,
    format_certificate_datetime,
    sanitise_component,
    serial_component,
)
from zeroize.config import Settings
from zeroize.discovery.simulation import simulated_devices
from zeroize.erase.methods import NVME_FORMAT_CRYPTO, OVERWRITE_DOD_7
from zeroize.models import (
    Device,
    EraseResult,
    JobState,
    PassResult,
    RunSummary,
    format_duration,
    format_size,
)

_START = datetime(2026, 7, 30, 8, 26, 14).astimezone()


def _result(device: Device, *, succeeded: bool = True) -> EraseResult:
    return EraseResult(
        device=device,
        method=NVME_FORMAT_CRYPTO,
        state=JobState.SUCCEEDED if succeeded else JobState.FAILED,
        started_at=_START,
        finished_at=_START + timedelta(seconds=11),
        passes=[PassResult(label="Cryptographic erase (SES=2)", succeeded=succeeded)],
        error_messages=[] if succeeded else ["the controller rejected the command"],
    )


def _summary(results: list[EraseResult]) -> RunSummary:
    return RunSummary(
        results=results,
        started_at=_START,
        finished_at=_START + timedelta(seconds=11),
        operator="C. White",
        machine="5CG22568DQ",
    )


def _device(serial: str, index: int) -> Device:
    return Device(
        path=f"/dev/nvme{index}n1",
        name=f"nvme{index}n1",
        serial=serial,
        model="Test SSD",
        size_bytes=250_059_350_016,
    )


class TestFilename:
    def test_single_drive_matches_the_reference_shape(self):
        summary = _summary([_result(_device("DD56419883A62", 0))])
        name = certificate_filename(summary)
        assert name.startswith("Certificate-DD56419883A62-Success-")
        assert name.endswith(".pdf")

    def test_every_serial_appears_for_a_small_run(self):
        """The stated requirement: list all the wiped serials for the run."""
        serials = ["DD56419883A62", "Y0V0A01ATU18", "BTPY72960ATT"]
        summary = _summary([_result(_device(serial, index)) for index, serial in enumerate(serials)])
        name = certificate_filename(summary, max_serials=4)
        for serial in serials:
            assert serial in name

    def test_large_run_collapses_but_says_how_many(self):
        serials = [f"SERIAL{index:04d}" for index in range(12)]
        summary = _summary([_result(_device(serial, index)) for index, serial in enumerate(serials)])
        name = certificate_filename(summary, max_serials=4)

        assert "and-8-more" in name
        assert all(serial in name for serial in serials[:4])

    def test_filename_stays_within_a_usable_length(self):
        serials = [f"VERYLONGSERIALNUMBER{index:04d}" for index in range(40)]
        summary = _summary([_result(_device(serial, index)) for index, serial in enumerate(serials)])
        assert len(certificate_filename(summary, max_serials=8)) <= 180

    def test_outcome_word_reflects_the_run(self):
        good = _result(_device("AAA", 0))
        bad = _result(_device("BBB", 1), succeeded=False)

        assert "-Success-" in certificate_filename(_summary([good]))
        assert "-Failed-" in certificate_filename(_summary([bad]))
        assert "-Partial-" in certificate_filename(_summary([good, bad]))

    def test_awkward_serials_are_made_safe(self):
        """Serials from the wild contain spaces, slashes and worse."""
        summary = _summary([_result(_device("WDC WD20/EFRX 68", 0))])
        name = certificate_filename(summary)
        assert "/" not in name
        assert " " not in name

    def test_drive_without_a_serial_still_names_something(self):
        device = _device("", 0)
        name = certificate_filename(_summary([_result(device)]))
        assert "nvme0n1" in name


class TestCertificateId:
    def test_is_stable_for_the_same_run(self):
        summary = _summary([_result(_device("DD56419883A62", 0))])
        assert certificate_id(summary) == certificate_id(summary)

    def test_differs_between_runs(self):
        first = _summary([_result(_device("AAA", 0))])
        second = _summary([_result(_device("BBB", 0))])
        assert certificate_id(first) != certificate_id(second)

    def test_has_the_expected_shape(self):
        identifier = certificate_id(_summary([_result(_device("AAA", 0))]))
        assert identifier.startswith("ZRO-")
        assert len(identifier.split("-")) == 3


class TestSummaryModel:
    def test_serials_lists_every_drive_in_order(self):
        serials = ["AAA", "BBB", "CCC"]
        summary = _summary([_result(_device(serial, index)) for index, serial in enumerate(serials)])
        assert summary.serials == serials

    def test_outcome_words(self):
        good = _result(_device("AAA", 0))
        bad = _result(_device("BBB", 1), succeeded=False)

        assert _summary([good]).outcome_word == "Success"
        assert _summary([bad]).outcome_word == "Failed"
        assert _summary([good, bad]).outcome_word == "Partial"
        assert _summary([]).outcome_word == "Empty"

    def test_failed_result_reports_its_error(self):
        result = _result(_device("AAA", 0), succeeded=False)
        assert result.result_word == "Failed"
        assert result.errors_word == "the controller rejected the command"

    def test_successful_result_reports_no_errors(self):
        assert _result(_device("AAA", 0)).errors_word == "No Errors"


class TestRendering:
    """The PDF must build for every shape of run, including the awkward ones."""

    @pytest.fixture(autouse=True)
    def _require_reportlab(self):
        pytest.importorskip("reportlab")

    def _render(self, tmp_path, summary):
        from zeroize.certificate.pdf import render_certificate

        target = tmp_path / certificate_filename(summary)
        render_certificate(summary, Settings(), target)
        assert target.exists()
        assert target.stat().st_size > 2000
        return target

    def test_renders_a_single_drive(self, tmp_path):
        self._render(tmp_path, _summary([_result(_device("DD56419883A62", 0))]))

    def test_renders_a_multi_drive_run(self, tmp_path):
        devices = simulated_devices()[:3]
        self._render(tmp_path, _summary([_result(device) for device in devices]))

    def test_renders_a_failed_multi_pass_overwrite(self, tmp_path):
        device = simulated_devices()[4]
        result = EraseResult(
            device=device,
            method=OVERWRITE_DOD_7,
            state=JobState.FAILED,
            started_at=_START,
            finished_at=_START + timedelta(hours=3),
            passes=[
                PassResult(label=spec.label, succeeded=index < 2, detail="" if index < 2 else "I/O error")
                for index, spec in enumerate(OVERWRITE_DOD_7.passes)
            ],
            error_messages=["write failed at byte 1284378624000: [Errno 5] Input/output error"],
        )
        self._render(tmp_path, _summary([result]))

    def test_renders_a_drive_with_no_partitions(self, tmp_path):
        device = _device("BTPY72960ATT256D", 2)
        assert device.partitions == []
        self._render(tmp_path, _summary([_result(device)]))

    def test_renders_with_a_populated_organisation_block(self, tmp_path):
        from zeroize.certificate.pdf import render_certificate

        settings = Settings()
        settings.organisation.name = "Contoso Asset Disposal"
        settings.organisation.address_lines = ["1200 Industrial Parkway", "Burlington, ON"]
        settings.organisation.customer = "Northwind Traders"
        settings.organisation.reference = "WO-2026-04417"

        summary = _summary([_result(_device("DD56419883A62", 0))])
        target = tmp_path / "with-org.pdf"
        render_certificate(summary, settings, target)
        assert target.exists()

    def test_json_sidecar_lists_every_serial(self, tmp_path):
        import json

        from zeroize.certificate.pdf import write_json_sidecar

        serials = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        summary = _summary([_result(_device(serial, index)) for index, serial in enumerate(serials)])

        target = write_json_sidecar(summary, Settings(), tmp_path / "run.json")
        payload = json.loads(target.read_text(encoding="utf-8"))

        # The filename may abbreviate; the data never does.
        assert payload["serials"] == serials
        assert len(payload["drives"]) == len(serials)


class TestFormatting:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (250_059_350_016, "250 GB"),
            (1_920_383_410_176, "1.9 TB"),
            (2_000_398_934_016, "2.0 TB"),
        ],
    )
    def test_sizes_use_vendor_units(self, given, expected):
        assert format_size(given) == expected

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(0, "00:00:00"), (11, "00:00:11"), (6129, "01:42:09"), (35_537, "09:52:17")],
    )
    def test_durations_match_the_certificate_format(self, given, expected):
        assert format_duration(given) == expected

    def test_certificate_datetime_is_spelled_out(self):
        date_text, time_text = format_certificate_datetime(datetime(2026, 7, 30, 10, 8, 22))
        assert date_text == "July 30, 2026"
        assert time_text == "10:08"

    def test_sanitise_component_falls_back(self):
        assert sanitise_component("   ", fallback="none") == "none"
        assert sanitise_component("a b/c") == "a-b-c"

    def test_serial_component_handles_an_empty_run(self):
        assert serial_component(_summary([])) == "no-drives"
