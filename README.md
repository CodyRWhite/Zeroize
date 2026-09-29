<div align="center">

<img src="docs/assets/zeroize-banner.png" alt="Zeroize Drive Wiper" width="760">

**Purge-grade drive erasure with proof, for Linux.**

[![License: MIT](https://img.shields.io/badge/License-MIT-FF6B1A?style=flat-square)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-16181D?style=flat-square&logo=python&logoColor=white)](#requirements)
[![GTK 4](https://img.shields.io/badge/GTK-4.8%2B-16181D?style=flat-square&logo=gtk&logoColor=white)](#requirements)
[![NIST SP 800-88r1](https://img.shields.io/badge/NIST-SP%20800--88r1-FF6B1A?style=flat-square)](#erase-methods)
[![Packaging](https://img.shields.io/badge/deb%20%7C%20rpm%20%7C%20live%20ISO-16181D?style=flat-square&logo=debian&logoColor=white)](#installing)
[![Secure Boot](https://img.shields.io/badge/Secure%20Boot-shim%20signed-FF6B1A?style=flat-square)](#bootable-live-usb)

</div>

---

Zeroize discovers attached drives, shows what is on them, erases the ones you
select using a method the drive's own firmware will accept, and issues a PDF
certificate recording what was destroyed, how, and on which machine. When
several drives are erased together, one certificate covers the run and lists
every serial number.

It ships as a `.deb`, an `.rpm`, and a bootable live USB image that boots to a
desktop with the application already running.

---

## What it does differently

**It asks the drive what it can do.** Not every NVMe controller implements
Format NVM, and far fewer implement Sanitize. Zeroize reads the capability
registers - `OACS`, `FNA`, `SANICAP` for NVMe; the ATA security block for
SATA - and offers only the methods that drive will actually accept. Methods it
cannot use are shown greyed out **with the reason**, so an operator can see
that a drive lacks Sanitize support rather than wondering where the option
went.

**It cannot erase the machine it is running on.** The drive carrying root,
`/boot`, active swap, or the live medium Zeroize itself booted from is detected
and can never be selected. The check is based on what is actually mounted, not
on device naming - on a live USB the boot medium is frequently `/dev/sda` while
the drive to be wiped is `/dev/nvme0n1`, and a naming heuristic gets that
exactly backwards.

**It tells the truth about what it did.** A failed erase still produces a
certificate, marked as a failure, listing which pass failed and why. Verification
is reported as what it actually was - "no recoverable structures found in 10% of
the device, sampled" - rather than an unqualified "100% verified".

---

## Erase methods

Offered in this order, following NIST SP 800-88r1: use the drive's own purge
command where it has one, and fall back to a software overwrite only when the
hardware offers nothing.

| Method | Command | Requires |
|---|---|---|
| NVMe Sanitize - crypto erase | `nvme sanitize <ctrl> --sanact=4` | `SANICAP` bit 0 |
| NVMe Sanitize - block erase | `nvme sanitize <ctrl> --sanact=2` | `SANICAP` bit 1 |
| NVMe Sanitize - overwrite | `nvme sanitize <ctrl> --sanact=3` | `SANICAP` bit 2 |
| NVMe Format - cryptographic erase | `nvme format <ns> --ses=2` | `OACS` bit 1 **and** `FNA` bit 2 |
| NVMe Format - user data erase | `nvme format <ns> --ses=1` | `OACS` bit 1 |
| ATA Enhanced Secure Erase | `hdparm --security-erase-enhanced` | ATA security supported, not frozen |
| ATA Secure Erase | `hdparm --security-erase` | ATA security supported, not frozen |
| ATA Sanitize - crypto scramble | `hdparm --sanitize-crypto-scramble` | `CRYPTO_SCRAMBLE_EXT` in the SANITIZE feature set |
| ATA Sanitize - block erase | `hdparm --sanitize-block-erase` | `BLOCK_ERASE_EXT` in the SANITIZE feature set |
| SCSI Sanitize - crypto / block | `sg_sanitize --crypto` / `--block` | drive accepts SANITIZE |
| Whole-device TRIM | `blkdiscard` | TRIM support **and** deterministic read zeros |
| Overwrite - 1 pass zeros | internal writer | any writable device |
| Overwrite - 1 pass pseudorandom | internal writer | any writable device |
| Overwrite - DoD 5220.22-M, 3 pass | internal writer | any writable device |
| Overwrite - DoD 5220.22-M ECE, 7 pass | internal writer | any writable device |
| Overwrite - HMG IS5 Enhanced, 3 pass | internal writer | any writable device |
| Overwrite - German VSITR, 7 pass | internal writer | any writable device |
| Overwrite - Gutmann, 35 pass | internal writer | any writable device |

ATA Sanitize is worth knowing about separately: unlike Secure Erase it is
**not blocked by the BIOS security freeze**, so on a frozen drive whose
controller will not accept a bus reset it is often the only firmware purge
still available. Whole-device TRIM is listed for completeness and labelled
honestly - it is a vendor operation, **not** a NIST SP 800-88 method, and is
only offered when the drive guarantees deterministic zeros after a trim.

### Two NVMe details worth knowing

**Scope.** Sanitize is issued to the *controller* (`/dev/nvme0`) and covers
every namespace. Format is issued to a *namespace* (`/dev/nvme0n1`) and covers
only that one - unless `FNA` bit 0 says otherwise. On a multi-namespace drive
with that bit clear, a single format erases part of the drive while reporting
complete success. Zeroize enumerates the namespaces and formats each in turn,
listing every one separately on the certificate.

**Completion.** A sanitize that has stopped running has not necessarily
succeeded: `SSTAT` 1 means completed, `SSTAT` 3 means failed, and both report
"not in progress". Zeroize requires an explicit success status and records the
controller's Global Data Erased attestation when it is set.

### Overwriting flash

Multi-pass overwrite methods are offered for SSDs and NVMe drives, because some
policies still mandate them - but they carry a caution. Wear levelling and
over-provisioning mean a block-level overwrite cannot reach every cell that once
held data, which is exactly why NIST directs you to the drive's own purge
command instead. Zeroize states this and lets the operator decide.

---

## Installing

### Debian / Ubuntu

```sh
sudo apt install ./zeroize_1.1.0_all.deb
```

### Fedora / RHEL / openSUSE

```sh
sudo dnf install ./zeroize-1.1.0-1.noarch.rpm     # Fedora, RHEL
sudo zypper install ./zeroize-1.1.0-1.noarch.rpm  # openSUSE
```

Dependencies come from the distribution archive; nothing is vendored. Zeroize
needs `nvme-cli`, `hdparm`, `sg3_utils`, `util-linux`, GTK 4, libadwaita,
PyGObject and ReportLab, all of which every target distribution packages.

### Bootable live USB

The live image boots to a desktop with Zeroize installed and a launcher on the
desktop - no installation, and nothing written to the machine's own drives.
See [packaging/iso/README.md](packaging/iso/README.md).

On Windows, write it with **Rufus in DD Image mode** (not the ISO Image mode
Rufus suggests first), then verify it landed:

```powershell
# elevated PowerShell
.\tools\Verify-UsbMedium.ps1 -IsoPath .\build\dist\zeroize-live-bookworm-amd64.iso
```

On Linux:

```sh
sudo dd if=zeroize-live-bookworm-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

Replace `/dev/sdX` with the USB stick, **not** a drive you want to keep.

The image boots under Secure Boot: Debian's shim is dual-signed by both the
Microsoft UEFI CA 2011 and the Microsoft UEFI CA 2023, so it works on firmware
carrying either certificate after the 2026 expiry of the 2011 CA.

> [!IMPORTANT]
> **Non-Windows Secure Boot certificates must be enabled in firmware.**
>
> Microsoft operates two Secure Boot CAs with confusingly similar names. The
> **Windows** UEFI CA signs Windows' own bootloader and nothing else. The
> **Microsoft** UEFI CA - the third-party one - signs Linux shim, and therefore
> this image.
>
> A machine set to trust only the Windows CA will refuse to boot Zeroize with
> Secure Boot enabled. That is not a fault in the image: the same firmware
> would reject Ubuntu, Fedora, Clonezilla or any other Linux live medium.
>
> Enable the third-party certificate in firmware setup. It is off by default on
> a growing number of OEM machines, particularly HP business hardware and
> anything prepared for Device Guard. On HP it is *Security* ->
> *Secure Boot Configuration* -> **Enable MS UEFI CA key**, and it needs a BIOS
> administrator password set first.
>
> Per-vendor steps and the full certificate table are in
> [packaging/iso/README.md](packaging/iso/README.md#if-the-machine-refuses-to-boot-it).

Certificates, logs and diagnostics are written to a FAT32 volume labelled
`ZEROIZE-OUT`, so they survive the live session and can be read on any Windows
machine afterwards.

**`zeroize-live-bookworm-amd64-usb.img` already contains that volume** as its
third partition - nothing to add, nothing to plug in. It is created at 1 GiB
and grown to fill the stick, up to 32 GiB, on first boot. Writing the `.iso`
instead gives you no such partition, and certificates then land on the live
tmpfs and are lost at power off.

Do **not** add the partition by hand with `diskpart`: it rewrites the hybrid
partition table and the stick stops booting. Full details in
[packaging/iso/README.md](packaging/iso/README.md).

---

## Using it

**On the live image** Zeroize starts by itself when the desktop appears. If you
close it, relaunch it from the desktop icon. There is no authentication prompt:
the live session carries a polkit rule for its own autologin account, because a
password prompt that cannot be satisfied would make the appliance unusable.

**On an installed system** launch **Zeroize Drive Wiper** from the applications
menu. It runs as root via `pkexec`, so expect an authentication prompt - the
live image's polkit rule is not shipped in the `.deb`.

1. Review the drive list. Check serial numbers against the physical drives.
2. Tick the drives to erase. Protected drives cannot be ticked.
3. Pick a method per drive, or take the recommendation.
4. Press **Erase selected**. Enter your name - it is printed on the
   certificate, is required, and is not pre-filled - then type `ERASE` to
   confirm.
5. When the run finishes the certificate is written to `Zeroize Certificates/`
   on the `ZEROIZE-OUT` volume and can be opened from the result screen.

### Where the certificate goes

Zeroize looks for **any mounted, writable volume labelled `ZEROIZE-OUT`** - it
is matched on the filesystem label, not on a device path, so it does not care
which disk or which port it arrives on. In order:

1. `certificate.output_directory`, if the configuration sets one explicitly.
2. A volume labelled `ZEROIZE-OUT`. The USB image carries one as its third
   partition, so in normal use this is always satisfied.
3. `~/Zeroize Certificates/` - **on the live image that is a tmpfs, and it is
   gone at the next power cycle.**

The live boot medium itself is never chosen, even if it somehow carries the
label: it is read-only, and failing *after* an erase is much worse than not
selecting it in the first place.

> [!TIP]
> **Use a second USB stick as the certificate destination.** Format a spare
> stick as FAT32 or exFAT, set its volume label to `ZEROIZE-OUT`, and plug it
> in. Zeroize will write certificates, logs and diagnostics to it instead of to
> RAM - so there is nothing to remember to copy off before shutting down, and
> the certificates leave with the stick rather than with the machine.
>
> This is the answer when booting from the `.iso` rather than the `.img`, when
> the boot stick is write-protected, or when certificates should simply live on
> separate media from the tool. If more than one such volume is mounted, the
> first writable one found is used.

A `READ ME FIRST.txt` is placed in the live session's certificates folder for
the case where neither applies, saying to copy them off before shutting down.

### Frozen drives

Most system firmware issues `SECURITY FREEZE LOCK` to every ATA drive during
POST. It is a reasonable thing for a BIOS to do - it stops malware locking or
wiping a disk - but it also blocks the entire ATA security feature set,
including Secure Erase. On a bench tool the default state of the world is
therefore "the good method is unavailable", and the ATA specification provides
no unfreeze command: the state clears only on a power-on reset.

Zeroize tries to *cause* a reset without opening the machine, in two stages:

1. **Detach and rescan**, automatically, during the first scan and before the
   drive list is drawn. It writes to `delete` in sysfs and rescans every SCSI
   host; on many controllers the re-attach performs a `COMRESET` that clears
   the freeze. It takes a few seconds and disturbs nothing visible. Set
   `safety.auto_unfreeze_on_scan` to `false` to skip it.
2. **A three-second suspend**, only if you ask. If the bus reset does not clear
   it, the **Try to unfreeze** button on the drive row offers an S3 cycle,
   after saying plainly that the screen will go blank and the machine will
   appear to be off. S3 removes power from the drive, which clears the freeze
   where a reset will not.

The suspend is never automatic, because S3 also cuts power to the **USB**
controller: on resume the bus re-enumerates, and if the boot stick returns
under a different device node the live filesystem stops responding and the
machine has to be rebooted. A short suspend survives this in practice; a long
one does not. If neither stage works, ATA Sanitize, a whole-device discard and
a software overwrite are all unaffected by the freeze.

A drive may come back under a different name after either stage, so the list is
always rescanned afterwards.

### Exporting diagnostics

The toolbar's export button - and `zeroize diagnostics` - collects everything
needed to investigate a problem somewhere other than the machine it happened
on, and writes it to the `ZEROIZE-OUT` volume:

```
README.txt     what this is, and every drive detected
logs/          the application's own logs
system/        lsblk, blkid, findmnt, dmesg, lspci, lsusb, kernel command line
drives/        per drive: nvme id-ctrl, sanitize-log, smart-log, id-ns, list-ns
               or hdparm -I and smartctl -a
journal/       systemd journal for the boot-time services, and the whole boot
config.json    the configuration actually in force
```

Every command it runs is a read. On the live image `/var/log` is a tmpfs, so
without this the log of a failed run is gone at the next power cycle - which is
exactly when it is worth reading.

### From the command line

```sh
sudo zeroize list          # every drive, its registers, and what it supports
sudo zeroize config        # the merged configuration
sudo zeroize diagnostics   # logs, drive firmware state and system info, for review elsewhere
sudo zeroize unfreeze --device /dev/sda   # clear an ATA security freeze
sudo zeroize --simulate    # the interface against fictitious drives
sudo zeroize --dry-run     # real drives; every destructive command logged and skipped

sudo zeroize erase --device /dev/nvme0n1 --device /dev/nvme1n1 \
                   --method nvme-sanitize-block \
                   --operator "C. White" \
                   --yes-i-am-sure
```

`--yes-i-am-sure` is mandatory. Without it the command refuses to run.

---

## The certificate

One PDF per run. Page 1 carries the organisation block, the machine, the
operator, and a table listing **every drive in the run** with its serial number,
method and result. Each drive then gets a detail section - attributes, disk
information, the full pass list with per-pass outcomes, and the partition layout
it had before erasure - followed by the host's system and hardware details and,
optionally, an appendix of every command issued.

Filenames follow the established shape:

```
Certificate-DD56419883A62-Success-2026-07-30-10-08-22.pdf
Certificate-DD56419883A62_Y0V0A01ATU18-Success-2026-09-26-14-22-05.pdf
```

Past four serials the *filename* collapses to `..._and-8-more`; the document and
the JSON sidecar always list every one.

A `.json` sidecar is written alongside each PDF for ingestion by an asset system.

### Naming the organisation

Zeroize is unbranded to any particular organisation - the certificate says who
performed the erase, and that is site configuration rather than something baked
into the build. Edit `/etc/zeroize/config.json`:

```json
{
  "organisation": {
    "name": "Contoso Asset Disposal",
    "department": "IT Operations",
    "address_lines": ["1200 Industrial Parkway", "Burlington, ON L7L 5H9"],
    "contact_email": "itad@contoso.example",
    "customer": "Northwind Traders",
    "reference": "WO-2026-04417"
  }
}
```

Blank fields are omitted from the certificate rather than printed empty. A
per-operator overlay at `~/.config/zeroize/config.json` takes precedence, and
merging is recursive - setting only `organisation.operator` there keeps every
other field from the site file. Both files are marked as configuration in the
packages, so an upgrade never overwrites them.

---

## Configuration reference

| Section | Key | Default | Meaning |
|---|---|---|---|
| `organisation` | `name`, `department`, `address_lines`, `contact_email`, `contact_phone`, `operator`, `customer`, `reference`, `logo_path` | empty | Printed on the certificate |
| `certificate` | `output_directory` | empty | Forces a location; empty means "the output volume, else home" |
| | `output_volume_label` | `ZEROIZE-OUT` | Label of the volume certificates are written to |
| | `include_command_log` | `true` | Append every command issued |
| | `include_prior_layout` | `true` | Show the pre-erase partition layout |
| | `write_json_sidecar` | `true` | Write the machine-readable twin |
| | `copy_log_beside_certificate` | `true` | Copy the run log next to the PDF |
| | `max_serials_in_filename` | `4` | Before the filename collapses to a count |
| `erase` | `default_method` | `""` (recommend per drive) | Preselected method key |
| | `verification_percent` | `10.0` | Proportion of the device sampled |
| | `max_concurrent_jobs` | `4` | Drives erased at once |
| | `overwrite_block_size` | `4194304` | Write block size, bytes |
| `safety` | `require_typed_confirmation` | `true` | Operator must type the phrase |
| | `confirmation_phrase` | `"ERASE"` | The phrase |
| | `allow_mounted_devices` | `false` | Unmount and proceed, rather than refuse |
| | `require_operator_name` | `true` | A run needs a named operator, typed each time |
| | `auto_unfreeze_on_scan` | `true` | Clear ATA security freezes during the first scan |

`allow_mounted_devices` never applies to the drive carrying the running system.
That interlock cannot be configured off.

---

## Building

```sh
python3 build.py stage   # assemble the install tree under build/stage
python3 build.py deb     # needs dpkg-deb  (Debian/Ubuntu host)
python3 build.py rpm     # needs rpmbuild  (Fedora/RHEL/openSUSE host)
python3 build.py iso     # needs live-build (Debian/Ubuntu host, root)
python3 build.py all     # deb + rpm
```

`stage` works anywhere, including Windows, so the tree can be assembled and
inspected without a Linux host. The ISO builds under **WSL2** (not WSL1) as
long as the scratch tree stays on the Linux filesystem rather than `/mnt/c` -
see [packaging/iso/README.md](packaging/iso/README.md). In containers:

```sh
docker run --rm -v "$PWD":/src -w /src debian:12 \
  sh -c 'apt-get update && apt-get install -y dpkg-dev python3 && python3 build.py deb'

docker run --rm -v "$PWD":/src -w /src fedora:40 \
  sh -c 'dnf install -y rpm-build python3 && python3 build.py rpm'
```

The application installs as a plain package tree at `/usr/lib/zeroize` with a
launcher at `/usr/bin/zeroize`, rather than into a Python site-packages
directory - those differ across the four target distributions and, on Fedora and
RHEL, embed the Python minor version. The `.deb` and the `.rpm` install
byte-identical trees.

The logo and every icon are generated at build time from
[`zeroize/branding.py`](zeroize/branding.py), so there is exactly one definition
of the mark and no binary asset that can drift out of step with it.

### Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

python -m pytest                          # 121 tests, no hardware needed
python -m ruff check zeroize/ tools/
python tools/make_sample_certificate.py   # render a sample for layout review
sudo python -m zeroize --simulate         # the interface, against fake drives
```

`--simulate` covers the awkward capability combinations on purpose: a drive with
Format NVM but no Sanitize, one with Sanitize but no Format, one with neither, a
BIOS-frozen SATA disk, a spinning disk, and a protected system disk.

---

## Safety model

Five interlocks, in the order they apply:

1. **Discovery** marks any drive carrying root, `/boot`, `/usr`, `/var`, active
   swap or the live medium. Such a drive is shown but can never be ticked.
2. **The confirmation dialog** lists each drive's path, product, serial and
   method, and requires the operator to type a phrase. A second "Are you sure?"
   button teaches people to click twice without reading; typing does not.
3. **The engine re-checks everything** immediately before issuing any command -
   the protection flag, and that the method is one the drive said it supports.
   The scan could be minutes old and a drive could have been hot-plugged since.
4. **The kernel** refuses an exclusive open of a mounted device, which stops a
   software overwrite independently of anything above.
5. **Every command is logged before it runs**, with its full argument vector, to
   `/var/log/zeroize/`, in CMTrace format, one timestamped file per run.

Cancellation is honest about its limits. A software overwrite stops between
blocks. Firmware commands - Sanitize, Secure Erase - are not interruptible by
design: the drive is unusable until they complete, so "cancel" there means "stop
waiting and report", never "stop erasing". The interface says so.

### If an ATA Secure Erase is interrupted

ATA Secure Erase arms the drive with a password before erasing. If the erase is
interrupted between those two steps, the drive is left locked. The password is a
fixed, documented constant for exactly this reason, and is written to the log at
every step:

```sh
hdparm --user-master u --security-disable Zeroize /dev/sdX
```

---

## Requirements

- Linux, x86-64. Debian 12+, Ubuntu 22.04+, Fedora 38+, RHEL 9+, openSUSE Leap 15.5+.
- Python 3.11 or newer.
- GTK 4.8+, libadwaita 1.2+, PyGObject - for the interface. The CLI needs none of them.
- `nvme-cli`, `hdparm`, `sg3_utils`, `util-linux` (`lsblk`, `blkid`, `partx`,
  `blockdev`, `blkdiscard`), and `udev` for `udevadm`. `dmidecode`,
  `smartmontools` and `parted` are recommended.
- Root. The desktop launcher uses `pkexec`.

### Known limitations

- **USB-to-NVMe enclosures** frequently do not pass the NVMe admin command set
  through, so no NVMe method is offered for a drive behind one. Connect it
  directly, or use a software overwrite.
- **eMMC and SD media** are enumerated and can be overwritten, but have no
  firmware purge command to offer.
- **Hardware RAID volumes** are erased as presented by the controller. Zeroize
  cannot reach the member drives behind it.

---

## Layout

```
zeroize/
  branding.py          the palette and the mark, as vector geometry
  config.py            layered configuration, including the organisation block
  models.py            the data model shared by every layer
  process.py           the single choke point for external commands
  diagnostics.py       one-shot collection of logs, drive state and system info
  discovery/           lsblk, nvme-cli, hdparm, DMI - and the protection flag
  erase/               the method catalogue, the gating rules, the engine
  certificate/         the PDF and its JSON sidecar
  ui/                  GTK 4 / libadwaita
packaging/
  iso/                 live-build wrapper, GRUB theme, Plymouth theme, hooks
  share/               .desktop, polkit policy, AppStream metainfo, launcher
build.py               the single build entrypoint
```

The discovery, erase and certificate layers never import the interface, so the
CLI works on a machine with no desktop stack installed - which is what makes
`zeroize list` usable over SSH.
