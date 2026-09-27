"""Site configuration, including the organisation block printed on certificates.

Zeroize ships unbranded to any particular organisation. The certificate has to
say *who* performed the erase, though, or it is worthless as a chain-of-custody
record - so that identity is configuration rather than something baked into the
build. The same package can then be deployed at several sites, or used on
customer-owned drives where the certificate must name the customer.

Configuration is layered, each file overriding the one before it:

1. The defaults in this module.
2. ``/etc/zeroize/config.json`` - site defaults, deployed with the package or
   written onto the live USB. This is where an organisation block normally
   lives.
3. ``$XDG_CONFIG_HOME/zeroize/config.json`` - per-operator overrides.
4. Anything passed on the command line.

Merging is recursive for nested objects, so a user file that sets only
``organisation.operator`` keeps every other organisation field from the site
file. An unreadable or malformed file is logged and skipped rather than being
fatal: a missing config must never stop a drive being erased.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .paths import config_files

_log = get_logger("Config")


#: Account names that are never a person, and so are never a valid operator.
#: The live image logs in as "zeroize"; a certificate naming the appliance
#: account as the operator attests to nothing.
RESERVED_OPERATOR_NAMES = frozenset({"zeroize", "root", "user", "live", "debian", "admin"})


def validate_operator_name(name: str) -> str:
    """Return why *name* is unacceptable as an operator, or an empty string.

    Lives here rather than in the dialog because it is policy, not
    presentation - and because a rule that can only be exercised through GTK
    cannot be tested on a machine without it, which is every machine this is
    developed on.
    """
    operator = (name or "").strip()
    if not operator:
        return "Enter the operator name"
    if len(operator) < 2:
        return "Enter the operator name in full"
    if operator.casefold() in RESERVED_OPERATOR_NAMES:
        return "Enter the name of the person performing this erase"
    return ""


@dataclass(slots=True)
class Organisation:
    """Who performed the erase - printed in the certificate's header block.

    Every field is optional. Blank fields are omitted from the certificate
    rather than printed empty, so a minimal configuration that sets only
    ``name`` still produces a clean document.
    """

    #: The organisation performing the erasure, e.g. "Contoso IT Services".
    name: str = ""
    #: Department or team within that organisation.
    department: str = ""
    #: Free-form address, one entry per line on the certificate.
    address_lines: list[str] = field(default_factory=list)
    #: Contact shown for questions about the certificate.
    contact_email: str = ""
    contact_phone: str = ""
    #: The technician who ran the job. Usually left blank here and supplied per
    #: run, either from the login name or typed into the confirmation dialog.
    operator: str = ""
    #: Optional customer the drives belong to, for third-party disposal work.
    customer: str = ""
    #: Optional reference - work order, ticket, or asset-disposal batch number.
    reference: str = ""
    #: Path to a PNG or SVG logo placed beside the Zeroize mark on the
    #: certificate. Left blank, only the Zeroize mark appears.
    logo_path: str = ""


@dataclass(slots=True)
class CertificateSettings:
    """How certificates are named, where they land, and what they include."""

    #: Directory for generated PDFs. Blank means "decide automatically" - see
    #: :func:`zeroize.paths.certificate_dir`. Setting it wins over everything.
    output_directory: str = ""
    #: Filesystem label of a removable volume to write certificates to when one
    #: is plugged in. This is how certificates survive a live session: the live
    #: filesystem is a tmpfs and the boot medium is read-only, so without a
    #: writable volume the one artefact the tool exists to produce is lost at
    #: power off. Label a second partition (or a second stick) with this and it
    #: is found automatically. Blank disables the search.
    #:
    #: Deliberately NOT "ZEROIZE": that is the live ISO's own volume label, and
    #: two mounted filesystems sharing a label make
    #: /dev/disk/by-label/<name> ambiguous - udev points it at whichever device
    #: it saw last. The boot medium would then shadow the output volume and
    #: certificates would silently fall back to RAM. Eleven characters, which
    #: is exactly the FAT32 label limit.
    output_volume_label: str = "ZEROIZE-OUT"
    #: Include the appendix listing every command issued against each drive.
    include_command_log: bool = True
    #: Include the partition layout each drive had before it was erased.
    include_prior_layout: bool = True
    #: Also write a JSON sidecar alongside the PDF, for ingestion by an asset
    #: system. The PDF is the record; the JSON is for machines.
    write_json_sidecar: bool = True
    #: Copy the run's log file out beside the certificate. On a live session
    #: the log is in RAM and is lost at power off, which would take the only
    #: record of what was actually issued to each drive with it.
    copy_log_beside_certificate: bool = True
    #: Maximum serial numbers to spell out in the filename before it collapses
    #: to a count. The PDF always lists every serial regardless.
    max_serials_in_filename: int = 4


@dataclass(slots=True)
class EraseSettings:
    """Defaults for the erase itself."""

    #: Method key preselected in the UI. Blank means "let the app recommend one
    #: per drive", which follows NIST SP 800-88r1 preference order.
    default_method: str = ""
    #: Percentage of the device sampled during verification, 0-100. 100 reads
    #: the whole device back, which doubles the time for overwrite methods.
    verification_percent: float = 10.0
    #: Drives erased concurrently. Hardware commands are nearly free to run in
    #: parallel; overwrite passes contend for bus bandwidth.
    max_concurrent_jobs: int = 4
    #: Block size for overwrite passes, in bytes.
    overwrite_block_size: int = 4 * 1024 * 1024


@dataclass(slots=True)
class SafetySettings:
    """The interlocks. Loosening any of these is a deliberate site decision."""

    #: Require the operator to type a confirmation phrase before erasing.
    require_typed_confirmation: bool = True
    #: The phrase they must type.
    confirmation_phrase: str = "ERASE"
    #: Allow selecting a device that has a mounted filesystem, unmounting it
    #: first. Devices carrying the running system are never selectable, and
    #: this setting cannot change that.
    allow_mounted_devices: bool = False
    #: Require a non-empty operator name before a run may start.
    require_operator_name: bool = True
    #: On startup, detach and re-attach any drive the firmware has frozen, so
    #: Secure Erase is available without the operator doing anything. Set false
    #: on a bench where disturbing the SCSI bus at start is unwelcome.
    auto_unfreeze_on_scan: bool = True


@dataclass(slots=True)
class Settings:
    """The whole configuration tree."""

    organisation: Organisation = field(default_factory=Organisation)
    certificate: CertificateSettings = field(default_factory=CertificateSettings)
    erase: EraseSettings = field(default_factory=EraseSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SECTION_TYPES: dict[str, type] = {
    "organisation": Organisation,
    "certificate": CertificateSettings,
    "erase": EraseSettings,
    "safety": SafetySettings,
}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* onto *base*, returning a new dictionary."""
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_file(path: Path) -> dict[str, Any]:
    """Read one config file, returning an empty dict if it is absent or bad."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        _log.warning("Could not read %s: %s", path, error)
        return {}

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        _log.error("Ignoring malformed config %s: %s", path, error)
        return {}

    if not isinstance(payload, dict):
        _log.error("Ignoring config %s: top level must be an object", path)
        return {}

    _log.info("Loaded configuration from %s", path)
    return payload


def _build_section(section_type: type, payload: Any) -> Any:
    """Instantiate one settings dataclass, ignoring keys it does not define."""
    if not isinstance(payload, dict):
        return section_type()
    known = {item.name for item in section_type.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(payload) - known
    if unknown:
        _log.warning("Ignoring unknown config keys in %s: %s", section_type.__name__, ", ".join(sorted(unknown)))
    return section_type(**{key: value for key, value in payload.items() if key in known})


def load_settings(*, overrides: dict[str, Any] | None = None) -> Settings:
    """Load and merge every configuration layer into a :class:`Settings`."""
    merged: dict[str, Any] = {}
    for path in config_files():
        merged = _merge(merged, _load_file(path))
    if overrides:
        merged = _merge(merged, overrides)

    settings = Settings(
        **{
            name: _build_section(section_type, merged.get(name, {}))
            for name, section_type in _SECTION_TYPES.items()
        }
    )

    # Clamp the values where a bad number would be actively harmful rather
    # than merely odd.
    settings.erase.verification_percent = max(0.0, min(100.0, settings.erase.verification_percent))
    settings.erase.max_concurrent_jobs = max(1, min(32, settings.erase.max_concurrent_jobs))
    settings.erase.overwrite_block_size = max(64 * 1024, min(64 * 1024 * 1024, settings.erase.overwrite_block_size))
    settings.certificate.max_serials_in_filename = max(1, settings.certificate.max_serials_in_filename)
    if not settings.safety.confirmation_phrase.strip():
        settings.safety.confirmation_phrase = "ERASE"

    return settings


def write_example_config(target: Path) -> Path:
    """Write a fully populated example config, used by the packaging step.

    The file shipped to ``/etc/zeroize/config.json`` is this, with the
    organisation block left blank for the site to fill in.
    """
    example = Settings().to_dict()
    example["organisation"].update(
        {
            "name": "",
            "department": "",
            "address_lines": [],
            "contact_email": "",
            "contact_phone": "",
            "operator": "",
            "customer": "",
            "reference": "",
            "logo_path": "",
        }
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(example, indent=2) + "\n", encoding="utf-8")
    return target
