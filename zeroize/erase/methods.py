"""The catalogue of erase methods, and the rules deciding which a drive allows.

Two separate concerns live here and are deliberately kept apart:

* **The catalogue** - every method the application knows how to perform, with
  the exact pass patterns and the exact wording that appears on the
  certificate. These are static data.
* **The gating** - :func:`availability_for` asks a specific drive which of those
  methods it will actually accept, based on the capability registers discovery
  read from the hardware. Nothing is assumed from the drive's name, vendor or
  transport; a method is offered only when the controller said it is supported.

Unsupported methods are returned alongside supported ones, carrying the reason
they are unavailable, so the UI can grey them out with an explanation rather
than silently shortening the list. An operator who cannot see that a drive
lacks Sanitize support will assume the tool is broken.

A note on overwriting flash: multi-pass overwrite methods are offered for SSDs
and NVMe drives because operators are sometimes required by policy to use them,
but they are marked with a caution. Wear levelling and over-provisioning mean a
block-level overwrite cannot reach every cell that once held data, which is
precisely why NIST SP 800-88 directs you to the drive's own Purge command
instead. The UI surfaces that caution; it does not override the operator.
"""

from __future__ import annotations

from ..models import (
    AtaSecurity,
    Device,
    DeviceKind,
    EraseMethod,
    MethodAvailability,
    NvmeCapabilities,
)

# Method families, used by the engine to pick an implementation.
FAMILY_NVME_FORMAT = "nvme_format"
FAMILY_NVME_SANITIZE = "nvme_sanitize"
FAMILY_ATA_SECURE_ERASE = "ata_secure_erase"
FAMILY_SCSI_SANITIZE = "scsi_sanitize"
FAMILY_OVERWRITE = "overwrite"
FAMILY_ATA_SANITIZE = "ata_sanitize"
FAMILY_DISCARD = "discard"

#: Canonical Gutmann pattern sequence (passes 5-31); 1-4 and 32-35 are random.
_GUTMANN_PATTERNS: tuple[bytes, ...] = (
    b"\x55\x55\x55",
    b"\xaa\xaa\xaa",
    b"\x92\x49\x24",
    b"\x49\x24\x92",
    b"\x24\x92\x49",
    b"\x00\x00\x00",
    b"\x11\x11\x11",
    b"\x22\x22\x22",
    b"\x33\x33\x33",
    b"\x44\x44\x44",
    b"\x55\x55\x55",
    b"\x66\x66\x66",
    b"\x77\x77\x77",
    b"\x88\x88\x88",
    b"\x99\x99\x99",
    b"\xaa\xaa\xaa",
    b"\xbb\xbb\xbb",
    b"\xcc\xcc\xcc",
    b"\xdd\xdd\xdd",
    b"\xee\xee\xee",
    b"\xff\xff\xff",
    b"\x92\x49\x24",
    b"\x49\x24\x92",
    b"\x24\x92\x49",
    b"\x6d\xb6\xdb",
    b"\xb6\xdb\x6d",
    b"\xdb\x6d\xb6",
)


