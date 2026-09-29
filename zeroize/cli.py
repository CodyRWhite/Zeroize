"""Command line entry point - argument parsing, privilege check, and a headless mode.

The graphical interface is the primary way Zeroize is used, but a command line
matters for three real cases: listing what a machine can see without starting a
session, scripting a bench that wipes the same hardware repeatedly, and
diagnosing a drive that the interface says it cannot erase.

Subcommands:

``zeroize`` (no subcommand)
    Launch the interface.
``zeroize list``
    Print every drive, its capabilities, and the methods it supports.
``zeroize erase --device ... --method ...``
    Erase from the command line. Requires ``--yes-i-am-sure`` so that no
    plausible typo can start one by accident.
``zeroize config``
    Print the merged configuration, including the organisation block.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __app_name__, __tagline__, __version__
from .config import load_settings
from .logging_setup import get_logger, setup_logging
from .process import set_dry_run

_log = get_logger("Cli")

#: The flag that must be present for a command-line erase to proceed.
_CONFIRMATION_FLAG = "--yes-i-am-sure"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zeroize",
        description=f"{__app_name__} - {__tagline__}",
        epilog="Run with no subcommand to launch the graphical interface.",
    )
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use a fixed set of fictitious drives; no hardware is touched",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="enumerate real drives, but log and skip every command that would change one",
    )
    parser.add_argument("--verbose", action="store_true", help="log at debug level")
    parser.add_argument(
        "--console",
        dest="console",
        action="store_true",
        default=None,
        help="force console logging even when stderr is not a terminal",
    )
    parser.add_argument(
        "--quiet",
        dest="console",
        action="store_false",
        help="suppress console logging",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="list attached drives and their erase capabilities")
    subparsers.add_parser("config", help="print the merged configuration")

    diagnostics = subparsers.add_parser(
        "diagnostics",
        help="export logs and system state for review elsewhere",
    )
    diagnostics.add_argument(
        "--output",
        metavar="DIRECTORY",
        help="where to write; defaults to the certificate volume, or home if none is attached",
    )

    thaw = subparsers.add_parser(
        "unfreeze",
        help="clear a firmware block so a firmware erase becomes available",
    )
    thaw.add_argument("--device", required=True, metavar="PATH", help="e.g. /dev/sda")
    thaw.add_argument(
        "--suspend",
        action="store_true",
        help=(
            "allow suspending this machine for a few seconds. Required for an "
            "NVMe controller refusing Sanitize, where nothing else clears it. "
            "Affects every drive attached, so it is never done implicitly."
        ),
    )

    erase = subparsers.add_parser("erase", help="erase drives without the interface")
    erase.add_argument(
        "--device",
        action="append",
        required=True,
        metavar="PATH",
        help="a device to erase, e.g. /dev/nvme0n1. Repeat for several drives.",
    )
    erase.add_argument(
        "--method",
        required=True,
        help="method key, as printed by `zeroize list`",
    )
    erase.add_argument("--operator", default="", help="name printed on the certificate")
    erase.add_argument(
        "--certificate-dir",
        type=Path,
        default=None,
        help="where to write the certificate; defaults to the configured directory",
    )
    erase.add_argument(
        _CONFIRMATION_FLAG,
        dest="confirmed",
        action="store_true",
        help="required. Without it the command refuses to run.",
    )

    return parser


def _require_root(action: str) -> bool:
    """True when the process has the privileges *action* needs."""
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        return True
    print(
        f"Zeroize must run as root to {action}. "
        f"Try: sudo zeroize  (or launch it from the desktop, which uses pkexec).",
        file=sys.stderr,
    )
    return False


def _command_list(settings, simulate: bool) -> int:
    from .discovery import discover_devices
    from .erase.methods import availability_for, recommended_method
    from .models import format_size

    devices = discover_devices(
        simulate=simulate,
        auto_unfreeze=settings.safety.auto_unfreeze_on_scan and not simulate,
    )
    if not devices:
        print("No block devices found.")
        return 1

    for device in devices:
        print(f"\n{device.path}  {device.product_name}")
        print(f"  Serial        {device.serial or 'not reported'}")
        print(f"  Capacity      {format_size(device.size_bytes)} ({device.size_bytes:,} bytes)")
        print(f"  Transport     {(device.transport or str(device.kind)).upper()}")
        print(f"  Partitioning  {device.partition_table_display}")

        if device.protection_reason:
            print(f"  PROTECTED     {device.protection_reason}")

        if device.nvme is not None:
            capabilities = device.nvme
            print(
                f"  NVMe          oacs=0x{capabilities.raw_oacs:04x} "
                f"fna=0x{capabilities.raw_fna:02x} "
                f"sanicap=0x{capabilities.raw_sanicap:08x} "
                f"namespaces={capabilities.active_namespaces or [capabilities.namespace_id]}"
            )
        if device.ata is not None and device.ata.supported:
            security = device.ata
            print(
                f"  ATA security  frozen={security.frozen} locked={security.locked} "
                f"enhanced={security.enhanced_erase_supported} "
                f"estimate={security.estimated_minutes}min"
            )

        preferred = recommended_method(device)
        print("  Methods:")
        for verdict in availability_for(device):
            marker = "*" if preferred and verdict.method.key == preferred.key else " "
            if verdict.supported:
                print(f"   {marker} {verdict.method.key:<26} {verdict.method.certificate_name}")
            else:
                print(f"     {verdict.method.key:<26} unavailable: {verdict.reason}")
        if preferred:
            print("  (* = recommended)")

    return 0


def _command_unfreeze(arguments, simulate: bool) -> int:
    """Clear a firmware block on erasing, then report what the drive allows.

    Two different conditions reach here. An ATA drive frozen by SECURITY FREEZE
    LOCK at POST is cleared by detaching and re-attaching that one drive. An
    NVMe controller answering Sanitize with Access Denied is not - only
    suspending the machine clears that, and it affects every drive attached, so
    it is never done without --suspend.
    """
    from .discovery import discover_devices
    from .erase.methods import availability_for
    from .erase.unfreeze import can_attempt, needs_suspend, suspend_warning, unfreeze

    devices = {device.path: device for device in discover_devices(simulate=simulate)}
    device = devices.get(arguments.device)
    if device is None:
        print(f"No such device: {arguments.device}", file=sys.stderr)
        return 2

    # Asked once, of the module that owns the question. Asking device.ata
    # directly here is what made this command answer "not frozen; nothing to
    # do" for an NVMe drive whose firmware erase was blocked.
    problem = can_attempt(device)
    if problem:
        print(f"{arguments.device}: {problem}")
        return 0

    allow_suspend = bool(getattr(arguments, "suspend", False))

    if needs_suspend(device):
        if not allow_suspend:
            print(
                f"{arguments.device}: the controller is refusing Sanitize.\n"
                f"\n"
                f"A bus reset does not clear this; only suspending the machine "
                f"for a few seconds does.\n"
                f"Every drive attached is affected, and no erase may be "
                f"running.\n"
                f"\n"
                f"Re-run with --suspend to go ahead.",
                file=sys.stderr,
            )
            warning = suspend_warning()
            if warning:
                print(f"\n{warning}", file=sys.stderr)
            return 1
        print(f"Suspending briefly to unstick {arguments.device}...")
    else:
        print(f"Attempting to clear the security freeze on {arguments.device}...")

    cleared, detail = unfreeze(device, allow_suspend=allow_suspend)
    print(f"  {detail}")

    if not cleared:
        print("\nStill blocked.", file=sys.stderr)
        return 1

    # The device may have come back under a different name, so re-discover and
    # match on serial rather than trusting the path.
    print("\nRescanning...")
    for candidate in discover_devices(simulate=simulate):
        if device.serial and candidate.serial == device.serial:
            state = []
            if candidate.ata is not None:
                state.append(f"frozen={candidate.ata.frozen}")
            if candidate.nvme is not None:
                state.append(f"sanitize_blocked={candidate.nvme.sanitize_blocked}")
            print(f"  {candidate.path}  serial {candidate.serial}  {' '.join(state)}")

            # Report every firmware method that became available, not only the
            # ATA ones - on an NVMe drive the whole point was Sanitize.
            firmware = {"ata_secure_erase", "nvme_sanitize", "nvme_format"}
            for verdict in availability_for(candidate):
                if verdict.supported and verdict.method.family in firmware:
                    print(f"  now available: {verdict.method.certificate_name}")
            return 0

    print("  the drive did not reappear; rescan or reboot", file=sys.stderr)
    return 1


def _command_diagnostics(arguments, settings, simulate: bool) -> int:
    """Gather logs and system state into one directory and say where it went.

    The same collection the interface's export button performs. It exists as a
    command because the situations worth diagnosing are exactly the ones where
    the desktop may not be usable.
    """
    from .diagnostics import collect_diagnostics
    from .discovery import discover_devices

    devices = discover_devices(simulate=simulate)
    destination = collect_diagnostics(
        devices,
        output_label=settings.certificate.output_volume_label,
        destination=Path(arguments.output) if arguments.output else None,
    )
    print(f"Diagnostics written to {destination}")
    return 0


def _command_config(settings) -> int:
    import json

    print(json.dumps(settings.to_dict(), indent=2))
    return 0


def _command_erase(arguments, settings, simulate: bool) -> int:
    from .certificate import issue_certificate
    from .discovery import discover_devices
    from .erase.engine import erase_devices
    from .erase.methods import METHODS_BY_KEY, availability_for
    from .models import format_size

    if not arguments.confirmed:
        print(
            f"Refusing to erase without {_CONFIRMATION_FLAG}.\n"
            f"This destroys all data on the named drives and cannot be undone.",
            file=sys.stderr,
        )
        return 2

    method = METHODS_BY_KEY.get(arguments.method)
    if method is None:
        print(f"Unknown method '{arguments.method}'. Run `zeroize list` to see the keys.", file=sys.stderr)
        return 2

    devices = {device.path: device for device in discover_devices(simulate=simulate)}

    assignments = []
    for path in arguments.device:
        device = devices.get(path)
        if device is None:
            print(f"No such device: {path}", file=sys.stderr)
            return 2

        verdict = next(item for item in availability_for(device) if item.method.key == method.key)
        if not verdict.supported:
            print(f"{path} cannot use {method.key}: {verdict.reason}", file=sys.stderr)
            return 2

        assignments.append((device, method))
        print(
            f"Will erase {path} ({device.serial or 'no serial'}, "
            f"{format_size(device.size_bytes)}) with {method.certificate_name}"
        )

    def report(update) -> None:
        percent = f"{update.percent:.0f}%" if update.percent is not None else "  --"
        print(f"  [{update.device_path}] {percent}  {update.message}")

    summary = erase_devices(
        assignments,
        settings,
        operator=arguments.operator,
        on_progress=report,
    )

    for result in summary.results:
        status = result.result_word.upper()
        print(f"{result.device.path}: {status} ({result.device.serial or 'no serial'})")
        for message in result.error_messages:
            print(f"    {message}", file=sys.stderr)

    try:
        pdf_path, _json_path = issue_certificate(
            summary, settings, output_directory=arguments.certificate_dir
        )
        print(f"\nCertificate: {pdf_path}")
        print(f"Serials in this run: {', '.join(summary.serials)}")
    except Exception as error:  # noqa: BLE001 - the erase already happened
        _log.exception("Certificate generation failed")
        print(f"Certificate could not be written: {error}", file=sys.stderr)
        return 1

    return 0 if summary.all_succeeded else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    log_path = setup_logging(verbose=arguments.verbose, console=arguments.console)
    _log.info("%s %s starting; log at %s", __app_name__, __version__, log_path)

    if arguments.dry_run:
        set_dry_run(True)

    settings = load_settings()

    # Simulated runs need no privileges; everything else talks to raw devices.
    needs_root = not arguments.simulate and arguments.command in (None, "list", "erase", "unfreeze")
    if needs_root and not _require_root("enumerate and erase drives"):
        return 13

    if arguments.command == "list":
        return _command_list(settings, arguments.simulate)
    if arguments.command == "config":
        return _command_config(settings)
    if arguments.command == "diagnostics":
        return _command_diagnostics(arguments, settings, arguments.simulate)
    if arguments.command == "unfreeze":
        return _command_unfreeze(arguments, arguments.simulate)
    if arguments.command == "erase":
        return _command_erase(arguments, settings, arguments.simulate)

    try:
        from .ui.app import run_application
    except ImportError as error:
        print(
            f"The graphical interface is unavailable: {error}\n"
            f"Install the GTK 4 and libadwaita Python bindings "
            f"(python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1), or use `zeroize list`.",
            file=sys.stderr,
        )
        return 1

    return run_application(settings, simulate=arguments.simulate)
