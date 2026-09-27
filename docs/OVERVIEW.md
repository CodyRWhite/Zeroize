# Zeroize - architecture

How the pieces fit, and why they are arranged this way. For what the tool does
and how to use it, see the [README](../README.md).

## The shape of a run

```
discovery  ──>  gating  ──>  confirmation  ──>  engine  ──>  certificate
   │              │              │                │              │
 lsblk         methods.py    dialogs.py       engine.py       pdf.py
 nvme-cli      capability    typed phrase     preflight       naming.py
 hdparm        registers     per-drive list   + threads       JSON sidecar
 DMI           decide what   of serials       + interlocks
               is offered
```

Each stage hands the next a finished object and stops caring:

1. **Discovery** produces a list of `Device`. Every fact about a drive is read
   once, here, and nothing downstream re-queries the hardware.
2. **Gating** turns a `Device` into a list of `MethodAvailability` - every
   method, whether this drive supports it, and if not, why not.
3. **Confirmation** turns the operator's selection into `(Device, EraseMethod)`
   pairs.
4. **The engine** turns each pair into an `EraseResult`, and the batch into a
   `RunSummary`.
5. **The certificate** renders a `RunSummary`. It never touches hardware, which
   means a certificate can be re-issued from a saved run with the drive long
   gone.

## Layering

```
        ui/  ────────────────┐        GTK 4 / libadwaita
                             │        imports everything below
  certificate/  ─────────────┤        ReportLab only
        erase/  ─────────────┤        the catalogue, gating, engine
     discovery/  ────────────┤        lsblk, nvme-cli, hdparm, DMI
  models · config · process · logging · paths · branding
```

Nothing below `ui/` imports it. That is what makes `zeroize list` and
`zeroize erase` work over SSH on a machine with no desktop stack - and it also
means the destructive code paths can be tested without a display.

`process.py` is the only module that runs an external command. Everything goes
through it, which buys three things a scattered `subprocess.run` would not:
a complete audit trail written *before* each command runs, one global dry-run
switch, and consistent handling of a missing tool.

## Key decisions

### Capability registers decide what is offered

Nothing is inferred from a drive's name, vendor or transport. `OACS`, `FNA` and
`SANICAP` for NVMe; the `hdparm -I` security block for ATA. A method a drive did
not claim to support is never offered, and unsupported methods are shown with
their reason rather than hidden - an operator who cannot see why an option
vanished will assume the tool is broken.

### The safety interlock is based on what is mounted

Not on device naming. On a live USB the boot medium is frequently `/dev/sda`
while the target is `/dev/nvme0n1`; a naming heuristic gets that exactly
backwards. `_protection_reason` walks the whole child tree, so root on LVM on
LUKS on a partition still protects the physical drive underneath.

### The engine re-checks everything

`_preflight` runs immediately before any command, repeating checks the interface
already made. The scan could be minutes old, a drive could have been hot-plugged,
and the consequence of being wrong is unrecoverable.

### Overwrite is written here, not delegated to `dd`

`dd` would work, but it reports progress only when it finishes, cannot be
cancelled promptly, and gives no way to verify against what was actually
written. The internal writer records a SHA-256 of each block that will later be
sampled *as it writes it*, so verification compares against what was sent to the
platter rather than against the pattern that was meant to be sent - which is the
difference between catching a drive that ignored the write and not.

### Verification is reported honestly

A firmware erase cannot be verified by comparing against an expected pattern,
because the specification does not say what a drive must return afterwards. So
the check is the one that means something: sample the device and confirm no
partition table, filesystem superblock, LVM or LUKS header survived. The
certificate says "no recoverable structures found in N% of the device, sampled",
because that is true. "100% verified" would not be.

### Cancellation admits its limits

Software overwrites stop between blocks. Firmware commands are not interruptible
by design - the drive is unusable until they finish - so cancel there means
"stop waiting and report". The interface says so rather than implying otherwise.

## Threading

The engine runs one thread per drive, bounded by `erase.max_concurrent_jobs`.
Firmware commands spend nearly all their time waiting on a controller, so four
drives take barely longer than one; software overwrites contend for bus
bandwidth, which is why the limit is configurable and defaults low.

`run_with_heartbeat` exists because `nvme format` and `hdparm --security-erase`
block for minutes or hours with no output. It runs the command on its own thread
and ticks a callback meanwhile, so the interface keeps showing elapsed time.
Abandoning the wait never kills the command: a half-completed firmware erase is
worse than a completed one.

Every progress report crosses into the main loop through a single
`GLib.idle_add` in `MainWindow._on_progress_from_worker`. Enforcing it at one
boundary rather than in each caller means an implementation cannot forget, and
touching GTK from a worker thread produces crashes that surface minutes into an
irreversible operation.

## Per-file documentation

The five-pillar Documentation Standard asks for a `docs/<file>.md` per source
file. This tool deviates deliberately: every module carries a substantial
docstring covering the same ground - what it does, why it is arranged that way,
and the traps specific to it - and a parallel tree of Markdown restating them
would drift out of step with the code on the first change. [INDEX.md](INDEX.md)
maps each file to its purpose and points at the docstring.

## Testing

121 tests, no hardware required. The simulated device set in
`discovery/simulation.py` deliberately covers the awkward capability
combinations: Format NVM without Sanitize, Sanitize without Format, neither, a
BIOS-frozen SATA disk, a spinning disk, and a protected system disk.

The tests that matter most are in `test_nvme.py`, covering the two failure modes
that are *silent* - a multi-namespace format that erases half a drive while
reporting success, and a failed sanitize read as a successful one because it had
stopped running.
