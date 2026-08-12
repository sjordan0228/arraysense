# Running on a Raspberry Pi

The reference installation runs on a Raspberry Pi 4, wired to the inverter over
RS485 rather than the WiFi dongle, with the database on a USB SSD. Nearly
everything that cost time on the way there is Pi-specific and useless to
somebody on an Intel NUC, so this page gathers it in one place. It describes
the setup the reference machine actually has — read off the machine rather than
reconstructed — and assumes the software is installed first, as
[installation.md](installation.md) describes.

## Which Pi, and its power supply

The reference machine is a Raspberry Pi 4. Memory and CPU are not the
constraint — the requirements are modest — so the model matters less than the
two things a Pi has to carry all day: continuous writes, and a device on a USB
bus. Those two workloads are what this page is about.

This page does not name a power supply. The reference machine's configuration
records none, and a wattage printed here would be invented rather than
verified. What a supply has to survive is a service that writes to a USB SSD
every few seconds, indefinitely — the workload that shapes the rest of this
page.

## The SD card is not a database device

SD cards wear out under sustained writes, and a card that fails takes the
database with it. The tool this project replaces wrote about 400 GB a year to
the card on the reference installation. ArraySense writes less than that, but
it writes continuously, every poll, every day. The reference database stands at
264 MB after 668 days of hourly history and 34.5 days of raw collection — small
enough that the risk sounds theoretical, and continuous enough to be real.

Put the database on a USB SSD and point `database_path` at it. On the reference
machine the database directory is `/mnt/ssd/arraysense`, the same path the
`ReadWritePaths` carve-out below names.

## Mounting the SSD by UUID

A USB SSD is mounted by filesystem UUID, never by device name. The kernel names
USB disks in the order they appear on the bus, so `/dev/sda` today can be
`/dev/sdb` after a reboot or after another disk is plugged in; a mount by name
eventually mounts the wrong device, or nothing. The UUID belongs to the
filesystem and does not move.

The reference machine's `/etc/fstab` line:

    UUID=<your-filesystem-uuid>  /mnt/ssd  ext4  defaults,noatime,nofail,x-systemd.device-timeout=10  0  2

Find your own UUID with `blkid`, or `lsblk -o NAME,UUID,SIZE,MOUNTPOINT`. The
placeholder above is exactly that — the real UUID belongs to your filesystem,
and copying one from anywhere else mounts the wrong disk or nothing.

`nofail` and `x-systemd.device-timeout=10` are the parts that cost a boot.
Without `nofail`, an SSD that is absent — unplugged, or failed — stops the boot
at the mount with a prompt nobody is watching. The timeout keeps a
slow-to-enumerate SSD from hanging the boot for a long wait instead. The trade
is a boot that comes up without the database; the service then fails on the
missing database with a logged, diagnosable error, where a machine that will
not boot at all cannot even be reached.

## The `ReadWritePaths` carve-out — required

The shipped unit runs the service with `ProtectSystem=strict`, which makes the
whole filesystem read-only to it apart from the `StateDirectory`
(`/var/lib/arraysense`). Put the database on the SSD — outside that one
writable directory — and every write fails with:

    attempt to write a readonly database

The SSD mounts fine and the file is visible; the service simply has no
permission to write there, and the error names neither the disk, the mount, nor
the directory permissions, so it is easy to chase the wrong thing. The fix is a
drop-in that carves the SSD path back out of the read-only filesystem:

    # /etc/systemd/system/arraysense.service.d/ssd.conf
    [Service]
    ReadWritePaths=/mnt/ssd/arraysense

then `systemctl daemon-reload` and restart the service. This is not optional:
a database on an SSD with no carve-out is a service whose every write fails,
and the "put the database on an SSD" advice elsewhere in these docs is exactly
what a reader follows straight into this error. Only the named path becomes
writable; the rest of the sandbox is untouched.

## A daily backup, on the card that is not the database

The SSD holds the only copy of the database, and an SSD can fail like anything
else. `arraysense backup` writes a compressed copy of the database every day to
`/var/backups/arraysense` — a directory on the SD card, which is precisely where
the database itself does not live, and that is the whole point: a backup on the
same disk as the original is protection against nothing.

