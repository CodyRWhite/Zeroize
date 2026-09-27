# The Zeroize live image

A bootable USB that starts a desktop with Zeroize already installed and a
launcher on the desktop. Nothing is written to the machine's own drives, which
is the point: the tool that erases a drive should not need the drive it is
erasing.

## Building

Requires a Debian or Ubuntu host (or container) with `live-build` and `xorriso`,
root privileges, and network access to the Debian archive. It takes 15-40
minutes and downloads around 1.5 GB.

```sh
python3 build.py iso
```

That builds the `.deb` first and hands it to the ISO build. To run the ISO stage
alone against an existing package:

```sh
sudo packaging/iso/build-iso.sh build/dist/zeroize_1.0.0_all.deb build/dist
```

In a container:

```sh
docker run --rm --privileged -v "$PWD":/src -w /src debian:12 \
  bash -c 'apt-get update && apt-get install -y live-build xorriso && \
           packaging/iso/build-iso.sh build/dist/zeroize_1.0.0_all.deb build/dist'
```

`--privileged` is needed because `live-build` mounts `/proc` and loop devices
inside the chroot it assembles.

### From Windows, using WSL

WSL2 builds the image fine - it runs a real Linux kernel with loop devices,
mount namespaces and working chroot semantics. **WSL1 cannot**: it has no
kernel, so `debootstrap` cannot create device nodes and the chroot stage fails.
Check with `wsl -l -v` and convert if needed:

```powershell
wsl --install -d Debian          # if WSL is not installed at all
wsl --set-version Debian 2       # if the distribution is on version 1
```

Then, inside the distribution:

```sh
sudo apt update
sudo apt install -y live-build xorriso dpkg-dev python3

# Build the .deb first. The repo can stay on the Windows drive for this part.
cd "/mnt/d/DevOps/Application Tools/src/Zeroize"
python3 build.py deb

# Build the ISO. The scratch tree must be on the Linux filesystem.
sudo WORK_DIR=/var/tmp/zeroize-iso packaging/iso/build-iso.sh \
    build/dist/zeroize_1.0.0_all.deb build/dist
```

**The one trap:** the build tree must never live on `/mnt/c`, `/mnt/d` or any
other Windows drive. DrvFs cannot represent the ownership, setuid bits,
symlinks and device nodes `debootstrap` needs, and the chroot stage fails
partway through with confusing permission errors - thirty minutes into a build
that looked like it was working. The script's default `WORK_DIR` is already
under `/tmp`, which is correct; it now refuses a `WORK_DIR` under `/mnt/` and
warns if the Linux filesystem has under 15 GB free.

Reading the `.deb` from `/mnt/d` and writing the finished ISO back to `/mnt/d`
are both fine - those are plain file copies, not a chroot.

Two other things worth knowing about WSL here:

- The scratch space comes out of the distribution's virtual disk, which grows
  on demand but does not shrink. After a few builds, reclaim it with
  `wsl --manage <distro> --resize` or by compacting the VHDX.
- Docker Desktop with the WSL2 backend works equally well and keeps the mess
  inside a container - use the `docker run --privileged` line above from a
  PowerShell prompt, which needs no separate distribution set up.

Environment overrides:

| Variable | Default | Notes |
|---|---|---|
| `DISTRIBUTION` | `bookworm` | The base release |
| `ARCHITECTURE` | `amd64` | |
| `WORK_DIR` | `/var/tmp/zeroize-iso-XXXXXX` | Where the chroot is assembled. Not `/tmp`: that is a tmpfs on WSL and on systemd hosts, and 10-15 GB of chroot does not belong in RAM. |

`bookworm` is the default rather than something newer because it is the oldest
release Zeroize supports, so an image built on it runs on the widest range of
bench hardware.

## Writing it to a USB stick

The image is a hybrid ISO - an ISO 9660 filesystem with a real MBR and an EFI
system partition inside it - so a byte-for-byte copy to the stick is all that
is needed for both BIOS and UEFI:

