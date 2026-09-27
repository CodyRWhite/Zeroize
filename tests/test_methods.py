"""Capability gating - the rules deciding what a drive is allowed to do.

These are the tests that matter most. Offering a method a drive cannot perform
wastes a bench slot; *failing* to offer one it can perform pushes an operator
towards a slower, less complete erase. Both are gating bugs, and both are
invisible without hardware that exhibits the case - which is what the simulated
device set exists to provide.
"""

from __future__ import annotations

import pytest

from zeroize.discovery.simulation import simulated_devices
from zeroize.erase.methods import (
    ATA_SECURE_ERASE,
    ATA_SECURE_ERASE_ENHANCED,
    NVME_FORMAT_CRYPTO,
    NVME_FORMAT_USER_DATA,
    NVME_SANITIZE_BLOCK,
    NVME_SANITIZE_CRYPTO,
    NVME_SANITIZE_OVERWRITE,
    OVERWRITE_DOD_7,
    OVERWRITE_GUTMANN_35,
    OVERWRITE_ZERO,
    availability_for,
    common_methods,
    method_caution,
    recommended_method,
    supported_methods,
)


@pytest.fixture
def devices() -> dict[str, object]:
    return {device.path: device for device in simulated_devices()}


def _keys(device) -> set[str]:
    return {method.key for method in supported_methods(device)}


def _reason(device, method) -> str:
    verdict = next(item for item in availability_for(device) if item.method.key == method.key)
    return verdict.reason


class TestNvmeGating:
    def test_consumer_drive_offers_format_but_not_sanitize(self, devices):
        """Format NVM with crypto erase, SANICAP zero - the commonest real case."""
        device = devices["/dev/nvme0n1"]
        available = _keys(device)

        assert NVME_FORMAT_USER_DATA.key in available
        assert NVME_FORMAT_CRYPTO.key in available
        assert NVME_SANITIZE_BLOCK.key not in available
        assert NVME_SANITIZE_CRYPTO.key not in available
        assert NVME_SANITIZE_OVERWRITE.key not in available

    def test_unsupported_sanitize_explains_itself(self, devices):
        """An unavailable method carries a reason, for the greyed-out tooltip."""
        reason = _reason(devices["/dev/nvme0n1"], NVME_SANITIZE_BLOCK)
        assert "SANICAP" in reason

    def test_enterprise_drive_offers_every_sanitize_action(self, devices):
        available = _keys(devices["/dev/nvme1n1"])
        assert NVME_SANITIZE_CRYPTO.key in available
        assert NVME_SANITIZE_BLOCK.key in available
        assert NVME_SANITIZE_OVERWRITE.key in available

    def test_drive_without_format_support_offers_no_nvme_method(self, devices):
        """OACS bit 1 clear means neither SES value is possible."""
        device = devices["/dev/nvme2n1"]
        available = _keys(device)

        assert NVME_FORMAT_USER_DATA.key not in available
        assert NVME_FORMAT_CRYPTO.key not in available
        assert "OACS" in _reason(device, NVME_FORMAT_USER_DATA)

        # It can still be overwritten in software - that is the fallback's job.
        assert OVERWRITE_ZERO.key in available

    def test_crypto_erase_needs_both_bits(self, devices):
        """FNA bit 2 alone is not enough; Format NVM must also be supported."""
        device = devices["/dev/nvme2n1"]
        device.nvme.raw_fna = 0x04
        device.nvme.crypto_erase_supported = False  # as discovery would compute it
        assert NVME_FORMAT_CRYPTO.key not in _keys(device)

    def test_ata_methods_never_offered_for_nvme(self, devices):
        device = devices["/dev/nvme1n1"]
        assert ATA_SECURE_ERASE.key not in _keys(device)
        assert "NVMe" in _reason(device, ATA_SECURE_ERASE)


class TestAtaGating:
    def test_frozen_drive_blocks_secure_erase_with_instructions(self, devices):
        """A frozen drive is the single most common ATA failure in practice.

        The reason has to be actionable. "Power-cycle it" is not, on a sealed
        laptop - the detach-and-rescan below is what actually clears it, and
        was confirmed on HP hardware.
        """
        device = devices["/dev/sda"]
        assert ATA_SECURE_ERASE.key not in _keys(device)

        reason = _reason(device, ATA_SECURE_ERASE)
        assert "frozen" in reason
        assert "zeroize unfreeze" in reason
        assert "scsi_host" in reason

    def test_frozen_drive_can_still_be_overwritten(self, devices):
        """Being frozen blocks the firmware command, not the software one."""
        assert OVERWRITE_DOD_7.key in _keys(devices["/dev/sda"])

    def test_unfrozen_drive_offers_both_secure_erase_variants(self, devices):
        device = devices["/dev/sdb"]
        available = _keys(device)
        assert ATA_SECURE_ERASE.key in available
        assert ATA_SECURE_ERASE_ENHANCED.key in available


