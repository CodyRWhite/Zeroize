"""The interlocks, the configuration layering, and the overwrite sampling.

Everything here guards against a destructive mistake rather than a cosmetic
one, so the assertions are deliberately blunt.
"""

from __future__ import annotations

import pytest

from zeroize.config import Settings, load_settings
from zeroize.discovery.block_devices import (
    _apply_protection,
    _build_device,
    _classify,
    _should_list,
)
from zeroize.erase.overwrite import _fill_buffer, _verification_offsets
from zeroize.erase.verify import _find_signature, _sample_offsets
from zeroize.models import DeviceKind

#: Passed to _apply_protection so the protection tests see only the fixture.
#:
#: Without this the check reads the REAL /proc/self/mountinfo, and a fixture
#: device called /dev/sda resolves to whatever /dev/sda is on the machine
#: running the tests. That passes on a laptop with no such disk and fails on a
#: CI runner rooted on /dev/sda1 - the interlock firing correctly, on the
#: wrong mounts.
_NO_HOST_MOUNTS: dict[str, list[str]] = {}
_NO_HOST_SWAP: set[str] = set()


def _protect(devices) -> None:
    """Apply protection using the fixture's own view of the world."""
    _apply_protection(devices, swap_sources=set(_NO_HOST_SWAP), mount_table=dict(_NO_HOST_MOUNTS))


def _lsblk_node(**overrides) -> dict:
    node = {
        "name": "sda",
        "path": "/dev/sda",
        "type": "disk",
        "size": 500_107_862_016,
        "model": "CT500MX500SSD1",
        "serial": "2015E4A1B2C3",
        "tran": "sata",
        "rota": True,
        "log-sec": 512,
        "phy-sec": 512,
        "pttype": "gpt",
        "children": [],
    }
    node.update(overrides)
    return node


class TestSystemDiskProtection:
    """The drive carrying the running system must never become selectable."""

    @pytest.mark.parametrize("mountpoint", ["/", "/boot", "/boot/efi", "/usr", "/var"])
    def test_critical_mountpoints_protect_the_whole_drive(self, mountpoint):
        node = _lsblk_node(
            children=[
                {
                    "name": "sda1",
                    "path": "/dev/sda1",
                    "size": 500_000_000_000,
                    "fstype": "ext4",
                    "mountpoint": mountpoint,
                }
            ]
        )
        device = _build_device(node)
        _protect([device])

        assert device.is_system
        assert not device.can_be_erased
        assert mountpoint in device.protection_reason

    def test_live_medium_protects_the_boot_usb(self):
        """Booted from USB, the stick must not appear erasable."""
        node = _lsblk_node(
            path="/dev/sdc",
            name="sdc",
            tran="usb",
            children=[
                {
                    "name": "sdc1",
                    "path": "/dev/sdc1",
                    "size": 61_000_000_000,
                    "fstype": "vfat",
                    "mountpoint": "/run/live/medium",
                }
            ],
        )
        device = _build_device(node)
        _protect([device])
        assert device.is_system

    def test_protection_reaches_through_nested_mappings(self):
        """Root on LVM on LUKS still protects the physical drive beneath it."""
        node = _lsblk_node(
            children=[
                {
                    "name": "sda1",
                    "path": "/dev/sda1",
                    "size": 500_000_000_000,
                    "fstype": "crypto_LUKS",
                    "children": [
                        {
                            "name": "luks-root",
                            "path": "/dev/mapper/luks-root",
                            "size": 499_000_000_000,
                            "children": [
                                {
                                    "name": "vg-root",
                                    "path": "/dev/mapper/vg-root",
                                    "size": 400_000_000_000,
                                    "fstype": "ext4",
                                    "mountpoint": "/",
                                }
                            ],
                        }
                    ],
                }
            ]
        )
        device = _build_device(node)
        _protect([device])
        assert device.is_system

    @pytest.mark.parametrize("mountpoint", ["/", "/boot", "/var"])
    def test_whole_device_filesystem_protects_the_drive(self, mountpoint):
        """A disk with no partition table can carry the root filesystem directly.

        Found on a real system: WSL mounts its root disk as the whole device,
        with no partition table at all. A protection check that only walks
        partitions finds nothing to object to and offers the running system as
        erasable. mdadm members, ZFS vdevs and anything formatted with
        `mkfs /dev/sdb` have the same shape.
        """
        node = _lsblk_node(pttype="", fstype="ext4", mountpoint=mountpoint, children=[])
        device = _build_device(node)
        _protect([device])

        assert device.mountpoints == [mountpoint]
        assert device.is_mounted
        assert device.is_system
        assert not device.can_be_erased
        assert mountpoint in device.protection_reason

    def test_whole_device_mountpoints_plural_field_is_read(self):
        """Newer lsblk reports every mount in `mountpoints`, not `mountpoint`."""
        node = _lsblk_node(
            pttype="",
            fstype="ext4",
            mountpoints=["/", "/mnt/wslg/distro"],
            children=[],
        )
        device = _build_device(node)
        _protect([device])
        assert device.is_system

    def test_whole_device_mounted_somewhere_harmless_is_still_erasable(self):
        """Mounted is not the same as system - the engine unmounts and proceeds."""
        node = _lsblk_node(pttype="", fstype="ext4", mountpoint="/mnt/scratch", children=[])
        device = _build_device(node)
        _protect([device])

        assert device.is_mounted
        assert not device.is_system
        assert device.can_be_erased

    def test_unpartitioned_unmounted_device_is_erasable(self):
        node = _lsblk_node(pttype="", children=[])
        device = _build_device(node)
        _protect([device])

        assert not device.is_mounted
        assert device.can_be_erased

    def test_a_data_drive_is_not_protected(self):
        node = _lsblk_node(
            children=[
                {
                    "name": "sda1",
                    "path": "/dev/sda1",
                    "size": 500_000_000_000,
                    "fstype": "ext4",
                    "mountpoint": "/mnt/archive",
                }
            ]
        )
        device = _build_device(node)
        _protect([device])

        assert not device.is_system
        assert device.can_be_erased
        # It is still mounted, which the engine handles separately.
        assert device.is_mounted

    def test_an_unmounted_drive_is_erasable(self):
        device = _build_device(_lsblk_node())
        _protect([device])
        assert device.can_be_erased
        assert not device.is_mounted

    def test_read_only_devices_are_never_erasable(self):
        device = _build_device(_lsblk_node(ro=True))
        _protect([device])
        assert not device.can_be_erased