```
Disklabel type: dos
Device     Boot Start     End Sectors  Size Id Type
image1     *       64 1941503 1941440  948M  0 Empty              <- the ISO 9660 filesystem
image2            540   10203    9664  4.7M ef EFI (FAT-12/16/32) <- the ESP holding shim + grub
```

### On Windows: Rufus, in DD Image mode

**Yes, Rufus works.** It is the recommended tool on Windows. One choice
matters:

1. Open Rufus, choose the stick under **Device**.
2. **SELECT** `zeroize-live-bookworm-amd64-usb.img` - the `.img`, not the
   `.iso`. It is the same bootable image with the `ZEROIZE-OUT` certificate
   partition already appended.
3. Leave everything else alone and press **START**.
4. Rufus detects the hybrid image and asks how to write it. Choose
   **"Write in DD Image mode"**, *not* the ISO Image mode it suggests first.
5. Accept the "all data will be destroyed" warning.

**Why DD mode, against Rufus's own recommendation.** Rufus prefers ISO mode
because it leaves the stick writable and usually boots fine. For this tool the
trade goes the other way:

- **DD mode is bit-identical to the image you built.** You can hash the stick
  and match it against the `.sha256` beside the ISO. For a tool whose whole
  output is an attestation that data was destroyed, being able to prove the
  wiping medium was not altered is worth more than a writable stick.