def _pattern_label(index: int, pattern: bytes | None) -> str:
    """Render a pass label the way the reference certificate does.

    A fill byte is shown widened to six bytes - ``Pass 4 (0x969696969696)`` -
    and a pseudorandom pass is shown as ``Pass 3 (Random)``.
    """
    if pattern is None:
        return f"Pass {index} (Random)"
    repeated = (pattern * (6 // len(pattern) + 1))[:6]
    return f"Pass {index} (0x{repeated.hex().upper()})"


def _passes(patterns: list[bytes | None], *, verify_last: bool = True) -> list:
    """Build the :class:`PassSpec` list for an overwrite method."""
    from ..models import PassSpec  # imported here to keep the module header light

    specs = [
        PassSpec(pattern=pattern, label=_pattern_label(index, pattern))
        for index, pattern in enumerate(patterns, start=1)
    ]
    if verify_last and specs:
        specs[-1].verify = True
    return specs


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------

NVME_FORMAT_USER_DATA = EraseMethod(
    key="nvme-format-user-data",
    certificate_name="NVMe Format (SES=1); User Data Erase",
    family=FAMILY_NVME_FORMAT,
    summary="Ask the controller to erase all user data in the namespace.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Seconds to a few minutes",
)

NVME_FORMAT_CRYPTO = EraseMethod(
    key="nvme-format-crypto",
    certificate_name="NVMe Format (SES=2); Cryptographic Erase",
    family=FAMILY_NVME_FORMAT,
    summary="Destroy the media encryption key, rendering all data unreadable.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Near instant",
)

NVME_SANITIZE_BLOCK = EraseMethod(
    key="nvme-sanitize-block",
    certificate_name="NVMe Sanitize (Block Erase)",
    family=FAMILY_NVME_SANITIZE,
    summary="Electrically erase every block, including over-provisioned areas.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Minutes",
    non_interruptible=True,
)

NVME_SANITIZE_CRYPTO = EraseMethod(
    key="nvme-sanitize-crypto",
    certificate_name="NVMe Sanitize (Crypto Erase)",
    family=FAMILY_NVME_SANITIZE,
    summary="Sanitize-grade key destruction, covering all reachable media.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Near instant",
    non_interruptible=True,
)

NVME_SANITIZE_OVERWRITE = EraseMethod(
    key="nvme-sanitize-overwrite",
    certificate_name="NVMe Sanitize (Overwrite)",
    family=FAMILY_NVME_SANITIZE,
    summary="Controller-driven overwrite of all media with a fixed pattern.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Minutes to hours",
    non_interruptible=True,
)

ATA_SECURE_ERASE = EraseMethod(
    key="ata-secure-erase",
    certificate_name="ATA Secure Erase",
    family=FAMILY_ATA_SECURE_ERASE,
    summary="The drive's own SECURITY ERASE UNIT command.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Minutes to hours, depending on capacity",
    non_interruptible=True,
)

ATA_SECURE_ERASE_ENHANCED = EraseMethod(
    key="ata-secure-erase-enhanced",
    certificate_name="ATA Enhanced Secure Erase",
    family=FAMILY_ATA_SECURE_ERASE,
    summary="SECURITY ERASE UNIT with the enhanced bit - also clears reallocated sectors.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Minutes to hours, depending on capacity",
    non_interruptible=True,
)

SCSI_SANITIZE_BLOCK = EraseMethod(
    key="scsi-sanitize-block",
    certificate_name="SCSI Sanitize (Block Erase)",
    family=FAMILY_SCSI_SANITIZE,
    summary="The SCSI SANITIZE command with the block erase service action.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Minutes",
    non_interruptible=True,
)

SCSI_SANITIZE_CRYPTO = EraseMethod(
    key="scsi-sanitize-crypto",
    certificate_name="SCSI Sanitize (Crypto Erase)",
    family=FAMILY_SCSI_SANITIZE,
    summary="The SCSI SANITIZE command with the cryptographic erase service action.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Near instant",
    non_interruptible=True,
)

ATA_SANITIZE_CRYPTO = EraseMethod(
    key="ata-sanitize-crypto",
    certificate_name="ATA Sanitize (Crypto Scramble)",
    family=FAMILY_ATA_SANITIZE,
    summary="Regenerate the drive's internal encryption keys.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Near instant",
    non_interruptible=True,
)

ATA_SANITIZE_BLOCK = EraseMethod(
    key="ata-sanitize-block",
    certificate_name="ATA Sanitize (Block Erase)",
    family=FAMILY_ATA_SANITIZE,
    summary="Electrically erase every block, including over-provisioned areas.",
    standard="NIST SP 800-88r1 Purge",
    speed_hint="Minutes",
    non_interruptible=True,
)

DISCARD_TRIM = EraseMethod(
    key="discard-trim",
    certificate_name="Whole-device TRIM (deterministic zeros); 1 pass",
    family=FAMILY_DISCARD,
    summary="Discard every block. Offered only where the drive guarantees zeros afterwards.",
    # NOT a NIST SP 800-88 method, and not DoD 5220.22-M either. NIST lists
    # ATA SANITIZE and SECURITY ERASE UNIT as Purge for ATA SSDs, and a full
    # overwrite as Clear; TRIM appears at no level. DoD 5220.22-M was written
    # for magnetic media and was removed from the NISPOM in 2007.
    #
    # It is offered anyway because on a drive that guarantees deterministic
    # zeros after TRIM it is fast, verifiable, and works on a frozen drive
    # where the alternative is hours of overwriting. But the certificate says
    # what it is, because a certificate that claims a standard it does not meet
    # is worse than no certificate.
    standard="Vendor TRIM - outside NIST SP 800-88",
    speed_hint="Seconds",
)

OVERWRITE_ZERO = EraseMethod(
    key="overwrite-zero",
    certificate_name="Single pass zeros; 1 pass",
    family=FAMILY_OVERWRITE,
    summary="Write zeros across the whole device once, then verify.",
    standard="NIST SP 800-88r1 Clear",
    passes=_passes([b"\x00"]),
    speed_hint="Limited by write speed",
)

OVERWRITE_RANDOM = EraseMethod(
    key="overwrite-random",
    certificate_name="Single pass pseudorandom; 1 pass",
    family=FAMILY_OVERWRITE,
    summary="Write pseudorandom data across the whole device once, then verify.",
    standard="NIST SP 800-88r1 Clear",
    passes=_passes([None]),
    speed_hint="Limited by write speed",
)

OVERWRITE_DOD_3 = EraseMethod(
    key="overwrite-dod-3",
    certificate_name="US DoD 5220.22-M; 3 passes",
    family=FAMILY_OVERWRITE,
    summary="Zeros, ones, then pseudorandom, with a verification read.",
    standard="US DoD 5220.22-M",
    passes=_passes([b"\x00", b"\xff", None]),
    speed_hint="Three times the single-pass time",
)

OVERWRITE_DOD_7 = EraseMethod(
    key="overwrite-dod-7",
    certificate_name="US DoD 5220.22-M (ECE); 7 passes",
    family=FAMILY_OVERWRITE,
    summary="The extended character erase sequence: seven patterned passes.",
    standard="US DoD 5220.22-M ECE",
    passes=_passes([b"\x00", b"\xff", None, b"\x96", b"\x00", b"\xff", None]),
    speed_hint="Seven times the single-pass time",
)

OVERWRITE_HMG_3 = EraseMethod(
    key="overwrite-hmg-3",
    certificate_name="British HMG IS5 (Enhanced); 3 passes",
    family=FAMILY_OVERWRITE,
    summary="Zeros, ones, then pseudorandom with a full verification read.",
    standard="HMG Infosec Standard 5 Enhanced",
    passes=_passes([b"\x00", b"\xff", None]),
    speed_hint="Three times the single-pass time",
)

OVERWRITE_VSITR_7 = EraseMethod(
    key="overwrite-vsitr-7",
    certificate_name="German VSITR; 7 passes",
    family=FAMILY_OVERWRITE,
    summary="The BSI VS-ITR sequence: alternating fills ending in 0xAA.",
    standard="BSI VS-ITR",
    passes=_passes([b"\x00", b"\xff", b"\x00", b"\xff", b"\x00", b"\xff", b"\xaa"]),
    speed_hint="Seven times the single-pass time",
)

OVERWRITE_GUTMANN_35 = EraseMethod(
    key="overwrite-gutmann-35",
    certificate_name="Peter Gutmann; 35 passes",
    family=FAMILY_OVERWRITE,
    summary="The full 35-pass Gutmann sequence. Designed for 1990s MFM/RLL drives.",
    standard="Gutmann",
    passes=_passes(
        [None, None, None, None, *_GUTMANN_PATTERNS, None, None, None, None]
    ),
    speed_hint="Thirty-five times the single-pass time - typically days",
)

#: Every method, in the order the UI lists them: hardware commands first,
#: because they are both faster and more thorough than a software overwrite.
ALL_METHODS: tuple[EraseMethod, ...] = (
    NVME_SANITIZE_CRYPTO,
    NVME_SANITIZE_BLOCK,
    NVME_SANITIZE_OVERWRITE,
    NVME_FORMAT_CRYPTO,
    NVME_FORMAT_USER_DATA,
    ATA_SECURE_ERASE_ENHANCED,
    ATA_SECURE_ERASE,
    ATA_SANITIZE_CRYPTO,
    ATA_SANITIZE_BLOCK,
    SCSI_SANITIZE_CRYPTO,
    SCSI_SANITIZE_BLOCK,
    DISCARD_TRIM,
    OVERWRITE_ZERO,
    OVERWRITE_RANDOM,
    OVERWRITE_DOD_3,
    OVERWRITE_HMG_3,
    OVERWRITE_DOD_7,
    OVERWRITE_VSITR_7,
    OVERWRITE_GUTMANN_35,
)

METHODS_BY_KEY: dict[str, EraseMethod] = {method.key: method for method in ALL_METHODS}


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------

def _nvme_reason(method: EraseMethod, capabilities: NvmeCapabilities | None) -> str:
    """Why an NVMe method is unavailable for a drive, or an empty string."""
    if capabilities is None:
        return "the controller did not answer an identify command"

    if method.family == FAMILY_NVME_FORMAT:
        if not capabilities.format_supported:
            return "the controller does not support the Format NVM command (OACS bit 1 clear)"
        if method is NVME_FORMAT_CRYPTO and not capabilities.crypto_erase_supported:
            return "the controller does not support cryptographic erase (FNA bit 2 clear)"
        return ""

    if method.family == FAMILY_NVME_SANITIZE:
        if not capabilities.any_sanitize_supported:
            return "the controller does not support the Sanitize command (SANICAP is zero)"
        supported = {
            NVME_SANITIZE_CRYPTO.key: capabilities.sanitize_crypto_supported,
            NVME_SANITIZE_BLOCK.key: capabilities.sanitize_block_supported,
            NVME_SANITIZE_OVERWRITE.key: capabilities.sanitize_overwrite_supported,
        }.get(method.key, False)
        if not supported:
            return "the controller does not support this sanitize action (SANICAP bit clear)"
        if capabilities.sanitize_in_progress:
            return f"a sanitize is already running ({capabilities.sanitize_progress_percent:.0f}% complete)"
        return ""

    return ""


def _ata_reason(method: EraseMethod, security: AtaSecurity | None) -> str:
    """Why an ATA Secure Erase variant is unavailable, or an empty string."""
    if security is None or not security.supported:
        return "the drive does not report the ATA security feature set"
    if security.frozen:
        return (
            "the drive is frozen by the system firmware. Run "
            "'zeroize unfreeze --device <path>' to try clearing it, or from a "
            "terminal: echo 1 | sudo tee /sys/block/<name>/device/delete "
            "&& echo '- - -' | sudo tee /sys/class/scsi_host/host*/scan"
        )
    if security.locked:
        return "the drive is locked by a security password that must be cleared first"
    if method is ATA_SECURE_ERASE_ENHANCED and not security.enhanced_erase_supported:
        return "the drive does not support the enhanced erase variant"
    return ""


def _ata_sanitize_reason(method: EraseMethod, security: AtaSecurity | None) -> str:
    """Why ATA Sanitize is unavailable, or an empty string.

    Note what is *not* checked here: the security freeze. ATA Sanitize is a
    separate feature set with its own commands, and SECURITY FREEZE LOCK does
    not touch it - which is exactly why it is worth offering on a drive whose
    Secure Erase is blocked.
    """
    if security is None:
        return "the drive did not report its capabilities"
    if method is ATA_SANITIZE_CRYPTO and not security.ata_sanitize_crypto_supported:
        return "the drive does not report CRYPTO_SCRAMBLE_EXT in its SANITIZE feature set"
    if method is ATA_SANITIZE_BLOCK and not security.ata_sanitize_block_supported:
        return "the drive does not report BLOCK_ERASE_EXT in its SANITIZE feature set"
    return ""


def _discard_reason(device: Device, security: AtaSecurity | None) -> str:
    """Why a whole-device discard is unavailable, or an empty string.

    Gated on the drive *guaranteeing* zeros after TRIM, not merely supporting
    TRIM. Without that guarantee the drive may return the old contents from a
    discarded block, so the operation would prove nothing and the verification
    would be meaningless.
    """
    if device.rotational:
        return "TRIM applies to flash media, and this is a rotational drive"
    if security is None or not security.trim_supported:
        return "the drive does not report TRIM support"
    if not security.deterministic_zeros_after_trim:
        return (
            "the drive supports TRIM but does not guarantee zeros afterwards, "
            "so a discard could not be verified"
        )
    return ""


def _scsi_reason(security: AtaSecurity | None) -> str:
    """Why SCSI Sanitize is unavailable, or an empty string."""
    if security is None or not security.scsi_sanitize_supported:
        return "the drive did not accept a SCSI SANITIZE probe"
    return ""


def _overwrite_reason(device: Device) -> str:
    """Why a software overwrite is impossible, or an empty string.

    Software overwrite is the universal fallback: any device that can be opened
    for writing can be overwritten, so the only hard blockers are the ones that
    apply to every method.
    """
    if device.read_only:
        return "the device is read-only"
    return ""


def overwrite_caution(device: Device) -> str:
    """A warning to show with overwrite methods on flash media, else empty.

    Not a blocker. Some sanitisation policies still mandate a multi-pass
    overwrite, and the operator is entitled to run one - but they should know
    that on flash it is both slower and less complete than the drive's own
    purge command.
    """
    if device.rotational:
        return ""
    return (
        "This is flash media. Wear levelling and over-provisioning mean a "
        "block-level overwrite cannot reach every cell that held data. "
        "NIST SP 800-88r1 recommends the drive's own purge command instead."
    )


def method_caution(device: Device, method: EraseMethod) -> str:
    """Anything the operator should know before choosing *method* on *device*.

    A caution is never a blocker - the method is supported and will run. It
    exists because several of these commands are correct choices that still
    have a caveat worth reading once.
    """
    if method.family == FAMILY_OVERWRITE:
        return overwrite_caution(device)

    if method in (NVME_FORMAT_CRYPTO, NVME_SANITIZE_CRYPTO, SCSI_SANITIZE_CRYPTO):
        return (
            "A cryptographic erase destroys the media encryption key, so it only "
            "protects data the drive actually encrypted. If self-encryption was "
            "off for part of this drive's life, use a block erase instead."
        )

    if method.family == FAMILY_NVME_FORMAT and device.nvme is not None:
        capabilities = device.nvme
        if len(capabilities.active_namespaces) > 1 and not capabilities.format_applies_to_all_namespaces:
            return (
                f"This drive exposes {len(capabilities.active_namespaces)} namespaces and does "
                "not format them together, so each one will be formatted in turn. "
                "Every namespace is listed separately on the certificate."
            )

    if method.family == FAMILY_DISCARD:
        note = (
            "TRIM is not a NIST SP 800-88 or DoD 5220.22-M method. It is fast "
            "and, on this drive, verifiable - the firmware guarantees zeros "
            "afterwards - but it is not required to reach blocks held in "
            "over-provisioning. The certificate records it as a vendor TRIM, "
            "not as a Clear or a Purge."
        )
        if device.ata is not None and device.ata.self_encrypting:
            note += (
                " This drive encrypts all user data, so anything unreachable "
                "is ciphertext."
            )
        return note

    if method.non_interruptible:
        return "Once started this cannot be cancelled; the drive is unusable until it finishes."

    return ""


def availability_for(device: Device) -> list[MethodAvailability]:
    """Return every method with a verdict on whether *device* supports it.

    The list is always complete and always in :data:`ALL_METHODS` order, so the
    UI can render a stable list with unsupported entries greyed out.
    """
    verdicts: list[MethodAvailability] = []

    for method in ALL_METHODS:
        if not device.can_be_erased:
            reason = device.protection_reason or "the device is protected"
            verdicts.append(MethodAvailability(method=method, supported=False, reason=reason))
            continue

        if method.family in (FAMILY_NVME_FORMAT, FAMILY_NVME_SANITIZE):
            reason = (
                "this is not an NVMe device"
                if device.kind is not DeviceKind.NVME
                else _nvme_reason(method, device.nvme)
            )
        elif method.family == FAMILY_ATA_SECURE_ERASE:
            reason = (
                "ATA Secure Erase does not apply to NVMe devices"
                if device.kind is DeviceKind.NVME
                else _ata_reason(method, device.ata)
            )
        elif method.family == FAMILY_ATA_SANITIZE:
            reason = (
                "ATA Sanitize does not apply to NVMe devices"
                if device.kind is DeviceKind.NVME
                else _ata_sanitize_reason(method, device.ata)
            )
        elif method.family == FAMILY_DISCARD:
            reason = _discard_reason(device, device.ata)
        elif method.family == FAMILY_SCSI_SANITIZE:
            reason = (
                "SCSI Sanitize does not apply to NVMe devices"
                if device.kind is DeviceKind.NVME
                else _scsi_reason(device.ata)
            )
        else:
            reason = _overwrite_reason(device)

        verdicts.append(MethodAvailability(method=method, supported=not reason, reason=reason))

    return verdicts


def supported_methods(device: Device) -> list[EraseMethod]:
    """Just the methods *device* will accept, in catalogue order."""
    return [verdict.method for verdict in availability_for(device) if verdict.supported]


def recommended_method(device: Device) -> EraseMethod | None:
    """The method to preselect for *device*.

    Preference follows NIST SP 800-88r1: use the drive's own purge command when
    it has one, preferring a cryptographic erase (instant and complete) over a
    block erase, and fall back to a software overwrite only when the hardware
    offers nothing. Returns ``None`` for a device that cannot be erased at all.
    """
    preference = (
        NVME_SANITIZE_CRYPTO,
        NVME_SANITIZE_BLOCK,
        NVME_FORMAT_CRYPTO,
        NVME_FORMAT_USER_DATA,
        SCSI_SANITIZE_CRYPTO,
        SCSI_SANITIZE_BLOCK,
        # Above Secure Erase: ATA Sanitize is an equivalent purge that the
        # security freeze cannot block, so it is the better bet when both are
        # available and the only option when the drive is frozen.
        ATA_SANITIZE_CRYPTO,
        ATA_SANITIZE_BLOCK,
        ATA_SECURE_ERASE_ENHANCED,
        ATA_SECURE_ERASE,
        OVERWRITE_DOD_3,
        OVERWRITE_ZERO,
    )
    available = {method.key for method in supported_methods(device)}
    for method in preference:
        if method.key in available:
            return method
    return None


def common_methods(devices: list[Device]) -> list[MethodAvailability]:
    """Methods that *every* device in the selection supports.

    Used when several drives are selected and one method is applied to all of
    them. A method unsupported by even one drive is reported unsupported, with
    that drive named in the reason so the operator knows which to deselect.
    """
    if not devices:
        return []

    verdicts: list[MethodAvailability] = []
    per_device = {device.path: {verdict.method.key: verdict for verdict in availability_for(device)} for device in devices}

    for method in ALL_METHODS:
        blocking = [
            (device, per_device[device.path][method.key])
            for device in devices
            if not per_device[device.path][method.key].supported
        ]
        if not blocking:
            verdicts.append(MethodAvailability(method=method, supported=True))
            continue

        device, verdict = blocking[0]
        extra = f" (and {len(blocking) - 1} other drive(s))" if len(blocking) > 1 else ""
        verdicts.append(
            MethodAvailability(
                method=method,
                supported=False,
                reason=f"{device.name}: {verdict.reason}{extra}",
            )
        )

    return verdicts
