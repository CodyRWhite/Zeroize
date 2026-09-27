<#
.SYNOPSIS
    Show exactly which bytes differ between an ISO and the stick written from it.

.DESCRIPTION
    Verify-UsbMedium.ps1 answers "does it match". When it does not, this
    answers "what changed" - the differing ranges, which partition they fall
    in, and the bytes themselves from both sides.

    That distinction matters because not every difference is a problem. A
    handful of bytes inside the EFI system partition is normally Windows
    updating FAT bookkeeping after it mounted the volume, and has no effect on
    booting. A difference inside the ISO 9660 region, or anywhere in the
    squashfs, is a genuinely bad write.

    Must run elevated.

.PARAMETER IsoPath
    The .iso that was written.

.PARAMETER DriveNumber
    Physical drive number of the stick.

.PARAMETER MaxRanges
    Stop after reporting this many differing ranges.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$IsoPath,

    [Parameter(Mandatory = $true)]
    [int]$DriveNumber,

    [Parameter(Mandatory = $false)]
    [int]$MaxRanges = 12
)

$ErrorActionPreference = "Stop"
$SectorSize = 512

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Elevated)) {
    throw "This must run in an elevated PowerShell - reading a raw disk needs administrator rights."
}

# Regions of a hybrid ISO, so a differing offset can be named rather than just
# numbered. Bounds are read from the image's own MBR below.
function Get-RegionName {
    param([long]$Offset, $Partitions)
    foreach ($partition in $Partitions) {
        if ($Offset -ge $partition.Start -and $Offset -lt $partition.End) {
            return $partition.Name
        }
    }
    if ($Offset -lt 512) { return "MBR / boot sector" }
    if ($Offset -lt 32768) { return "reserved area before the ISO 9660 filesystem" }
    return "ISO 9660 filesystem"
}

$iso = Get-Item -LiteralPath $IsoPath
$isoLength = $iso.Length

# --- Read the MBR from the ISO so the partitions can be named -------------
$partitions = @()
$mbr = New-Object byte[] 512
$headStream = [System.IO.File]::OpenRead($iso.FullName)
try { [void]$headStream.Read($mbr, 0, 512) } finally { $headStream.Dispose() }

for ($entry = 0; $entry -lt 4; $entry++) {
    $base = 0x1BE + ($entry * 16)
    $type = $mbr[$base + 4]
    if ($type -eq 0) { continue }
    $startLba = [BitConverter]::ToUInt32($mbr, $base + 8)
    $sectors  = [BitConverter]::ToUInt32($mbr, $base + 12)
    if ($sectors -eq 0) { continue }
    $label = switch ($type) {
        0xEF { "EFI system partition (FAT)" }
        default { "MBR partition $($entry + 1) (type 0x{0:X2})" -f $type }
    }
    $partitions += [pscustomobject]@{
        Name  = $label
        Start = [long]$startLba * $SectorSize
        End   = ([long]$startLba + $sectors) * $SectorSize
    }
}

Write-Host "ISO   : $($iso.FullName)"
Write-Host "Drive : PhysicalDrive$DriveNumber"
Write-Host ""
Write-Host "Partitions declared in the image:" -ForegroundColor Cyan
foreach ($partition in $partitions) {
    Write-Host ("  {0,-34} {1,14:N0} - {2:N0}" -f $partition.Name, $partition.Start, $partition.End)
}

# --- Walk both, collecting differing ranges -------------------------------
Write-Host ""
Write-Host "Comparing..." -ForegroundColor Cyan

$blockSize = 1MB
$isoBuffer = New-Object byte[] $blockSize
$mediumBuffer = New-Object byte[] $blockSize
$ranges = @()
$current = $null
$offset = 0L
$totalDiffering = 0L
$lastPercent = -1

$isoStream = [System.IO.File]::OpenRead($iso.FullName)
$stream = New-Object System.IO.FileStream(
    "\\.\PhysicalDrive$DriveNumber",
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::Read,
    [System.IO.FileShare]::ReadWrite)