Measured on the real 264 MB database:

| step | size |
| --- | --- |
| live database | 264 MB |
| working copy beside it, on the SSD | 264 MB — `Connection.backup()` copies free pages too |
| compressed, on the card | about 21 MB |

A compressed daily copy writes about **7.2 GB a year** to the card, against
96 GB a year for a naive `cp` of the raw file — the difference between 1.8% and
24% of the write load the database was moved off the card to escape. The card
only ever receives the compressed file. The SSD needs free space equal to the
database size while a backup runs; if it runs out the collector's own writes fail
for that window and are recorded as gaps.

The uncompressed working copy is never written to the card. It is made beside
the database itself with SQLite's own online backup API — the only correct way
to copy a live database that is being written to, in WAL mode, where a plain
file copy of the `.db` alone would miss the `-wal` and produce a torn snapshot —
then verified with `PRAGMA quick_check` before it is trusted, and only then
compressed and renamed into place on the card. A run interrupted halfway leaves
a `.part` file, never something that looks like a finished backup. Fourteen
daily copies are kept; the oldest are rotated away, and only after a new one has
been written and verified — never before, because a rotation that runs first
turns a failed backup into data loss.

Install it:

    sudo cp packaging/arraysense-backup.service packaging/arraysense-backup.timer /etc/systemd/system/
    sudo cp packaging/arraysense-backup.tmpfiles.conf /etc/tmpfiles.d/arraysense-backup.conf
    sudo systemd-tmpfiles --create
    sudo systemctl daemon-reload
    sudo systemctl enable --now arraysense-backup.timer

The tmpfiles.d fragment creates `/var/backups/arraysense` owned by the
`arraysense` user before either the timer or a hand-run backup touches it.
Without it, a hand-run backup as root creates the directory root:root and the
timer (which runs as `arraysense`) can never write there — failing silently every
night.

The timer fires at 03:15 and is `Persistent=true`, so a Pi that was off at 03:15
runs the backup when it comes back rather than skipping a day silently. The
service runs as the `arraysense` user under `ProtectSystem=strict`, with
`/var/backups/arraysense` the only writable path apart from the database's own
directory — which `StateDirectory=arraysense` covers, exactly as for the
collector. The working copy and the lock are written beside the database, so an
installation whose database lives outside that directory — the SSD here — needs
a carve-out for the backup service as well as for the collector:

    # /etc/systemd/system/arraysense-backup.service.d/ssd.conf
    [Service]
    ReadWritePaths=/mnt/ssd/arraysense

Getting this wrong produces a timer that fails every night with `Read-only file
system` on the lock or the working copy, so that is the phrase to search for
when a backup reports nothing written.

Run a backup by hand with `sudo arraysense backup` (add `--dir PATH` or
`--keep N` to override the destination or the number kept). A successful run
prints the path written and the restore command to use:

    restore with: arraysense restore /var/backups/arraysense/arraysense-2026-08-12.db.gz

`arraysense restore` unpacks the archive beside the live database, verifies it
thoroughly — the file is non-empty, has database pages, contains the expected
tables, and has rows — and only then stops the service, preserves the current
database as a `.prev`, removes the stale write-ahead log and shared-memory
sidecars, moves the restored file into place preserving the service user's
ownership, starts the service, and waits for it to answer. The live database
is never overwritten until every check above has passed. Add `--yes` for
unattended restores.

The old shell recipe that this replaces could destroy a live database in five
keystrokes. `gunzip` writes nothing on a corrupt archive, the shell redirect
creates a zero-byte file, `PRAGMA quick_check` prints "ok" on zero bytes, and
the `mv` overwrites the live database with an empty file. `sqlite3` was not
installed on the reference Pi, so the step that should have caught this
silently did nothing. A command is not an incantation; every guard a shell
line cannot carry is straightforward in Python.

## The USB enclosure: `usb-storage.quirks`

The reference SSD is a USB enclosure, and under sustained writes it dropped off
the bus once. The reference machine's kernel command line carries a quirk that
keeps it there:

    usb-storage.quirks=152d:a578:u

