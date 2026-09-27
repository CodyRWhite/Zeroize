"""NVMe capability discovery via ``nvme-cli``.

The controller itself decides which erase commands are legal, and the answer
differs widely between drives: consumer SSDs routinely support Format NVM but
not Sanitize, some enterprise drives support Sanitize but refuse a crypto
erase, and a handful support neither. Offering an operator a method the drive
will reject wastes a bench slot and produces a confusing failure, so every
method is gated on the bits read here.

The three registers that matter, all from ``nvme id-ctrl``:

``OACS`` (Optional Admin Command Support)
    Bit 1 - the Format NVM command exists at all. Without it neither
    ``--ses=1`` nor ``--ses=2`` is possible.

``FNA`` (Format NVM Attributes)
    Bit 0 - a format applies to every namespace, not just the selected one.
    Bit 1 - a secure erase applies to every namespace.
    Bit 2 - cryptographic erase (``--ses=2``) is supported.

``SANICAP`` (Sanitize Capabilities)
    Bit 0 - Crypto Erase Sanitize. Bit 1 - Block Erase Sanitize.
    Bit 2 - Overwrite Sanitize. A zero register means Sanitize is unsupported,
    which is the common case on consumer hardware.

Sanitize progress comes from the Sanitize Status log page, where ``SSTAT``
carries the state in its low three bits and ``SPROG`` is a fraction of 65536.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..logging_setup import get_logger
from ..models import Device, DeviceKind, NvmeCapabilities
from ..process import run, tool_available

_log = get_logger("Nvme")

# OACS bit 1 - Format NVM command supported.
_OACS_FORMAT = 1 << 1

# FNA bits.
_FNA_FORMAT_ALL_NAMESPACES = 1 << 0
_FNA_SECURE_ERASE_ALL_NAMESPACES = 1 << 1
_FNA_CRYPTO_ERASE_SUPPORTED = 1 << 2

# SANICAP bits.
_SANICAP_CRYPTO = 1 << 0
_SANICAP_BLOCK = 1 << 1
_SANICAP_OVERWRITE = 1 << 2

# SSTAT low three bits - the sanitize operation's state. Distinguishing 1 from
# 3 is the whole point: both mean "not running any more", and only one of them
# means the drive was erased.
_SSTAT_STATUS_MASK = 0x7
SSTAT_NEVER_SANITIZED = 0x0
SSTAT_COMPLETED = 0x1
SSTAT_IN_PROGRESS = 0x2
SSTAT_FAILED = 0x3
SSTAT_COMPLETED_NO_DEALLOCATE = 0x4

#: SSTAT bit 8 - Global Data Erased. Set when no user data written before the
#: most recent sanitize can be read. It is the controller's own attestation and
#: is worth recording on the certificate when present.
_SSTAT_GLOBAL_DATA_ERASED = 1 << 8

_SSTAT_DESCRIPTIONS = {
    SSTAT_NEVER_SANITIZED: "never sanitized",
    SSTAT_COMPLETED: "completed successfully",
    SSTAT_IN_PROGRESS: "in progress",
    SSTAT_FAILED: "failed",
    SSTAT_COMPLETED_NO_DEALLOCATE: "completed successfully (without deallocate)",
}

_NAMESPACE_PATTERN = re.compile(r"^(?P<controller>/dev/nvme\d+)(n\d+(p\d+)?)?$")


@dataclass(slots=True)
class SanitizeLog:
    """A decoded read of the Sanitize Status log page."""

    status: int
    percent: float
    global_data_erased: bool = False
    raw_sstat: int = 0
    #: Dword 10 of the most recent Sanitize command the controller accepted.
    #: Its low 3 bits are the sanitize action, so comparing it against the
    #: action just issued distinguishes "this controller never received our
    #: command" from "it received it and has not updated SSTAT yet". Without
    #: that distinction a slow controller is indistinguishable from a deaf one.
    raw_scdw10: int = 0

    def records_action(self, action: int) -> bool:
        """True when the log names *action* as the last sanitize requested."""
        return bool(self.raw_scdw10 & 0x07) and (self.raw_scdw10 & 0x07) == action

    @property
    def in_progress(self) -> bool:
        return self.status == SSTAT_IN_PROGRESS

    @property
    def succeeded(self) -> bool:
        """True only for an explicit success.

        ``not in_progress`` is emphatically not the same thing - a failed
        sanitize is also not in progress, and treating the two alike would
        certify an unerased drive as erased.
        """
        return self.status in (SSTAT_COMPLETED, SSTAT_COMPLETED_NO_DEALLOCATE)

    @property
    def description(self) -> str:
        return _SSTAT_DESCRIPTIONS.get(self.status, f"unknown status {self.status}")


def controller_path_for(device_path: str) -> str:
    """Map a namespace path to its controller: ``/dev/nvme0n1`` -> ``/dev/nvme0``.

    Admin commands such as ``id-ctrl`` and ``sanitize`` are issued against the
    controller. ``nvme-cli`` accepts a namespace path for most of them, but
    being explicit keeps the logged command unambiguous when a drive exposes
    several namespaces.
    """
    match = _NAMESPACE_PATTERN.match(device_path)
    return match.group("controller") if match else device_path


def namespace_id_for(device_path: str) -> int:
    """Namespace number from a path, defaulting to 1 for a bare controller."""
    match = re.search(r"n(\d+)$", device_path)
    return int(match.group(1)) if match else 1


def _nvme_json(subcommand: list[str]) -> dict | list | None:
    """Run an ``nvme`` subcommand asking for JSON, tolerating CLI differences.

    nvme-cli changed its output-format flag spelling between the versions
    shipped by the target distributions, so both spellings are attempted before
    giving up.
    """
    for flag in (["-o", "json"], ["--output-format=json"]):
        result = run(["nvme", *subcommand, *flag], timeout=60.0, log_output=False)
        if not result.ok or not result.stdout.strip():
            continue
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            # Some builds prepend a warning line before the JSON body.
            brace = result.stdout.find("{")
            bracket = result.stdout.find("[")
            start = min(index for index in (brace, bracket) if index >= 0) if (brace >= 0 or bracket >= 0) else -1
            if start >= 0:
                try:
                    return json.loads(result.stdout[start:])
                except json.JSONDecodeError:
                    pass
            _log.warning("Unparseable JSON from: nvme %s", " ".join(subcommand))
    return None


def _coerce_register(value: object) -> int:
    """id-ctrl values arrive as ints, decimal strings or ``0x``-prefixed hex."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return 0
    return 0