class TestClassification:
    @pytest.mark.parametrize(
        ("node", "expected"),
        [
            ({"tran": "nvme", "name": "nvme0n1"}, DeviceKind.NVME),
            ({"tran": "sata", "name": "sda"}, DeviceKind.ATA),
            ({"tran": "usb", "name": "sdb"}, DeviceKind.USB),
            ({"tran": "sas", "name": "sdc"}, DeviceKind.SCSI),
            ({"tran": "mmc", "name": "mmcblk0"}, DeviceKind.MMC),
        ],
    )
    def test_transport_maps_to_kind(self, node, expected):
        assert _classify(node) == expected

    def test_nvme_recognised_without_a_transport_field(self):
        """Some lsblk builds leave tran empty for NVMe."""
        assert _classify({"name": "/dev/nvme0n1", "tran": ""}) == DeviceKind.NVME

    def test_partitions_are_not_listed_as_drives(self):
        assert not _should_list({"type": "part", "size": 1000, "path": "/dev/sda1"}, False)

    def test_zero_sized_devices_are_skipped(self):
        """An empty card reader slot reports as a zero-byte disk."""
        assert not _should_list({"type": "disk", "size": 0, "path": "/dev/sdd"}, False)

    def test_loop_devices_are_hidden_unless_asked_for(self):
        node = {"type": "loop", "size": 1000, "path": "/dev/loop0"}
        assert not _should_list(node, False)
        assert _should_list(node, True)


