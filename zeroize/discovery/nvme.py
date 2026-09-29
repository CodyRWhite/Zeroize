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


def _unwrap_device(payload: object, *expected: str) -> dict | None:
    """Return the object that actually carries *expected*, descending one level.

    nvme-cli 2.x keys each controller's log by its device name::

        {"nvme0": {"sprog": ..., "sstat": {...}, "cdw10_info": 2}}

    Older builds put those fields at the top level. Searching only the top
    level finds nothing on the newer output, which sends the caller to the
    text fallback - and the text output does not print everything the JSON
    carries, so the result is not an error but a quietly poorer answer.

    Only one level is descended, and only into a dict that holds one of the
    expected names, so an unrelated nested object cannot be mistaken for the
    log body.
    """
    if not isinstance(payload, dict):
        return None
    if any(name in payload for name in expected):
        return payload
    for value in payload.values():
        if isinstance(value, dict) and any(name in value for name in expected):
            return value
    return None


#: The leading code in nvme-cli's decoded status, e.g.
#: "(1) Most Recent Sanitize Command Completed Successfully."
_SANITIZE_STATUS_CODE = re.compile(r"^\s*\((?P<code>\d+)\)")


def _coerce_status(value: object) -> int | None:
    """SSTAT's three status bits, from any form nvme-cli reports them in.

    Returns ``None`` rather than 0 when the value cannot be read: 0 is a
    meaningful status ("never sanitized"), so a failure to parse must not be
    able to masquerade as one.
    """
    if isinstance(value, str):
        match = _SANITIZE_STATUS_CODE.match(value)
        if match:
            return int(match.group("code"))
        register = _coerce_register(value)
        return register & _SSTAT_STATUS_MASK if value.strip() else None
    if isinstance(value, bool):
        return int(value) & _SSTAT_STATUS_MASK
    if isinstance(value, int):
        return value & _SSTAT_STATUS_MASK
    return None


#: "Global Data Erased set: ..." / "Global Data Erased cleared: ..." in the
#: human-readable log page. Needed because the SSTAT hex printed beside it does
#: NOT include bit 8, so the text output is the only place the flag appears
#: when the JSON cannot be read.
_GLOBAL_ERASED_TEXT = re.compile(r"Global Data Erased\s+(?P<state>set|cleared)", re.IGNORECASE)


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


#: SMART critical warning bits that matter to an erase.
_CRITICAL_RELIABILITY_DEGRADED = 0x04
_CRITICAL_MEDIA_READ_ONLY = 0x08


def read_health(controller_path: str) -> tuple[bool, bool, int]:
    """Return ``(media_read_only, reliability_degraded, percentage_used)``.

    An NVMe controller that has run out of spare blocks puts the media into
    read-only mode and sets bit 3 of the SMART critical warning. That state is
    permanent: the drive will answer a format or a sanitize with "Access
    Denied", and a software overwrite fails just as surely, because the media
    genuinely will not accept writes any more.

    Worth reading during discovery rather than after a failed erase. The
    operator otherwise selects the drive, waits, and is told the erase was
    refused - which reads like a tool problem rather than a dead drive.

    Missing or unreadable health is reported as healthy. This gates a warning,
    not the erase itself, and refusing to offer a drive because its SMART log
    could not be read would be worse than the warning being absent.
    """
    payload = _nvme_json(["smart-log", controller_path])
    if not isinstance(payload, dict):
        return False, False, 0

    warning = _coerce_register(
        _first_present(payload, "critical_warning", "critical_comp_time") or 0
    )
    # Some builds nest it as an object of decoded flags.
    raw_warning = payload.get("critical_warning")
    if isinstance(raw_warning, dict):
        warning = _coerce_register(raw_warning.get("value", 0))

    used = _coerce_register(_first_present(payload, "percent_used", "percentage_used") or 0)

    return (
        bool(warning & _CRITICAL_MEDIA_READ_ONLY),
        bool(warning & _CRITICAL_RELIABILITY_DEGRADED),
        used,
    )