def _first_present(payload: dict, *names: str) -> object | None:
    """First key of *names* actually present, or ``None`` if none of them are.

    Deliberately distinct from ``dict.get`` with a default: the caller needs to
    tell "the controller reported 0" from "nvme-cli called this something else",
    and a default collapses those into the same answer.
    """
    for name in names:
        if name in payload:
            return payload[name]
    return None


#: SSTAT/SPROG/SCDW10 as nvme-cli's human-readable output prints them, e.g.
#: "Sanitize Status  (SSTAT) :  0x1".
_SANITIZE_TEXT_PATTERN = re.compile(
    r"\((?P<field>SPROG|SSTAT|SCDW10)\)\s*:\s*(?P<value>0x[0-9a-fA-F]+|\d+)"
)


def _parse_sanitize_text(output: str) -> tuple[object, object, object] | None:
    """Pull SSTAT, SPROG and SCDW10 out of the text log page.

    The fallback for when the JSON output does not carry fields under any name
    this code knows. Returns ``None`` when the status is absent, because a
    sanitize status that cannot be read must never be reported as zero.
    """
    found: dict[str, str] = {
        match.group("field"): match.group("value")
        for match in _SANITIZE_TEXT_PATTERN.finditer(output)
    }
    if "SSTAT" not in found:
        return None
    return found["SSTAT"], found.get("SPROG", 0), found.get("SCDW10", 0)