class TestVerificationSampling:
    def test_first_and_last_blocks_are_always_sampled(self):
        """Where the partition table and the backup GPT header live."""
        offsets = _verification_offsets(10_000, percent=1)
        assert 0 in offsets
        assert 9_999 in offsets

    def test_full_verification_covers_every_block(self):
        assert _verification_offsets(500, percent=100) == set(range(500))

    def test_zero_percent_samples_nothing(self):
        assert _verification_offsets(10_000, percent=0) == set()

    def test_sample_count_is_capped_for_huge_drives(self):
        """A 20 TB drive must not spend unbounded memory on digests."""
        offsets = _verification_offsets(5_000_000, percent=50)
        assert len(offsets) <= 20_000

    def test_sample_count_tracks_the_requested_percentage(self):
        sparse = _verification_offsets(100_000, percent=1)
        dense = _verification_offsets(100_000, percent=10)
        assert len(dense) > len(sparse)

    def test_fill_buffer_repeats_the_pattern_exactly(self):
        assert _fill_buffer(b"\x96", 8) == b"\x96" * 8
        assert _fill_buffer(b"\x92\x49\x24", 7) == b"\x92\x49\x24\x92\x49\x24\x92"


class TestSignatureDetection:
    """The post-erase check looks for structures that should not have survived."""

    def test_finds_an_mbr_boot_signature(self):
        window = bytearray(4096)
        window[0x1FE:0x200] = b"\x55\xaa"
        assert "MBR" in _find_signature(bytes(window))

    def test_finds_a_gpt_header_anywhere_in_the_window(self):
        window = bytearray(4096)
        window[2048:2056] = b"EFI PART"
        assert "GPT" in _find_signature(bytes(window))

    def test_finds_an_ext_superblock_at_its_offset(self):
        window = bytearray(4096)
        window[0x438:0x43A] = b"\x53\xef"
        assert "ext" in _find_signature(bytes(window))

    def test_finds_a_luks_header(self):
        assert "LUKS" in _find_signature(b"LUKS\xba\xbe" + bytes(1024))

    def test_zeroed_media_yields_nothing(self):
        assert _find_signature(bytes(1024 * 1024)) == ""

    def test_random_media_yields_nothing(self):
        import os

        assert _find_signature(os.urandom(65536)) == ""

    def test_sample_offsets_cover_head_and_tail(self):
        size = 1_000_000_000_000
        offsets = _sample_offsets(size, percent=1)
        assert offsets[0] == 0
        assert offsets[-1] >= size - 1024 * 1024


class TestConfiguration:
    def test_defaults_are_safe(self):
        """Every interlock defaults to on."""
        settings = Settings()
        assert settings.safety.require_typed_confirmation
        assert settings.safety.confirmation_phrase == "ERASE"
        assert not settings.safety.allow_mounted_devices
        assert settings.safety.require_operator_name

    def test_organisation_block_starts_empty(self):
        """Zeroize ships unbranded; the site fills this in."""
        organisation = Settings().organisation
        assert organisation.name == ""
        assert organisation.address_lines == []

    def test_overrides_merge_without_clobbering_siblings(self, monkeypatch, tmp_path):
        import json

        site = tmp_path / "site.json"
        site.write_text(
            json.dumps(
                {
                    "organisation": {"name": "Contoso", "contact_email": "it@contoso.example"},
                    "erase": {"verification_percent": 25},
                }
            ),
            encoding="utf-8",
        )
        user = tmp_path / "user.json"
        user.write_text(json.dumps({"organisation": {"operator": "C. White"}}), encoding="utf-8")

        monkeypatch.setattr("zeroize.config.config_files", lambda: [site, user])
        settings = load_settings()

        # The user file set only the operator; the site's fields survive.
        assert settings.organisation.operator == "C. White"
        assert settings.organisation.name == "Contoso"
        assert settings.organisation.contact_email == "it@contoso.example"
        assert settings.erase.verification_percent == 25

    def test_malformed_config_is_ignored_not_fatal(self, monkeypatch, tmp_path):
        """A broken config must never stop a drive being erased."""
        broken = tmp_path / "broken.json"
        broken.write_text("{ this is not json", encoding="utf-8")
        monkeypatch.setattr("zeroize.config.config_files", lambda: [broken])

        settings = load_settings()
        assert settings.safety.confirmation_phrase == "ERASE"

    def test_unknown_keys_are_dropped(self, monkeypatch, tmp_path):
        import json

        config = tmp_path / "config.json"
        config.write_text(json.dumps({"safety": {"nonsense": True}}), encoding="utf-8")
        monkeypatch.setattr("zeroize.config.config_files", lambda: [config])
        assert load_settings().safety.require_typed_confirmation

    @pytest.mark.parametrize(
        ("percent", "expected"),
        [(-10, 0.0), (0, 0.0), (50, 50.0), (100, 100.0), (500, 100.0)],
    )
    def test_verification_percent_is_clamped(self, monkeypatch, tmp_path, percent, expected):
        import json

        config = tmp_path / "config.json"
        config.write_text(json.dumps({"erase": {"verification_percent": percent}}), encoding="utf-8")
        monkeypatch.setattr("zeroize.config.config_files", lambda: [config])
        assert load_settings().erase.verification_percent == expected

    def test_certificate_output_volume_label_has_a_default(self):
        """Certificates must have somewhere to go that survives a live session."""
        assert Settings().certificate.output_volume_label == "ZEROIZE-OUT"

    def test_output_label_does_not_collide_with_the_iso_label(self):
        """The live ISO's own volume label is ZEROIZE.

        Were the output volume labelled the same, /dev/disk/by-label/ZEROIZE
        would be ambiguous and the read-only boot medium could shadow the
        writable output volume - certificates would then silently fall back to
        RAM and be lost at power off.
        """
        assert Settings().certificate.output_volume_label != "ZEROIZE"

    def test_output_label_fits_a_fat32_volume_label(self):
        """FAT32 labels are capped at 11 characters; longer ones get truncated."""
        assert len(Settings().certificate.output_volume_label) <= 11

    def test_blank_confirmation_phrase_falls_back(self, monkeypatch, tmp_path):
        """An empty phrase would make the typed confirmation a no-op."""
        import json

        config = tmp_path / "config.json"
        config.write_text(json.dumps({"safety": {"confirmation_phrase": "  "}}), encoding="utf-8")
        monkeypatch.setattr("zeroize.config.config_files", lambda: [config])
        assert load_settings().safety.confirmation_phrase == "ERASE"