#: Identify Namespace DLFEAT bits 2:0 - the read behaviour of a deallocated
#: logical block. 0 = not reported, 1 = reads as 0x00, 2 = reads as 0xFF.
_DLFEAT_READ_BEHAVIOUR_MASK = 0x07


def read_namespace_dlfeat(namespace_path: str) -> int:
    """DLFEAT for one namespace, or 0 when it cannot be read.

    0 is both "not reported" and "could not read", and here the two mean the
    same thing to the caller: without a positive guarantee a discard must not
    be offered as an erase. Erring towards 0 withholds a method; erring the
    other way would certify one that proves nothing.
    """
    payload = _nvme_json(["id-ns", namespace_path])
    body = _unwrap_device(payload, "dlfeat", "nsfeat", "nsze")
    if not isinstance(body, dict):
        return 0
    return _coerce_register(_first_present(body, "dlfeat") or 0)


def probe_sanitize_reachable(controller_path: str) -> bool | None:
    """Will this controller accept a Sanitize command at all?

    Answers a different question from SANICAP. SANICAP says what the controller
    implements; this says whether it will run it now. Samsung drives on some
    OEM firmware advertise Sanitize and refuse every action with Access Denied
    until the machine has been suspended to RAM and resumed.

    Sanitize Exit Failure Mode (SANACT=1) is the probe because it ERASES
    NOTHING: it asks the controller to leave a failed-sanitize state, and on a
    drive with no failed sanitize to leave it has nothing to do.

    Classified on the message, never on the exit status. A drive with nothing
    to exit legitimately answers Invalid Field, which is a refusal of the
    ARGUMENT and means the command itself is reachable. Only Access Denied
    means the firmware is refusing Sanitize. Returning ``None`` when the probe
    could not run keeps "unknown" distinct from "blocked".
    """
    if not tool_available("nvme"):
        return None

    result = run(
        ["nvme", "sanitize", controller_path, "--sanact=1"],
        timeout=60.0,
        log_output=False,
    )

    haystack = f"{result.stdout} {result.stderr}".casefold().replace("_", " ")
    if "access denied" in haystack:
        _log.warning(
            "%s refuses Sanitize (Access Denied). A firmware erase will not run "
            "until the controller is unstuck; suspending to RAM and resuming "
            "clears it on the drives seen so far.",
            controller_path,
        )
        return False

    # An exit code with no recognisable message means the probe itself did not
    # work - a missing subcommand, a permissions problem. That is not evidence
    # the drive is fine, so it is reported as unknown.
    if not result.ok and not haystack.strip():
        return None

    return True


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

    dlfeat = read_namespace_dlfeat(device_path)
    capabilities.raw_dlfeat = dlfeat
    capabilities.deallocated_read_behaviour = dlfeat & _DLFEAT_READ_BEHAVIOUR_MASK

    if capabilities.any_sanitize_supported:
        log = read_sanitize_status(controller)
        if log is not None:
            capabilities.sanitize_in_progress = log.in_progress
            capabilities.sanitize_progress_percent = log.percent

        # Only probe when nothing is already running. Exit Failure Mode against
        # a sanitize in progress would be asking the controller to abandon work
        # it is part way through.
        if not capabilities.sanitize_in_progress:
            capabilities.sanitize_reachable = probe_sanitize_reachable(controller)

    # Enumerate namespaces so the erase path knows whether a single format can
    # cover the whole drive. Reported namespace count (NN) is the maximum the
    # controller supports, not how many are active, so it cannot be used here.
    namespaces = list_namespaces(controller)
    if namespaces:
        capabilities.namespace_count = len(namespaces)
        capabilities.active_namespaces = namespaces

    _log.info(
        "%s capabilities: format=%s crypto=%s sanitize(crypto=%s block=%s overwrite=%s) "
        "reachable=%s dlfeat=0x%02x [oacs=0x%04x fna=0x%02x sanicap=0x%08x]",
        controller,
        capabilities.format_supported,
        capabilities.crypto_erase_supported,
        capabilities.sanitize_crypto_supported,
        capabilities.sanitize_block_supported,
        capabilities.sanitize_overwrite_supported,
        capabilities.sanitize_reachable,
        dlfeat,
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
    # The log body, which on nvme-cli 2.x sits one level down under the device
    # name rather than at the top level.
    body = _unwrap_device(
        payload, "sstat", "status", "sanitize_status", "sprog", "cdw10_info"
    )

    decoded_erased: bool | None = None

    if isinstance(body, dict):
        # nvme-cli has used several spellings for these fields over the years.
        status_raw = _first_present(body, "sstat", "status", "sanitize_status")
        progress_raw = _first_present(body, "sprog", "progress", "sanitize_progress")

        # SSTAT may arrive as an object of already-decoded fields rather than
        # as the register:
        #     "sstat": {"global_erased": 1, "no_cmplted_passes": 0,
        #               "status": "(1) Most Recent Sanitize Command ..."}
        # This is the ONLY place Global Data Erased is reported faithfully.
        # The human-readable output prints SSTAT with bit 8 masked off, so
        # decoding the flag from that register always yields false - which is
        # how a purged drive came to be certified as one whose controller had
        # not set the bit.
        if isinstance(status_raw, dict):
            erased_field = _first_present(status_raw, "global_erased")
            if erased_field is not None:
                decoded_erased = bool(_coerce_register(erased_field))
            status_raw = _first_present(status_raw, "status", "sanitize_status")
        # "cdw10_info" is what nvme-cli actually emits - confirmed by reading
        # the key strings out of bookworm's own binary (2.4+really2.3-3). The
        # other spellings were guesses, none of them matched, and SCDW10
        # therefore read 0x00000000 on every poll while the drive's own log
        # page showed 0x2. A guessed key name is indistinguishable from a
        # controller that reported nothing.
        command_raw = _first_present(
            body, "cdw10_info", "scdw10", "sanitize_cdw10", "cdw10", "SCDW10"
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

        # The SSTAT hex on the line above this one has bit 8 masked off, so the
        # flag has to come from nvme-cli's decoded line instead of from the
        # register it is printed beside.
        flag = _GLOBAL_ERASED_TEXT.search(text.stdout)
        if flag is not None:
            decoded_erased = flag.group("state").lower() == "set"

    progress = _coerce_register(progress_raw)

    status = _coerce_status(status_raw)
    if status is None:
        # Unreadable is not "never sanitized". Reporting a drive as unerased
        # when it has in fact been erased is a certificate-grade error, and so
        # is the reverse, so neither is guessed at.
        _log.warning(
            "%s sanitize status could not be read from %r", controller_path, status_raw
        )
        return None

    # Prefer the decoded flag over the register. The register printed by the
    # human-readable output does not carry bit 8 at all, so decoding it there
    # yields false for every drive; the decoded field is the only faithful
    # source, and the register is used only when nothing decoded one.
    raw_register = _coerce_register(status_raw)
    global_erased = (
        decoded_erased
        if decoded_erased is not None
        else bool(raw_register & _SSTAT_GLOBAL_DATA_ERASED)
    )

    # Rebuilt rather than taken from the register, so the recorded value agrees
    # with the fields actually reported even when the register was masked.
    raw_sstat = (raw_register & ~_SSTAT_GLOBAL_DATA_ERASED & ~_SSTAT_STATUS_MASK) | status
    if global_erased:
        raw_sstat |= _SSTAT_GLOBAL_DATA_ERASED

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

        if device.nvme is not None:
            read_only, degraded, used = read_health(controller_path_for(device.path))
            device.nvme.media_read_only = read_only
            device.nvme.reliability_degraded = degraded
            device.nvme.percentage_used = used
            if read_only:
                _log.error(
                    "%s has placed its media in READ-ONLY mode (SMART critical "
                    "warning bit 3). No erase can succeed on it; the controller "
                    "answers format and sanitize with Access Denied.",
                    device.path,
                )
            elif degraded:
                _log.warning(
                    "%s reports degraded reliability (SMART critical warning "
                    "bit 2); %d%% of rated endurance used",
                    device.path,
                    used,
                )

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