The parameter is `vendor:product:flags`. The `u` flag disables UAS — USB
Attached SCSI — for that one device and falls back to plain usb-storage.
`152d:a578` is *this* enclosure's vendor:product id, found with `lsusb`; it
identifies this enclosure and no other, so find your own rather than copying
it. With `lsusb`, your enclosure is a line whose leading `xxxx:yyyy` is its id;
take the two halves from your own hardware.

The parameter goes on the kernel command line — the same line `cat
/proc/cmdline` prints, and the same command confirms the quirk is in force.
This page does not name the file that holds the command line: its location has
moved between Raspberry Pi OS releases, and the reference facts record the
running line, not an edit path. Appending the parameter to that one line is the
whole change; the parameter itself is the part that cost time.

## Reaching the inverter over RS485

The WiFi dongle accepts exactly one TCP client, and newer firmware removes port
8000, so the durable path to the inverter is wired RS485 — the 485A/485B
terminals, which are independent of the dongle. The reference Pi reaches the
inverter that way, through a USB-to-RS485 adapter. This section is what a
serial installation needs, and each piece earned its place by failing without
it.

### A udev rule pins the adapter to `/dev/rs485`

The adapter is a CH340 (`1a86:7523`), and it is pinned to a stable name so the
service does not have to chase which `ttyUSB` it came up as. The rule, verbatim
from the reference machine:

    SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="rs485"

The rule's own comment on that machine records its limitation, and it is worth
knowing before you trust it: the CH340 reports no unique iSerial, so the match
is on vendor and product alone. Attach a second CH340 and the symlink points at
whichever udev processed last. One adapter, one rule.

The result, confirmed on the reference machine:

    /dev/rs485 -> ttyUSB0
    crw-rw---- 1 root dialout 188, 0 /dev/ttyUSB0

### The group, and the sandbox

Two things must both be true, or every poll fails with an error that points
nowhere near either of them:

    could not open port ... No such file or directory

The device node is owned by `dialout` with mode 0660, so the service user must
be a member of the group. The shipped unit already carries
`SupplementaryGroups=dialout`, which adds the group at runtime; the reference
machine's user is a member of `dialout` outright
(`groups=985(arraysense),20(dialout)`).

And the device node must be visible to the service. The shipped unit already
sets `PrivateDevices=false`, with a comment explaining exactly why: a private
`/dev` holds only pseudo-devices, and the adapter is not among them. A unit
running `PrivateDevices=true` hides every real device node and produces exactly
the error above. If your unit predates that setting, or you tightened it back
for a dongle-only install, a drop-in restores it:

    # /etc/systemd/system/arraysense.service.d/serial.conf
    [Service]
    PrivateDevices=false

Both are already in the shipped unit, so a serial installation works out of the
box. They are listed here because they are the two things that failed on the
way to the reference setup, and the first things to check when a serial poll
reports the device missing.

## Serving the dashboard on port 80

Ports below 1024 are privileged, and the service deliberately does not run as
root. Port 80 needs the one capability that grants that binding and nothing
else: `CAP_NET_BIND_SERVICE`.

If you installed with the bootstrap installer and chose port 80, the installer
wrote the drop-in for you, including the capability — do not write it twice.
This section is for a hand-set-up installation, or for reading what the
installer left behind.

The reference machine's drop-in:

    # /etc/systemd/system/arraysense.service.d/port.conf
    [Service]
    AmbientCapabilities=CAP_NET_BIND_SERVICE
    ExecStart=
    ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --config /etc/arraysense/config.toml --host 0.0.0.0 --port 80

The installer writes this file itself, and it leaves `--host` out because
`0.0.0.0` is already the default — the reference file just states it out loud,
so its absence is not a difference that matters.

The blank `ExecStart=` is deliberate, not a typo: systemd appends to a list
directive, so the drop-in must clear the unit's ExecStart before setting its
own — without the empty assignment the unit carries two ExecStart lines and
refuses to start. The capability grants the one low-port binding and nothing
else; running the whole service as root to get a shorter URL is the wrong
trade.
