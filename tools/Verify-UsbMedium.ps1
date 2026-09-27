<#
.SYNOPSIS
    Verify that a USB stick written in DD mode byte-for-byte matches the ISO.

.DESCRIPTION
    Rufus reports success when the write call returns. That is not the same as
    the bytes being on the stick: Windows may still be holding most of the
    write in cache, and pulling the device without ejecting it loses the lot.
    The failure is silent and looks like a broken image - the stick boots,
    loads its kernel, and then dies mounting a filesystem whose contents are
    wrong.

    This reads the physical drive back and compares it against the ISO. It is
    the only check that proves the medium holds what you built.

    The boot sector is compared separately from the payload, because adding a
    certificates partition with diskpart rewrites the MBR at sector 0. That
    makes the whole-image hash differ while the image data is perfectly
    intact, so the two are reported apart rather than as one pass/fail.

    For Zeroize specifically this is worth more than convenience: writing in DD
    mode is what lets the wiping medium be proven unaltered, which matters when
    the certificates it produces are evidence.

    Must run elevated - reading a raw physical drive requires administrator
    rights.

.PARAMETER IsoPath
    The .iso that was written to the stick.

.PARAMETER DriveNumber
    Physical drive number of the stick, as shown by Get-Disk. Omit to have the
    script list the removable drives and ask.

.EXAMPLE
    .\Verify-UsbMedium.ps1 -IsoPath .\build\dist\zeroize-live-bookworm-amd64.iso

.EXAMPLE
    .\Verify-UsbMedium.ps1 -IsoPath .\zeroize.iso -DriveNumber 2
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$IsoPath,

    [Parameter(Mandatory = $false)]
    [int]$DriveNumber = -1
)

$ErrorActionPreference = "Stop"

$SectorSize = 512

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function ConvertTo-HexString {
    param([byte[]]$Bytes)
    return (($Bytes | ForEach-Object { $_.ToString("x2") }) -join "").ToUpperInvariant()
}

if (-not (Test-Elevated)) {
    throw "This must run in an elevated PowerShell - reading a raw disk needs administrator rights."
}

$iso = Get-Item -LiteralPath $IsoPath
$isoLength = $iso.Length
Write-Host "ISO   : $($iso.FullName)"
Write-Host "Size  : $("{0:N0}" -f $isoLength) bytes"

# --- Pick the drive -------------------------------------------------------
if ($DriveNumber -lt 0) {
    Write-Host ""
    Write-Host "Removable and USB disks:" -ForegroundColor Cyan
    $candidates = Get-Disk | Where-Object { $_.BusType -eq "USB" -or $_.IsRemovable }
    if (-not $candidates) {
        throw "No USB or removable disks found. Pass -DriveNumber explicitly."
    }
    $candidates |
        Select-Object Number,
                      FriendlyName,
                      @{ n = "SizeGB"; e = { [math]::Round($_.Size / 1GB, 1) } },
                      BusType |
        Format-Table -AutoSize

    $answer = Read-Host "Which disk number is the stick? (Ctrl+C to abort)"
    $DriveNumber = [int]$answer
}

$disk = Get-Disk -Number $DriveNumber
Write-Host ""
Write-Host "Drive : PhysicalDrive$DriveNumber - $($disk.FriendlyName) ($([math]::Round($disk.Size / 1GB, 1)) GB, $($disk.BusType))"

if ($disk.Size -lt $isoLength) {
    throw "Disk $DriveNumber is smaller ($($disk.Size) bytes) than the ISO ($isoLength bytes)."
}

# --- Work out where the EFI partition lives -------------------------------
# Differences inside it are expected and harmless: Windows creates a
# System Volume Information folder on any FAT volume it mounts, which allocates
# clusters and writes directory entries. That is a few hundred bytes of
# bookkeeping, not a bad write, and reporting it as a failure would train
# people to ignore this tool.
$efiStart = -1L
$efiEnd = -1L
$mbr = New-Object byte[] $SectorSize
$headStream = [System.IO.File]::OpenRead($iso.FullName)
try { [void]$headStream.Read($mbr, 0, $SectorSize) } finally { $headStream.Dispose() }

for ($entry = 0; $entry -lt 4; $entry++) {
    $base = 0x1BE + ($entry * 16)
    if ($mbr[$base + 4] -ne 0xEF) { continue }
    $startLba = [BitConverter]::ToUInt32($mbr, $base + 8)
    $sectors = [BitConverter]::ToUInt32($mbr, $base + 12)
    if ($sectors -eq 0) { continue }
    $efiStart = [long]$startLba * $SectorSize
    $efiEnd = ([long]$startLba + $sectors) * $SectorSize
    break
}

# --- Compare block by block -----------------------------------------------
# A pass/fail hash says the stick is wrong but not *how*, and the difference
# between "nothing landed past 512 MB" and "a few bytes of FAT housekeeping"
# points at completely different causes. So the two are compared directly and
# every difference is attributed to a region.
Write-Host ""
Write-Host "Comparing the stick against the ISO (a minute or two)..." -ForegroundColor Cyan