def read_capabilities(device_path: str) -> NvmeCapabilities | None:
    """Read the capability registers for one NVMe device.

    Returns ``None`` when ``nvme-cli`` is missing or the controller does not
    answer, which the caller treats as "offer no NVMe-specific methods" rather
    than as a fatal error.
    """
    if not tool_available("nvme"):
        _log.error("nvme-cli is not installed - NVMe methods will be unavailable")
        return None

    controller = controller_path_for(device_path)
    identity = _nvme_json(["id-ctrl", controller])
    if not isinstance(identity, dict):
        _log.warning("Could not read id-ctrl for %s", controller)
        return None

    oacs = _coerce_register(identity.get("oacs"))
    fna = _coerce_register(identity.get("fna"))
    sanicap = _coerce_register(identity.get("sanicap"))

    format_supported = bool(oacs & _OACS_FORMAT)
    capabilities = NvmeCapabilities(
        controller_path=controller,
        namespace_id=namespace_id_for(device_path),
        format_supported=format_supported,
        # A crypto erase is a mode of Format NVM, so it needs both bits.
        crypto_erase_supported=format_supported and bool(fna & _FNA_CRYPTO_ERASE_SUPPORTED),
        format_applies_to_all_namespaces=bool(fna & _FNA_FORMAT_ALL_NAMESPACES),
        secure_erase_applies_to_all_namespaces=bool(fna & _FNA_SECURE_ERASE_ALL_NAMESPACES),
        sanitize_crypto_supported=bool(sanicap & _SANICAP_CRYPTO),
        sanitize_block_supported=bool(sanicap & _SANICAP_BLOCK),
        sanitize_overwrite_supported=bool(sanicap & _SANICAP_OVERWRITE),
        namespace_count=_coerce_register(identity.get("nn")) or 1,
        model=str(identity.get("mn", "")).strip(),
        firmware=str(identity.get("fr", "")).strip(),
        raw_oacs=oacs,
        raw_fna=fna,
        raw_sanicap=sanicap,
    )

    if capabilities.any_sanitize_supported:
        log = read_sanitize_status(controller)
        if log is not None:
            capabilities.sanitize_in_progress = log.in_progress
            capabilities.sanitize_progress_percent = log.percent

    # Enumerate namespaces so the erase path knows whether a single format can
    # cover the whole drive. Reported namespace count (NN) is the maximum the
    # controller supports, not how many are active, so it cannot be used here.
    namespaces = list_namespaces(controller)
    if namespaces:
        capabilities.namespace_count = len(namespaces)
        capabilities.active_namespaces = namespaces

    _log.info(
        "%s capabilities: format=%s crypto=%s sanitize(crypto=%s block=%s overwrite=%s) "
        "[oacs=0x%04x fna=0x%02x sanicap=0x%08x]",
        controller,
        capabilities.format_supported,
        capabilities.crypto_erase_supported,
        capabilities.sanitize_crypto_supported,
        capabilities.sanitize_block_supported,
        capabilities.sanitize_overwrite_supported,
        oacs,
        fna,
        sanicap,
    )
    return capabilities


def read_sanitize_status(controller_path: str) -> SanitizeLog | None:
    """Read and decode the Sanitize Status log page.

    ``None`` means the log page could not be read, which on a drive without
    Sanitize support is expected rather than exceptional - and during an active
    sanitize is also normal, because some controllers refuse most commands
    while they work.
    """
    status_raw = None
    progress_raw = None
    command_raw = None

    # --rae retains the asynchronous event. Reading a log page without it tells
    # the controller to clear the event associated with that page, which is the
    # wrong thing to do when polling a sanitize to completion - the reads that
    # confirmed this drive's progress by hand all used it.
    payload = _nvme_json(["sanitize-log", controller_path, "--rae"])
    if payload is None:
        payload = _nvme_json(["sanitize-log", controller_path])
    if isinstance(payload, dict):
        # nvme-cli has used several spellings for these fields over the years.
        status_raw = _first_present(payload, "sstat", "status", "sanitize_status")
        progress_raw = _first_present(payload, "sprog", "progress", "sanitize_progress")
        # "cdw10_info" is what nvme-cli actually emits - confirmed by reading
        # the key strings out of bookworm's own binary (2.4+really2.3-3). The
        # other spellings were guesses, none of them matched, and SCDW10
        # therefore read 0x00000000 on every poll while the drive's own log
        # page showed 0x2. A guessed key name is indistinguishable from a
        # controller that reported nothing.
        command_raw = _first_present(
            payload, "cdw10_info", "scdw10", "sanitize_cdw10", "cdw10", "SCDW10"
        )

    # A MISSING field is not a field reading zero, and conflating the two is
    # how this went wrong: every poll reported "SSTAT 0x0000 - never sanitized"
    # for five minutes on a drive whose sanitize had in fact completed, because
    # the JSON key was not one of the spellings above and the absent value
    # defaulted silently to 0. A drive reported as unerased when it is erased
    # is a certificate-grade error, so when the status cannot be found the
    # human-readable output is parsed instead of guessing.
    if status_raw is None:
        _log.info(
            "%s sanitize-log JSON carried no recognised status field; "
            "falling back to the text output",
            controller_path,
        )
        text = run(
            ["nvme", "sanitize-log", controller_path, "-H", "--rae"],
            timeout=60.0,
            log_output=False,
        )
        if not text.ok:
            text = run(
                ["nvme", "sanitize-log", controller_path, "-H"],
                timeout=60.0,
                log_output=False,
            )
        if not text.ok:
            text = run(["nvme", "sanitize-log", controller_path], timeout=60.0, log_output=False)
        if not text.ok:
            return None
        parsed = _parse_sanitize_text(text.stdout)
        if parsed is None:
            return None
        status_raw, progress_raw, command_raw = parsed

    raw_sstat = _coerce_register(status_raw)
    progress = _coerce_register(progress_raw)
    status = raw_sstat & _SSTAT_STATUS_MASK

    # nvme-cli reports the Global Data Erased bit directly as well as inside
    # SSTAT. Prefer its answer when present; fall back to decoding the bit.
    erased_raw = _first_present(payload, "global_erased") if isinstance(payload, dict) else None
    global_erased = (
        bool(_coerce_register(erased_raw))
        if erased_raw is not None
        else bool(raw_sstat & _SSTAT_GLOBAL_DATA_ERASED)
    )

    if status == SSTAT_IN_PROGRESS:
        percent = round(progress / 65535 * 100, 1)
    elif status in (SSTAT_COMPLETED, SSTAT_COMPLETED_NO_DEALLOCATE):
        percent = 100.0
    else:
        percent = 0.0

    return SanitizeLog(
        status=status,
        percent=percent,
        global_data_erased=global_erased,
        raw_scdw10=_coerce_register(command_raw),
        raw_sstat=raw_sstat,
    )