class TestProtection:
    def test_system_disk_offers_nothing_at_all(self, devices):
        """The interlock is absolute: no method, whatever the hardware allows."""
        device = devices["/dev/sdc"]
        assert _keys(device) == set()
        assert recommended_method(device) is None

    def test_every_method_names_the_protection_reason(self, devices):
        device = devices["/dev/sdc"]
        for verdict in availability_for(device):
            assert not verdict.supported
            assert "/run/live/medium" in verdict.reason


class TestRecommendation:
    def test_prefers_sanitize_crypto_when_available(self, devices):
        """NIST SP 800-88r1 order: sanitize crypto beats everything else."""
        assert recommended_method(devices["/dev/nvme1n1"]).key == NVME_SANITIZE_CRYPTO.key

    def test_falls_back_to_format_crypto(self, devices):
        assert recommended_method(devices["/dev/nvme0n1"]).key == NVME_FORMAT_CRYPTO.key

    def test_falls_back_to_overwrite_when_firmware_offers_nothing(self, devices):
        recommended = recommended_method(devices["/dev/nvme2n1"])
        assert recommended is not None
        assert recommended.family == "overwrite"

    def test_never_recommends_a_method_the_drive_rejects(self, devices):
        for device in devices.values():
            recommended = recommended_method(device)
            if recommended is not None:
                assert recommended.key in _keys(device)


class TestCommonMethods:
    def test_method_unsupported_by_one_drive_is_unsupported_for_the_batch(self, devices):
        selection = [devices["/dev/nvme0n1"], devices["/dev/nvme1n1"]]
        verdicts = {item.method.key: item for item in common_methods(selection)}

        # Only the enterprise drive supports sanitize, so the batch cannot.
        assert not verdicts[NVME_SANITIZE_BLOCK.key].supported
        assert "nvme0n1" in verdicts[NVME_SANITIZE_BLOCK.key].reason

        # Both support format crypto erase.
        assert verdicts[NVME_FORMAT_CRYPTO.key].supported

    def test_overwrite_is_common_to_every_erasable_drive(self, devices):
        erasable = [device for device in devices.values() if device.can_be_erased]
        verdicts = {item.method.key: item for item in common_methods(erasable)}
        assert verdicts[OVERWRITE_ZERO.key].supported

    def test_empty_selection_returns_nothing(self):
        assert common_methods([]) == []


class TestPassPatterns:
    def test_dod_seven_pass_labels_match_the_reference_certificate(self):
        """These strings are printed verbatim on the certificate."""
        labels = [spec.label for spec in OVERWRITE_DOD_7.passes]
        assert labels == [
            "Pass 1 (0x000000000000)",
            "Pass 2 (0xFFFFFFFFFFFF)",
            "Pass 3 (Random)",
            "Pass 4 (0x969696969696)",
            "Pass 5 (0x000000000000)",
            "Pass 6 (0xFFFFFFFFFFFF)",
            "Pass 7 (Random)",
        ]

    def test_gutmann_has_thirty_five_passes(self):
        assert OVERWRITE_GUTMANN_35.pass_count == 35

    def test_gutmann_opens_and_closes_with_random_passes(self):
        passes = OVERWRITE_GUTMANN_35.passes
        assert all(spec.pattern is None for spec in passes[:4])
        assert all(spec.pattern is None for spec in passes[-4:])
        assert all(spec.pattern is not None for spec in passes[4:31])

    def test_only_the_last_pass_is_verified(self):
        """Verifying an intermediate pass would be overwritten by the next one."""
        for method in (OVERWRITE_DOD_7, OVERWRITE_GUTMANN_35, OVERWRITE_ZERO):
            flags = [spec.verify for spec in method.passes]
            assert flags[-1] is True
            assert not any(flags[:-1])


class TestCautions:
    def test_overwrite_on_flash_carries_a_caution(self, devices):
        caution = method_caution(devices["/dev/nvme0n1"], OVERWRITE_DOD_7)
        assert "wear levelling" in caution.lower() or "wear levelling" in caution

    def test_overwrite_on_a_spinning_disk_carries_none(self, devices):
        assert method_caution(devices["/dev/sdb"], OVERWRITE_DOD_7) == ""

    def test_crypto_erase_warns_about_unencrypted_history(self, devices):
        caution = method_caution(devices["/dev/nvme0n1"], NVME_FORMAT_CRYPTO)
        assert "encryption key" in caution

    def test_a_caution_is_never_a_blocker(self, devices):
        """Every cautioned method is still offered."""
        device = devices["/dev/nvme0n1"]
        assert method_caution(device, OVERWRITE_DOD_7)
        assert OVERWRITE_DOD_7.key in _keys(device)