$blockSize = 1MB
$isoBuffer = New-Object byte[] $blockSize
$mediumBuffer = New-Object byte[] $blockSize

$isoStream = $null
$stream = $null

$firstDifference = -1L
$differingBytes = 0L
$bootSectorDiffers = $false
$offset = 0L
$lastPercent = -1

try {
    $isoStream = [System.IO.File]::OpenRead($iso.FullName)
    $stream = New-Object System.IO.FileStream(
        "\\.\PhysicalDrive$DriveNumber",
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::ReadWrite)

    while ($offset -lt $isoLength) {
        $remaining = $isoLength - $offset
        $want = [int][Math]::Min([long]$blockSize, $remaining)
        # Raw device reads must be whole sectors; read aligned, compare exact.
        $aligned = [int]((([long]$want + $SectorSize - 1) / $SectorSize) * $SectorSize)
        if ($aligned -gt $blockSize) { $aligned = $blockSize }

        $isoGot = 0
        while ($isoGot -lt $want) {
            $chunk = $isoStream.Read($isoBuffer, $isoGot, $want - $isoGot)
            if ($chunk -le 0) { break }
            $isoGot += $chunk
        }

        $mediumGot = $stream.Read($mediumBuffer, 0, $aligned)
        if ($mediumGot -le 0) {
            Write-Host ""
            Write-Host "The drive stopped returning data at offset $offset." -ForegroundColor Red
            $firstDifference = $offset
            $differingBytes += $remaining
            break
        }

        $compare = [int][Math]::Min([int]$isoGot, $mediumGot)
        for ($index = 0; $index -lt $compare; $index++) {
            if ($isoBuffer[$index] -ne $mediumBuffer[$index]) {
                $position = $offset + $index
                if ($position -lt $SectorSize) {
                    $bootSectorDiffers = $true
                }
                elseif ($firstDifference -lt 0) {
                    $firstDifference = $position
                }
                $differingBytes++
            }
        }

        $offset += $compare

        $percent = [int](($offset * 100) / $isoLength)
        if ($percent -ne $lastPercent -and ($percent % 5) -eq 0) {
            Write-Progress -Activity "Comparing PhysicalDrive$DriveNumber" -PercentComplete $percent
            $lastPercent = $percent
        }
    }
}
finally {
    Write-Progress -Activity "Comparing" -Completed
    if ($isoStream) { $isoStream.Dispose() }
    if ($stream) { $stream.Dispose() }
}

# --- Verdict --------------------------------------------------------------
Write-Host ""
if ($firstDifference -lt 0 -and -not $bootSectorDiffers) {
    Write-Host "MATCH - the stick is a byte-for-byte copy of the ISO." -ForegroundColor Green
    Write-Host "Safe to add the ZEROIZE-OUT partition now, then eject." -ForegroundColor Green
    exit 0
}

if ($firstDifference -lt 0 -and $bootSectorDiffers) {
    Write-Host "IMAGE DATA MATCHES - only the boot sector differs." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "That is expected if you already added the ZEROIZE-OUT partition:"
    Write-Host "diskpart rewrites the partition table in sector 0. Everything the"
    Write-Host "system actually boots from is intact, so this stick is good."
    exit 0
}

$mb = [math]::Round($firstDifference / 1MB, 1)
Write-Host "MISMATCH - the stick does not match this ISO." -ForegroundColor Red
Write-Host ""
Write-Host ("  first difference at : {0:N0} bytes ({1} MB in)" -f $firstDifference, $mb)
Write-Host ("  bytes differing     : {0:N0} of {1:N0}" -f $differingBytes, $isoLength)
Write-Host ""

if ($firstDifference -gt ($isoLength * 0.98)) {
    Write-Host "Only the tail differs - most likely a flush that never completed." -ForegroundColor Yellow
    Write-Host "Rewrite and eject the stick safely before removing it."
}
elseif ($differingBytes -gt ($isoLength * 0.5)) {
    Write-Host "Most of the image is absent. Either the write stopped early, or" -ForegroundColor Yellow
    Write-Host "Rufus was left in ISO Image mode rather than DD Image mode."
}
else {
    Write-Host "The image starts correctly and diverges partway through." -ForegroundColor Yellow
    Write-Host "If that offset is a round number, the write was truncated there."
    Write-Host ""
    Write-Host "BUT FIRST: confirm you wrote THIS ISO. Rebuilds can produce a file"
    Write-Host "of identical size and a different content, so a stale copy looks"
    Write-Host "exactly like a corrupt write. Check the ISO against its own hash:"
    Write-Host ""
    Write-Host "  Get-FileHash '$($iso.FullName)' -Algorithm SHA256"
    Write-Host "  Get-Content '$($iso.FullName).sha256'"
}
exit 1