def list_namespaces(controller_path: str) -> list[int]:
    """Return every active namespace id on *controller_path*.

    This matters more than it looks. On a drive with several namespaces where
    FNA bit 0 is clear, a Format NVM applies only to the namespace it was
    addressed to - so formatting ``/dev/nvme0n1`` on a two-namespace drive
    erases half of it and leaves the rest intact, while every indication to the
    operator says the drive was wiped. The erase path uses this list to cover
    every namespace explicitly.

    An empty list means the enumeration failed; the caller falls back to the
    single namespace it was given rather than assuming there is only one.
    """
    payload = _nvme_json(["list-ns", controller_path])
    identifiers: list[int] = []

    if isinstance(payload, dict):
        entries = payload.get("nsid_list", payload.get("namespaces", []))
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []

    for entry in entries or []:
        if isinstance(entry, dict):
            value = entry.get("nsid", entry.get("NSID"))
        else:
            value = entry
        number = _coerce_register(value)
        if number > 0:
            identifiers.append(number)

    if not identifiers:
        # Fall back to parsing the plain-text form, which older nvme-cli emits
        # as lines like "[   0]:0x1".
        result = run(["nvme", "list-ns", controller_path], timeout=30.0, log_output=False)
        if result.ok:
            for match in re.finditer(r":\s*(0x[0-9a-fA-F]+|\d+)", result.stdout):
                number = _coerce_register(match.group(1))
                if number > 0:
                    identifiers.append(number)

    return sorted(set(identifiers))


def _index_nvme_list() -> dict[str, dict]:
    """Index ``nvme list`` output by device path for serial/model enrichment.

    ``lsblk`` already reports a serial for most NVMe drives, but some
    controllers only surface it through the NVMe identify path, and the serial
    is the primary identifier on the certificate - it is worth a second source.
    """
    payload = _nvme_json(["list"])
    if not isinstance(payload, dict):
        return {}
    indexed: dict[str, dict] = {}
    for entry in payload.get("Devices", []) or []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("DevicePath") or entry.get("device_path") or "").strip()
        if path:
            indexed[path] = entry
    return indexed


def enrich_nvme_devices(devices: list[Device]) -> None:
    """Attach :class:`NvmeCapabilities` to every NVMe device in *devices*.

    Mutates the list in place. Devices that are not NVMe are left untouched,
    and a device whose controller refuses to identify simply keeps ``nvme``
    set to ``None`` so no NVMe method is offered for it.
    """
    nvme_devices = [device for device in devices if device.kind is DeviceKind.NVME]
    if not nvme_devices:
        return

    listing = _index_nvme_list()
    for device in nvme_devices:
        device.nvme = read_capabilities(device.path)

        entry = listing.get(device.path, {})
        if not device.serial:
            device.serial = str(entry.get("SerialNumber", "")).strip()
        if not device.model:
            device.model = str(entry.get("ModelNumber", "")).strip()
        if not device.firmware:
            device.firmware = str(entry.get("Firmware", "")).strip()

        if device.nvme:
            device.model = device.model or device.nvme.model
            device.firmware = device.firmware or device.nvme.firmware
            if device.nvme.sanitize_in_progress:
                device.protection_reason = (
                    f"a sanitize operation is already running "
                    f"({device.nvme.sanitize_progress_percent:.0f}% complete)"
                )