class TestCertificateDestination:
    """Where certificates land, which on a live USB decides if they survive."""

    def test_output_volume_is_preferred_when_writable(self, tmp_path):
        from zeroize.paths import certificate_dir

        volume = tmp_path / "stick"
        volume.mkdir()

        destination = certificate_dir(output_volume=volume)
        assert destination == volume / "Zeroize Certificates"
        assert destination.is_dir()

    def test_falls_back_when_no_volume_is_attached(self, monkeypatch, tmp_path):
        """Unplugging the output stick must not stop a certificate being written."""
        import zeroize.paths as paths

        monkeypatch.setattr(paths, "invoking_user_home", lambda: tmp_path)

        destination = paths.certificate_dir(output_volume=None)
        assert destination == tmp_path / "Zeroize Certificates"


class TestOutputVolumeSelection:
    """Choosing the output volume from what lsblk reports.

    The collision case is the one that matters: the live ISO carries its own
    volume label, so a naive lookup can land on the read-only boot medium and
    send certificates to RAM without saying so.
    """

    @staticmethod
    def _select(tree, label, monkeypatch):
        import json

        import zeroize.discovery.volumes as volumes

        class _Result:
            ok = True
            stdout = json.dumps(tree)
            failure_summary = ""

        monkeypatch.setattr(volumes, "tool_available", lambda name: True)
        monkeypatch.setattr(volumes, "run", lambda *args, **kwargs: _Result())
        return volumes.find_writable_volume(label)

    def test_picks_a_writable_labelled_volume(self, monkeypatch, tmp_path):
        tree = {
            "blockdevices": [
                {
                    "path": "/dev/sdb1",
                    "label": "ZEROIZE-OUT",
                    "mountpoints": [str(tmp_path)],
                    "ro": False,
                }
            ]
        }
        assert self._select(tree, "ZEROIZE-OUT", monkeypatch) == tmp_path

    def test_never_picks_the_live_boot_medium(self, monkeypatch):
        """An exact label match is still refused when it is the medium we booted."""
        tree = {
            "blockdevices": [
                {
                    "path": "/dev/sdc1",
                    "label": "ZEROIZE-OUT",
                    "mountpoints": ["/run/live/medium"],
                    "ro": False,
                }
            ]
        }
        assert self._select(tree, "ZEROIZE-OUT", monkeypatch) is None

    def test_skips_a_read_only_match_and_takes_the_writable_one(self, monkeypatch, tmp_path):
        """Two volumes can share a label; only one of them is usable."""
        tree = {
            "blockdevices": [
                {
                    "path": "/dev/sdc",
                    "label": "ZEROIZE-OUT",
                    "mountpoints": ["/mnt/readonly"],
                    "ro": True,
                },
                {
                    "path": "/dev/sdb1",
                    "label": "ZEROIZE-OUT",
                    "mountpoints": [str(tmp_path)],
                    "ro": False,
                },
            ]
        }
        assert self._select(tree, "ZEROIZE-OUT", monkeypatch) == tmp_path

    def test_matches_the_label_case_insensitively(self, monkeypatch, tmp_path):
        """FAT uppercases labels; an operator may type it either way."""
        tree = {
            "blockdevices": [
                {
                    "path": "/dev/sdb1",
                    "label": "zeroize-out",
                    "mountpoints": [str(tmp_path)],
                    "ro": False,
                }
            ]
        }
        assert self._select(tree, "ZEROIZE-OUT", monkeypatch) == tmp_path

    def test_finds_a_volume_nested_under_its_disk(self, monkeypatch, tmp_path):
        """The real live-USB shape: boot medium and output are both on one stick."""
        tree = {
            "blockdevices": [
                {
                    "path": "/dev/sdb",
                    "label": None,
                    "children": [
                        {
                            "path": "/dev/sdb1",
                            "label": "ZEROIZE",
                            "mountpoints": ["/run/live/medium"],
                            "ro": True,
                        },
                        {
                            "path": "/dev/sdb2",
                            "label": "ZEROIZE-OUT",
                            "mountpoints": [str(tmp_path)],
                            "ro": False,
                        },
                    ],
                }
            ]
        }
        assert self._select(tree, "ZEROIZE-OUT", monkeypatch) == tmp_path

    def test_unmounted_match_is_not_selected(self, monkeypatch):
        tree = {
            "blockdevices": [
                {"path": "/dev/sdb1", "label": "ZEROIZE-OUT", "mountpoints": [], "ro": False}
            ]
        }
        assert self._select(tree, "ZEROIZE-OUT", monkeypatch) is None

    def test_blank_label_searches_for_nothing(self):
        from zeroize.discovery.volumes import find_writable_volume

        assert find_writable_volume("") is None


