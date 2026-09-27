# File index

Every file, what it is for, and where to read more. Each module carries a
docstring covering its design and its traps - see the note on per-file
documentation in [OVERVIEW.md](OVERVIEW.md).

## Core

| File | Purpose |
|---|---|
| [`zeroize/__init__.py`](../zeroize/__init__.py) | Product identity - name, version, app id, slug. The single source every other file reads. |
| [`zeroize/branding.py`](../zeroize/branding.py) | The palette and the mark, as vector geometry. Both the interface and the PDF draw from it, and the build generates every icon from it. |
| [`zeroize/models.py`](../zeroize/models.py) | The data model shared by every layer: `Device`, `EraseMethod`, `EraseResult`, `RunSummary`, and the size/duration formatters. |
| [`zeroize/config.py`](../zeroize/config.py) | Layered configuration and the organisation block printed on certificates. |
| [`zeroize/paths.py`](../zeroize/paths.py) | Where logs, certificates and configuration live. Resolves the *invoking* user under `pkexec`, so certificates do not land in root's home. |
| [`zeroize/process.py`](../zeroize/process.py) | The only module that runs an external command. Audit trail, dry-run switch, heartbeat runner. |
| [`zeroize/logging_setup.py`](../zeroize/logging_setup.py) | CMTrace-format logging, one timestamped file per run. |
| [`zeroize/cli.py`](../zeroize/cli.py) | Argument parsing, privilege check, and the `list` / `config` / `erase` subcommands. |
| [`zeroize/__main__.py`](../zeroize/__main__.py) | `python -m zeroize`. |

## Discovery

| File | Purpose |
|---|---|
| [`discovery/__init__.py`](../zeroize/discovery/__init__.py) | `discover_devices()` - the one call the interface and CLI use. |
| [`discovery/block_devices.py`](../zeroize/discovery/block_devices.py) | `lsblk` enumeration, partition tree, **and the system-disk protection flag**. |
| [`discovery/nvme.py`](../zeroize/discovery/nvme.py) | `OACS` / `FNA` / `SANICAP` decoding, namespace enumeration, sanitize status log. |
| [`discovery/ata.py`](../zeroize/discovery/ata.py) | The `hdparm -I` security block, and the SCSI SANITIZE probe. |
| [`discovery/sysinfo.py`](../zeroize/discovery/sysinfo.py) | Host identification for the certificate, from DMI. |
| [`discovery/simulation.py`](../zeroize/discovery/simulation.py) | The fake drive set behind `--simulate`. |

## Erase

| File | Purpose |
|---|---|
| [`erase/methods.py`](../zeroize/erase/methods.py) | The method catalogue and **the gating rules**. Pass patterns, certificate wording, cautions. |
| [`erase/context.py`](../zeroize/erase/context.py) | The contract between the engine and each implementation. |
| [`erase/engine.py`](../zeroize/erase/engine.py) | Scheduling, the preflight interlocks, and assembling the `RunSummary`. |
| [`erase/nvme_ops.py`](../zeroize/erase/nvme_ops.py) | Format NVM and Sanitize. Multi-namespace scope; explicit completion status. |
| [`erase/ata_ops.py`](../zeroize/erase/ata_ops.py) | ATA Secure Erase (arm, erase, confirm) and SCSI Sanitize. |
| [`erase/overwrite.py`](../zeroize/erase/overwrite.py) | The multi-pass software writer, with digest-based verification. |
| [`erase/verify.py`](../zeroize/erase/verify.py) | Post-erase sampling for firmware methods - looks for structures that should not have survived. |

## Certificate

| File | Purpose |
|---|---|
| [`certificate/__init__.py`](../zeroize/certificate/__init__.py) | `issue_certificate()` - writes the PDF and the JSON sidecar. |
| [`certificate/naming.py`](../zeroize/certificate/naming.py) | Filenames, the certificate id, and the multi-serial collapse rule. |
| [`certificate/pdf.py`](../zeroize/certificate/pdf.py) | The document itself: header band, seal, drive summary table, per-drive detail, host block, command appendix. |

## Interface

| File | Purpose |
|---|---|
| [`ui/app.py`](../zeroize/ui/app.py) | The libadwaita application and the Zeroize stylesheet. |
| [`ui/main_window.py`](../zeroize/ui/main_window.py) | The three pages, the run, and the worker-to-main-loop boundary. |
| [`ui/device_row.py`](../zeroize/ui/device_row.py) | One drive: selection, identity, partition map, method chooser, capability report. |
| [`ui/partition_bar.py`](../zeroize/ui/partition_bar.py) | The proportional partition map. |
| [`ui/dialogs.py`](../zeroize/ui/dialogs.py) | The typed-confirmation gate and the capability dialog. |
| [`ui/progress_view.py`](../zeroize/ui/progress_view.py) | Live progress per drive, and the result page. |

## Build and packaging

| File | Purpose |
|---|---|
| [`build.py`](../build.py) | The single build entrypoint: `stage`, `deb`, `rpm`, `iso`, `all`, `clean`. |
| [`packaging/share/bin/zeroize`](../packaging/share/bin/zeroize) | The installed launcher. |
| [`packaging/share/applications/`](../packaging/share/applications/) | The `.desktop` entry, which launches via `pkexec`. |
| [`packaging/share/polkit/`](../packaging/share/polkit/) | The polkit action authorising the escalation. |
| [`packaging/share/metainfo/`](../packaging/share/metainfo/) | AppStream metadata for software centres. |
| [`packaging/iso/build-iso.sh`](../packaging/iso/build-iso.sh) | The live image build. See its [README](../packaging/iso/README.md). |

## Tests and tools

| File | Purpose |
|---|---|
| [`tests/test_methods.py`](../tests/test_methods.py) | Capability gating: what each simulated drive is and is not offered. |
| [`tests/test_nvme.py`](../tests/test_nvme.py) | Format scope across namespaces, and sanitize completion semantics. |
| [`tests/test_certificate.py`](../tests/test_certificate.py) | Naming, the multi-serial rule, and rendering every shape of run. |
| [`tests/test_safety.py`](../tests/test_safety.py) | The protection interlock, signature detection, sampling, configuration. |
| [`tools/make_sample_certificate.py`](../tools/make_sample_certificate.py) | Renders a sample certificate for layout review. |
| [`tools/Verify-UsbMedium.ps1`](../tools/Verify-UsbMedium.ps1) | Reads a written USB stick back and proves it matches the ISO byte for byte. |