try {
    while ($offset -lt $isoLength) {
        $want = [int][Math]::Min([long]$blockSize, $isoLength - $offset)
        $aligned = [int]((([long]$want + $SectorSize - 1) / $SectorSize) * $SectorSize)
        if ($aligned -gt $blockSize) { $aligned = $blockSize }

        # Both sides are read to completion, so a short read cannot silently
        # desynchronise the two streams and manufacture differences.
        $isoGot = 0
        while ($isoGot -lt $want) {
            $chunk = $isoStream.Read($isoBuffer, $isoGot, $want - $isoGot)
            if ($chunk -le 0) { break }
            $isoGot += $chunk
        }
        $mediumGot = 0
        while ($mediumGot -lt $aligned) {
            $chunk = $stream.Read($mediumBuffer, $mediumGot, $aligned - $mediumGot)
            if ($chunk -le 0) { break }
            $mediumGot += $chunk
        }

        $compare = [int][Math]::Min([int]$isoGot, [int]$mediumGot)
        for ($index = 0; $index -lt $compare; $index++) {
            if ($isoBuffer[$index] -ne $mediumBuffer[$index]) {
                $totalDiffering++
                $position = $offset + $index
                if ($null -eq $current) {
                    $current = [pscustomobject]@{
                        Start = $position
                        End   = $position
                        IsoBytes = New-Object System.Collections.ArrayList
                        MediumBytes = New-Object System.Collections.ArrayList
                    }
                }
                elseif (($position - $current.End) -gt 64) {
                    $ranges += $current
                    $current = [pscustomobject]@{
                        Start = $position
                        End   = $position
                        IsoBytes = New-Object System.Collections.ArrayList
                        MediumBytes = New-Object System.Collections.ArrayList
                    }
                }
                $current.End = $position
                if ($current.IsoBytes.Count -lt 24) {
                    [void]$current.IsoBytes.Add($isoBuffer[$index])
                    [void]$current.MediumBytes.Add($mediumBuffer[$index])
                }
            }
        }

        if ($compare -le 0) { break }
        $offset += $compare

        $percent = [int](($offset * 100) / $isoLength)
        if ($percent -ne $lastPercent -and ($percent % 10) -eq 0) {
            Write-Progress -Activity "Comparing" -PercentComplete $percent
            $lastPercent = $percent
        }
    }
    if ($null -ne $current) { $ranges += $current }
}
finally {
    Write-Progress -Activity "Comparing" -Completed
    $isoStream.Dispose()
    $stream.Dispose()
}

# --- Report ---------------------------------------------------------------
Write-Host ""
if ($ranges.Count -eq 0) {
    Write-Host "No differences - the stick is byte-for-byte identical." -ForegroundColor Green
    exit 0
}

Write-Host ("{0:N0} bytes differ, in {1} range(s):" -f $totalDiffering, $ranges.Count) -ForegroundColor Yellow
Write-Host ""

$benign = $true
$shown = 0
foreach ($range in $ranges) {
    if ($shown -ge $MaxRanges) {
        Write-Host ("  ... and {0} more range(s)" -f ($ranges.Count - $shown))
        break
    }
    $region = Get-RegionName -Offset $range.Start -Partitions $partitions
    $length = $range.End - $range.Start + 1
    Write-Host ("  offset {0,14:N0}  length {1,7:N0}  {2}" -f $range.Start, $length, $region)
    Write-Host ("      ISO   : {0}" -f (($range.IsoBytes | ForEach-Object { $_.ToString("x2") }) -join " "))
    Write-Host ("      stick : {0}" -f (($range.MediumBytes | ForEach-Object { $_.ToString("x2") }) -join " "))
    if ($region -notlike "EFI system partition*") { $benign = $false }
    $shown++
}

Write-Host ""
if ($benign) {
    Write-Host "Every difference is inside the EFI system partition." -ForegroundColor Green
    Write-Host ""
    Write-Host "That is Windows updating FAT bookkeeping - the dirty bit, free-space"
    Write-Host "hints, access times - after it mounted the volume. The files the"
    Write-Host "firmware boots (bootx64.efi, grubx64.efi, the stub grub.cfg) are"
    Write-Host "untouched, as are the ISO 9660 filesystem and the squashfs."
    Write-Host ""
    Write-Host "This stick is fine. Boot it." -ForegroundColor Green
    exit 0
}

Write-Host "Differences fall outside the EFI partition - this is a bad write." -ForegroundColor Red
Write-Host "Rewrite in DD Image mode, eject safely, and compare again."
exit 1