class TestAutoUnfreeze:
    """Detaching frozen drives at startup, before the list is drawn."""

    def test_enabled_by_default(self):
        """A bench tool should not start with its best method unavailable."""
        assert Settings().safety.auto_unfreeze_on_scan is True

    def test_never_touches_the_live_medium(self):
        """Detaching the medium we booted from would take the tool down."""
        from zeroize.erase.unfreeze import can_attempt
        from zeroize.models import AtaSecurity, Device, DeviceKind

        device = Device(
            path="/dev/sdc",
            name="sdc",
            kind=DeviceKind.USB,
            is_system=True,
            protection_reason="/dev/sdc1 is mounted at /run/live/medium",
            ata=AtaSecurity(supported=True, frozen=True),
        )
        assert "live/medium" in can_attempt(device)

    def test_never_touches_a_mounted_drive(self):
        from zeroize.erase.unfreeze import can_attempt
        from zeroize.models import AtaSecurity, Device, DeviceKind, Partition

        device = Device(
            path="/dev/sdb",
            name="sdb",
            kind=DeviceKind.ATA,
            ata=AtaSecurity(supported=True, frozen=True),
        )
        device.partitions = [
            Partition(name="sdb1", path="/dev/sdb1", size_bytes=1000, mountpoint="/mnt/data")
        ]
        assert "mounted" in can_attempt(device)

    def test_skips_a_drive_that_is_not_frozen(self):
        from zeroize.erase.unfreeze import can_attempt
        from zeroize.models import AtaSecurity, Device, DeviceKind

        device = Device(
            path="/dev/sdb",
            name="sdb",
            kind=DeviceKind.ATA,
            ata=AtaSecurity(supported=True, frozen=False),
        )
        assert can_attempt(device) == "not frozen" or "not frozen" in can_attempt(device)

    def test_eligible_frozen_drive_is_accepted(self):
        from zeroize.erase.unfreeze import can_attempt
        from zeroize.models import AtaSecurity, Device, DeviceKind

        device = Device(
            path="/dev/sdb",
            name="sdb",
            kind=DeviceKind.ATA,
            ata=AtaSecurity(supported=True, frozen=True),
        )
        assert can_attempt(device) == ""

    def test_clear_freezes_does_nothing_without_candidates(self, monkeypatch):
        """No frozen drives means no bus disturbance at all."""
        from zeroize.erase.unfreeze import clear_freezes
        from zeroize.models import AtaSecurity, Device, DeviceKind

        devices = [
            Device(path="/dev/sdb", name="sdb", kind=DeviceKind.ATA,
                   ata=AtaSecurity(supported=True, frozen=False)),
        ]
        assert clear_freezes(devices) == []

    def test_the_automatic_path_never_suspends(self):
        """Startup unfreeze must never reach S3, however tempting.

        S3 clears an ATA freeze where a bus reset will not, so it has a real
        use and is offered behind an explicit prompt. What must never happen is
        reaching it automatically: S3 also cuts power to the USB controller, and
        on resume the boot stick can return under a different device node while
        the live filesystem is still a loop mount over the old one. Every read
        then fails with EIO and the session dies by inches - the terminal will
        not start, modprobe cannot load a module, nvme cannot be executed. None
        of that looks like a suspend fault, which is what makes it dangerous.

        So clear_freezes(), which runs before the drive list is even shown, must
        use only the detach-and-rescan path.
        """
        import ast
        import inspect

        from zeroize.erase import unfreeze as module

        tree = ast.parse(inspect.getsource(module))
        automatic = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "clear_freezes"
        )
        called = {
            node.func.id
            for node in ast.walk(automatic)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "suspend_to_ram" not in called, (
            "clear_freezes() suspends the machine at startup; on a live USB "
            "that can take the whole session down"
        )

    def test_unfreeze_only_suspends_when_asked(self):
        """The default call path must not suspend either."""
        import ast
        import inspect

        from zeroize.erase import unfreeze as module

        signature = inspect.signature(module.unfreeze)
        assert signature.parameters["allow_suspend"].default is False

        tree = ast.parse(inspect.getsource(module))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "unfreeze"
        )
        # Every suspend_to_ram() call must sit inside a branch testing the flag.
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "suspend_to_ram":
                guarded = any(
                    isinstance(parent, ast.If)
                    and any(
                        getattr(sub, "id", "") == "allow_suspend"
                        for sub in ast.walk(parent.test)
                    )
                    for parent in ast.walk(function)
                    if isinstance(parent, ast.If) and node in list(ast.walk(parent))
                )
                assert guarded, "suspend_to_ram() is reachable without allow_suspend"