- **It leaves the signed boot chain exactly as built.** ISO mode rewrites the
  bootloader with Rufus's own, which is a different chain from the
  Microsoft-signed shim described under [Secure Boot](#secure-boot).
- **There is nothing to gain from letting Rufus manage the layout.** The
  writable certificate partition is already in the `.img`; Rufus does not need
  to create anything.

**After writing, Windows may offer to format part of the stick. Say no.** The
boot portion is an ISO 9660 filesystem, which Windows cannot read, so it
reports it as unformatted. That is expected. The `ZEROIZE-OUT` partition *will*
mount normally and get a drive letter - that is the one certificates land on.

### Verify the stick. Every time.

**Do not skip this.** Rufus reports success when the write call returns, which
is not the same as the bytes being on the stick. A silently bad write produces
a stick that boots, loads its kernel, and then dies mounting a filesystem whose
contents are wrong - a failure that looks like a broken image and is not.

In an **elevated** PowerShell:

```powershell
.\tools\Verify-UsbMedium.ps1 -IsoPath .\build\dist\zeroize-live-bookworm-amd64.iso
```

It lists the removable drives, reads back exactly as many bytes as the ISO
contains, and compares SHA-256. `MATCH` means the medium is a byte-for-byte
copy; anything else means rewrite it.

That check is also what makes the DD-mode argument real: a stick that hashes
equal to the published image is a wiping medium you can demonstrate was not
altered, which matters when the certificates it produces are evidence.

To check only that the downloaded ISO is intact:

```powershell
Get-FileHash .\zeroize-live-bookworm-amd64.iso -Algorithm SHA256
Get-Content .\zeroize-live-bookworm-amd64.iso.sha256
```

### Do not use an SSD in a USB enclosure

Write the image to a **plain USB flash drive**. USB-to-SATA and USB-to-NVMe
bridges - JMicron, ASMedia, Realtek - are a recurring source of two failures
that both present as a corrupt image:

* They cache writes and report flushes they have not performed, so the write
  completes and the data is not there. Such a bridge typically reports
  `doesn't support DPO or FUA` in the kernel log.
* Several corrupt *reads* under sustained load through the UAS driver. Small
  reads succeed - the ISO metadata, the kernel, the initrd - and then the first
  large sequential read of the ~850 MB squashfs comes back wrong. The symptom
  is `Can't find a SQUASHFS superblock on loop0` and a drop to an
  `(initramfs)` prompt.

If you must use one, verify with the script above before booting, and if reads
are the problem add `usb-storage.quirks=VVVV:PPPP:u` to the kernel command line
to disable UAS for that bridge.

The same bridges rarely pass the NVMe admin command set through, so a drive
attached that way will only offer the software overwrite methods anyway -
never `nvme format` or `nvme sanitize`.

### Other Windows tools

| Tool | Verdict |
|---|---|
| **Rufus, DD Image mode** | **Recommended.** Exact copy, signed chain intact, verifiable. |
| Rufus, ISO Image mode | Works, but rewrites the bootloader and the stick no longer matches the published hash. Use only if DD mode fails to boot on a specific machine. |
| balenaEtcher | Fine. It only does DD-style writes, so there is no mode to get wrong. No control over the second partition. |
| **Ventoy** | **Avoid for this image.** Ventoy inserts its own bootloader ahead of shim, so Secure Boot requires enrolling Ventoy's key through MOK on every machine - and the chain that boots is no longer the Debian-signed one this image was verified as. |
| Windows built-in tools | No. `diskpart` and File Explorer cannot write a hybrid ISO; copying the files out does not produce a bootable stick. |

### The certificates partition is already in the image

Write **`zeroize-live-bookworm-amd64-usb.img`**, not the `.iso`. It is the ISO
byte-for-byte with a FAT32 partition appended and registered as MBR entry 3,
labelled `ZEROIZE-OUT`. One write gives you a bootable stick *and* the volume
certificates and logs are saved to, which Windows mounts as an ordinary drive
when you plug it back in.

**Do not add a partition with `diskpart`.** A hybrid ISO has a deliberately odd
partition table - a type `0x00` entry, and the EFI partition nested inside its
sector range, alongside a GPT. Windows does not understand that layout and
rewrites it when adding a partition, which breaks the boot. This has been
tested and it does break it. The same applies to resizing from Windows.

Size it at build time to suit your sticks:

```sh
CERT_PARTITION_MB=8192 python3 build.py iso     # 8 GB certificate partition
CERT_PARTITION_MB=0    python3 build.py iso     # plain ISO, no USB image
```

The default is 1024 MB, which holds on the order of 65,000 certificates with
their logs.

To grow it after the fact, do it **from Linux** - from the Zeroize live session
itself, or any other Linux machine. `parted` and `fatresize` are on the image
for exactly this, and unlike Windows they leave the partition entries they were
not asked about alone:

```sh
parted /dev/sdX resizepart 3 100%
fatresize -s max /dev/sdX3
```

### Adding a certificates partition by hand (not recommended)

Only if you are writing the plain `.iso` rather than the `-usb.img`, and only
from Linux:

```sh
# sdX is the stick. Entry 3 is free on a freshly written image.
printf 'n
p
3


t
3
c
w
' | fdisk /dev/sdX
mkfs.vfat -F 32 -n ZEROIZE-OUT /dev/sdX3
```

**Never do this from Windows.** `diskpart` and Disk Management rewrite the
hybrid partition table and the stick stops booting.

If you would rather not repartition at all, a plain second USB stick labelled
`ZEROIZE-OUT` works identically - plug it in alongside the boot stick and
Zeroize finds it.

### On Linux

```sh
sudo dd if=build/dist/zeroize-live-bookworm-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

Replace `/dev/sdX` with the stick. Check it twice with `lsblk` first - `dd`
will overwrite whatever you name, including the drive you are working from.

## What is on it

- XFCE, autologin as `zeroize`, no password. XFCE rather than GNOME because it
  boots faster on the elderly hardware these images usually run on, and because
  a GNOME live session pulls in an installer, a software centre and an
  online-accounts stack that have no business on a wipe appliance.
- Zeroize, plus `nvme-cli`, `hdparm`, `sg3-utils`, `smartmontools`, `dmidecode`,
  `gdisk` and `parted`.
- A desktop launcher and a menu entry.
- A polkit rule allowing the autologin operator to run Zeroize without an
  authentication prompt. **This exists only inside the ISO** - the `.deb` and
  `.rpm` never ship it, so an ordinary installation still requires
  authentication. On the live image the account has no password, so a prompt
  could not be satisfied and the appliance would be unusable.

## Secure Boot

> [!IMPORTANT]
> **Non-Windows Secure Boot certificates must be enabled in firmware for this
> image to boot with Secure Boot on.** The third-party *Microsoft UEFI CA* is
> what signs Linux shim; the similarly named *Windows UEFI CA* signs only
> Windows. See [If the machine refuses to boot
> it](#if-the-machine-refuses-to-boot-it) below for the distinction and the
> per-vendor steps.

The image boots with Secure Boot enabled. The chain is the standard Debian one:

```
firmware db  ->  shim (bootx64.efi)  ->  grub (grubx64.efi)  ->  vmlinuz
                 Microsoft-signed        Debian-signed          Debian-signed
```

**The 2026 certificate transition is covered.** The Microsoft Corporation UEFI
CA 2011 - the third-party CA that has signed shims for over a decade - expired
in June 2026, and is being replaced by the Microsoft UEFI CA 2023. Debian's
shim carries **both** signatures, so one image boots on firmware that trusts
either certificate:

| Firmware db contains | Result |
|---|---|
| 2011 CA only (most existing machines) | Boots, via the 2011 signature |
| Both, after the Windows Update rollout | Boots |
| 2023 CA only (hardware shipping from 2026) | Boots, via the 2023 signature |

Verify it yourself on any built image:

```sh
xorriso -osirrox on -indev zeroize-live-bookworm-amd64.iso \
        -extract /EFI/boot/bootx64.efi /tmp/bootx64.efi
sbverify --list /tmp/bootx64.efi | grep "CN="
```

You should see both `Microsoft Corporation UEFI CA 2011` and
`Microsoft UEFI CA 2023`. The build runs this check itself and warns loudly if
either signature is missing, because a bootloader package that failed to
install still produces a bootable-*looking* ISO that a Secure Boot machine
silently refuses.

The build passes `--uefi-secure-boot enable` rather than leaving live-build on
its `auto` default, so a missing signed bootloader fails the build instead of
the boot.

### If the machine refuses to boot it

Almost always the same cause: **the firmware trusts the Windows CA but not the
third-party one.** Microsoft operates two separate Secure Boot CAs, and the
names are close enough to be actively misleading:

| Certificate in firmware setup | What it signs |
|---|---|
| **Windows** UEFI CA 2023 (was: Microsoft Windows Production PCA 2011) | Windows' own bootloader, and nothing else |
| **Microsoft** UEFI CA 2023 (was: Microsoft Corporation UEFI CA 2011) | Third-party bootloaders - Linux shim, so **this image** |
| Microsoft Option ROM UEFI CA 2023 | PCIe option ROMs: GPUs, RAID controllers |

Zeroize's shim is signed by the **third-party** CA, under both its 2011 and
2023 names. A machine with only the Windows CA enabled rejects it - and would
equally reject Ubuntu, Fedora, Clonezilla or any other Linux live medium. It is
not a fault in the image.

Enabling the third-party CA is off by default on a growing number of OEM
machines, particularly HP business hardware and anything prepared for Device
Guard.

**On HP** (F10 at power-on, *Security* -> *Secure Boot Configuration*):

1. Tick **Microsoft UEFI CA 2023**.
2. Optionally also tick **Enable MS UEFI CA key**, the legacy 2011 third-party
   CA. The shim carries both signatures, so either one alone is sufficient.
3. Leave **Windows UEFI CA 2023** ticked, or the machine will not boot Windows.
4. F10 to save, then reboot and retry.

If those checkboxes will not toggle, set a **BIOS administrator password**
first - HP gates Secure Boot key management behind one. The warning about
*Sure Start Secure Boot Keys Protection* applies to the Import / Clear / Reset
options above the CA list, not to the CA checkboxes themselves.

Dell calls the same thing *Allow Microsoft UEFI CA* or *UEFI CA (3rd party)*
under Boot Configuration; Lenovo ships it as *Allow Microsoft 3rd Party UEFI
CA* under Security -> Secure Boot.

**If site policy forbids enabling it**, in order of preference:

1. **Turn Secure Boot off for the wipe.** The machine is being decommissioned
   and its drive is about to be destroyed; re-enable it afterwards if the
   machine is being redeployed.
2. Wipe on a dedicated bench machine that permits the third-party CA.
3. Pull the drive and erase it from the bench machine. Note that USB-to-NVMe
   bridges rarely pass the NVMe admin command set through, so an enclosure
   usually leaves only the software overwrite methods available.

Enrolling a key of our own through MOK is **not** a workaround: MOK sits below
shim in the chain and cannot authorise shim itself, which is what the firmware
is refusing.

Once it does boot, confirm the machine really was enforcing:

```sh
mokutil --sb-state      # "SecureBoot enabled"
```

### Caveats

- **This is Debian's shim, not a Zeroize-signed one.** Nothing here is signed
  by a key you control, so `mokutil` enrolment is not required and not used.
- **Only the boot chain is verified, not the application.** Secure Boot
  attests that the kernel Debian shipped is the kernel that booted. Zeroize
  itself is a Python package on the live filesystem and is covered by the
  squashfs, not by a signature - the ISO's SHA-256 is what attests to that.
- **Custom or locked-down firmware may still refuse it.** Machines with only
  an OEM key in db, or with the Microsoft third-party CA removed by policy,
  reject every Linux live image including this one. `mokutil --sb-state` is
  installed in the live session to help diagnose that.

## Where certificates go

The live filesystem is a tmpfs, and the boot medium is a read-only ISO 9660
image. Anything written to either is gone at power off - which, for the one
artefact the tool exists to produce, is not acceptable.

So Zeroize looks for a mounted, writable volume labelled **`ZEROIZE-OUT`** and
writes certificates to `Zeroize Certificates\` on it. That is either a second
partition on the boot stick or a separate stick; see
[Adding a certificates partition](#adding-a-certificates-partition).

Resolution order, first match wins:

1. `certificate.output_directory`, if set in `/etc/zeroize/config.json`.
2. A mounted writable volume labelled `certificate.output_volume_label`
   (default `ZEROIZE-OUT`).
3. The invoking user's home directory - in the live session, RAM.

The USB image carries that volume as its third partition, so in normal use it
is always present. If it is missing - booted from the `.iso`, or the partition
damaged - certificates still get written, just to RAM. The result screen always shows the full path, and there is a
`READ ME FIRST.txt` in the live session's certificates folder saying to copy
them off before shutting down.

### Baking in site configuration

For a permanent appliance, add the organisation block and any settings to
`config/includes.chroot/etc/zeroize/config.json` before building - then every
certificate the image produces is correctly attributed without anyone having to
remember:

```json
{
  "organisation": {
    "name": "Contoso Asset Disposal",
    "department": "IT Operations",
    "contact_email": "itad@contoso.example"
  },
  "certificate": { "output_volume_label": "ZEROIZE-OUT" }
}
```

Editing `/etc/zeroize/config.json` from inside a running live session works but
does not survive a reboot.

## The boot medium protects itself

Zeroize detects the live medium it booted from - it is mounted at
`/run/live/medium` - and refuses to list it as erasable. The check is based on
what is mounted rather than on device naming, which matters here: the boot stick
is frequently `/dev/sda` while the drive to be wiped is `/dev/nvme0n1`.

## Boot parameters

The image boots with `boot=live components quiet splash noeject username=zeroize
hostname=zeroize`.

`noeject` is deliberate: a wipe appliance is routinely shut down with the USB
still in it, and the default prompt to remove the medium blocks unattended
teardown. `nomodeset` is deliberately *not* set, because it breaks the
compositor on most modern hardware and the desktop is the whole point.
