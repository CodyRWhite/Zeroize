#!/usr/bin/env python3
"""Render a sample erase certificate from the simulated device set.

Run this after changing anything in :mod:`zeroize.certificate` or
:mod:`zeroize.branding`. It builds a realistic multi-drive run - including a
failed drive, because the failure path is the one that never gets exercised by
accident - and renders the certificate so the layout can be looked at rather
than assumed.

    python tools/make_sample_certificate.py [output-directory]

It needs only ReportLab, not GTK, and it touches no hardware, so it runs on any
platform the project is developed on.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zeroize.config import Settings  # noqa: E402
from zeroize.discovery.simulation import simulated_devices  # noqa: E402
from zeroize.erase.methods import (  # noqa: E402
    NVME_FORMAT_CRYPTO,
    NVME_SANITIZE_BLOCK,
    OVERWRITE_DOD_7,
)
from zeroize.models import EraseResult, JobState, PassResult, RunSummary  # noqa: E402


def _build_summary() -> RunSummary:
    """A three-drive run: two successes and one failure."""
    devices = {device.path: device for device in simulated_devices()}
    started = datetime(2026, 9, 26, 9, 14, 3).astimezone()

    crypto = EraseResult(
        device=devices["/dev/nvme0n1"],
        method=NVME_FORMAT_CRYPTO,
        state=JobState.SUCCEEDED,
        started_at=started,
        finished_at=started + timedelta(seconds=11),
        passes=[PassResult(label="Cryptographic erase (SES=2)", succeeded=True)],
        verification_percent=10.0,
        verification_passed=True,
        commands=["nvme format /dev/nvme0n1 --namespace-id=1 --ses=2 --force"],
        bytes_written=devices["/dev/nvme0n1"].size_bytes,
    )
    crypto.passes.append(
        PassResult(
            label="Verification",
            succeeded=True,
            detail="no recoverable structures found in 25.0 GB sampled (10.0% of the device); "
            "100.0% of sampled bytes were zero",
        )
    )

    sanitize = EraseResult(
        device=devices["/dev/nvme1n1"],
        method=NVME_SANITIZE_BLOCK,
        state=JobState.SUCCEEDED,
        started_at=started + timedelta(seconds=2),
        finished_at=started + timedelta(minutes=6, seconds=41),
        passes=[
            PassResult(label="Sanitize - block erase (SANACT=2)", succeeded=True),
            PassResult(
                label="Verification",
                succeeded=True,
                detail="no recoverable structures found in 192.0 GB sampled (10.0% of the device); "
                "100.0% of sampled bytes were zero",
            ),
        ],
        verification_percent=10.0,
        verification_passed=True,
        commands=["nvme sanitize /dev/nvme1 --sanact=2"],
        bytes_written=devices["/dev/nvme1n1"].size_bytes,
    )

    # The failure case: a multi-pass overwrite that hit a bad sector partway
    # through pass 4 of 7.
    failed_device = devices["/dev/sdb"]
    overwrite = EraseResult(
        device=failed_device,
        method=OVERWRITE_DOD_7,
        state=JobState.FAILED,
        started_at=started + timedelta(seconds=4),
        finished_at=started + timedelta(hours=9, minutes=52, seconds=17),
        passes=[
            PassResult(label=spec.label, succeeded=True)
            for spec in OVERWRITE_DOD_7.passes[:3]
        ]
        + [
            PassResult(
                label=OVERWRITE_DOD_7.passes[3].label,
                succeeded=False,
                detail="write failed at byte 1284378624000 (1.28 TB in): [Errno 5] Input/output error",
            )
        ]
        + [
            PassResult(label=spec.label, succeeded=False, detail="not attempted")
            for spec in OVERWRITE_DOD_7.passes[4:]
        ],
        error_messages=[
            "write failed at byte 1284378624000 (1.28 TB in): [Errno 5] Input/output error"
        ],
        commands=[
            "# internal writer: 7 pass(es) over /dev/sdb block_size=4194304 verify_samples=4768"
        ],
        bytes_written=1_284_378_624_000,
    )

    return RunSummary(
        results=[crypto, sanitize, overwrite],
        started_at=started,
        finished_at=started + timedelta(hours=9, minutes=52, seconds=21),
        operator="C. White",
        machine="5CG22568DQ",
        system_info={
            "OS": "Ubuntu 24.04.1 LTS",
            "Type": "64-bit",
            "Kernel": "6.8.0-45-generic (linux)",
            "Hostname": "bench-wipe-01",
        },
        hardware_info={
            "Processor": "12th Gen Intel(R) Core(TM) i5-1235U (x86_64)",
            "Logical Processors": "12",
            "Motherboard": "HP 8ABB",
            "Motherboard Serial": "PQSPP00WBGS19B",
            "BIOS": "HP U71 Ver. 01.17.00",
            "BIOS Serial": "5CG22568DQ",
            "Memory": "31.6 GB",
        },
    )


def main() -> int:
    from zeroize.certificate.naming import certificate_filename
    from zeroize.certificate.pdf import render_certificate, write_json_sidecar

    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build") / "sample"
    destination.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    settings.organisation.name = "Contoso Asset Disposal"
    settings.organisation.department = "IT Operations"
    settings.organisation.address_lines = ["1200 Industrial Parkway", "Burlington, ON L7L 5H9"]
    settings.organisation.contact_email = "itad@contoso.example"
    settings.organisation.customer = "Northwind Traders"
    settings.organisation.reference = "WO-2026-04417"

    summary = _build_summary()
    pdf_path = destination / certificate_filename(summary)
    render_certificate(summary, settings, pdf_path)
    json_path = write_json_sidecar(summary, settings, pdf_path.with_suffix(".json"))

    print(f"PDF  : {pdf_path}")
    print(f"JSON : {json_path}")
    print(f"Serials in this run: {', '.join(summary.serials)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