class TestChildStdin:
    """A child process must never inherit our stdin.

    nvme-cli asks "Type 'YES' to proceed:" before a destructive command, and
    hdparm has prompts of its own. A child that inherits a stdin nobody is
    driving blocks on that prompt until the timeout expires - six hours for an
    NVMe format - while the heartbeat reports the erase as running. The
    operator sees a wipe in progress that is really a process waiting for a
    keystroke that can never arrive.
    """

    def test_stdin_is_closed_for_a_command_with_no_input(self, monkeypatch):
        """The call must explicitly pass DEVNULL, not merely appear to work.

        Asserting on observed behaviour is not enough here. A test runner's own
        stdin is usually already closed or redirected, so a child inherits an
        stdin that returns EOF anyway and the bug hides completely - this test
        passed against the unfixed code for exactly that reason. On the live
        image the parent is a desktop session launched through pkexec, whose
        stdin is a real descriptor, and the prompt blocks. So the contract is
        checked at the call itself.
        """
        import subprocess

        from zeroize import process

        seen: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(process.subprocess, "run", fake_run)
        process.run(["nvme", "format", "/dev/nvme0n1"], timeout=5.0)

        assert seen.get("stdin") is subprocess.DEVNULL, (
            "run() let the child inherit our stdin; an nvme-cli confirmation "
            "prompt will block until the timeout - six hours for a format"
        )

    def test_stdin_is_not_clobbered_when_input_is_supplied(self, monkeypatch):
        """Passing DEVNULL alongside input= would raise; it must stay unset."""
        import subprocess

        from zeroize import process

        seen: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(process.subprocess, "run", fake_run)
        process.run(["nvme", "format", "/dev/nvme0n1"], timeout=5.0, input_text="YES")

        assert seen.get("stdin") is None
        assert seen.get("input") == "YES"

    def test_supplied_input_still_reaches_the_child(self):
        """Closing stdin must not break the commands that do send input."""
        import sys

        from zeroize.process import run

        result = run(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            timeout=20.0,
            input_text="YES\n",
        )
        assert result.stdout.strip() == "YES"

    def test_heartbeat_runner_forwards_input(self):
        """The confirmation for the no-force nvme retry travels this path."""
        import sys

        from zeroize.process import run_with_heartbeat

        result = run_with_heartbeat(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            heartbeat=lambda _elapsed: True,
            interval=0.2,
            timeout=20.0,
            input_text="YES\n",
        )
        assert result.stdout.strip() == "YES"


class TestOperatorName:
    """A required operator name must actually be entered.

    The field used to be pre-filled from the login name, which on the live
    image is the appliance account "zeroize". That satisfied the not-empty
    check without anyone typing anything, so a certificate could attest that
    "zeroize" performed the erase - which attests to nothing. A default that
    passes validation is not a required field.
    """

    def test_the_appliance_account_is_rejected(self):
        from zeroize.config import validate_operator_name

        for name in ("zeroize", "ZEROIZE", "  root  ", "live", "Admin"):
            assert validate_operator_name(name), f"{name!r} was accepted"

    def test_an_empty_or_one_character_name_is_rejected(self):
        from zeroize.config import validate_operator_name

        for name in ("", "   ", "C", "	"):
            assert validate_operator_name(name), f"{name!r} was accepted"

    def test_a_real_name_is_accepted(self):
        from zeroize.config import validate_operator_name

        for name in ("Cody White", "c.white", "AB", " Jo "):
            assert validate_operator_name(name) == "", f"{name!r} was rejected"

    def test_the_certificate_records_whatever_was_entered(self):
        """The operator string reaches the summary unaltered."""
        from datetime import datetime

        from zeroize.models import RunSummary

        summary = RunSummary(
            results=[],
            started_at=datetime.now().astimezone(),
            finished_at=datetime.now().astimezone(),
            operator="Cody White",
            machine="bench-1",
        )
        assert summary.operator == "Cody White"


class TestProtectionIsolation:
    """The protection tests must judge the fixture, not the host.

    _mount_table() is keyed on a device's major:minor, obtained by stat()ing
    the path - so a fixture that calls its fake disk /dev/sda resolves to the
    real /dev/sda when the machine has one. On a developer's laptop there
    usually is none and the test passes; on a CI runner rooted at /dev/sda1 the
    interlock fires correctly against the host's mounts and the test fails,
    having proved nothing about the code either way.

    So the tests supply their own tables. This asserts they really do.
    """

    def test_a_supplied_table_stops_the_host_being_read(self, monkeypatch):
        from zeroize.discovery import block_devices

        def fail_if_called():  # pragma: no cover - the assertion is that it is not
            raise AssertionError("_mount_table() was read despite a table being supplied")

        monkeypatch.setattr(block_devices, "_mount_table", fail_if_called)
        monkeypatch.setattr(block_devices, "_active_swap_sources", fail_if_called)

        device = _build_device(_lsblk_node())
        _protect([device])

        assert not device.is_system

    def test_the_host_is_read_when_nothing_is_supplied(self, monkeypatch):
        """Production must still consult the running system. It is the interlock."""
        from zeroize.discovery import block_devices

        seen = {"mounts": False, "swap": False}
        monkeypatch.setattr(
            block_devices, "_mount_table", lambda: seen.__setitem__("mounts", True) or {}
        )
        monkeypatch.setattr(
            block_devices, "_active_swap_sources", lambda: seen.__setitem__("swap", True) or set()
        )

        _apply_protection([_build_device(_lsblk_node())])

        assert seen["mounts"], "production did not read the real mount table"
        assert seen["swap"], "production did not read the real swap list"
