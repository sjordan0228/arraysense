# Troubleshooting

Most of what a new installation hits happens in the first hour, before the first
poll is ever stored, and each failure leaves a distinct trace. The management
command for reading that trace is:

```bash
arraysense logs -n 50
```

`arraysense logs` forwards its arguments to journalctl, so `-n 200` and
`--since today` work the same way. `arraysense status` prints what the service
thinks it is doing — version, whether the collector is connected, which driver
and database — or says plainly that it is not answering on its port.

## In the first hour

### The installer asks which port to use

The installer probes port 80 and uses it when it is free. When something already
listens there it asks, defaulting to 8080, because the usual occupant of port 80
is a web server the owner cares about and silently moving would leave them
looking for the dashboard at an address nobody mentioned.

Choose 8080 and the dashboard URL carries the port: `http://<host>:8080`. The
choice is written into a systemd drop-in,
`/etc/systemd/system/arraysense.service.d/port.conf`, which is where the
installer and `arraysense upgrade` both expect it.

### The dashboard does not come up, and the log shows a bind failure

If the port the installer chose is claimed by something else later — another web
server, a second copy of this service — systemd reports a bind failure on every
start. `arraysense status` says `service: not answering on port 8080`, and
`arraysense logs` shows the underlying `address already in use`.

Free the port, or move the service by editing the `--port` value in the
`port.conf` drop-in above, then:

```bash
systemctl daemon-reload
systemctl restart arraysense
```

### The setup wizard cannot reach the inverter

A fresh installation has no configuration file, so the service comes up in setup
mode and serves the wizard. The wizard's *detect* step opens the candidate
connection and reads the inverter's serial; when nothing answers, it reports the
connection error in the browser.

The journal confirms the mode: `no configuration at ... — serving first-run
setup` appears at startup. After the wizard writes its configuration and the
service restarts, a still-unreachable inverter shows up like any other — as
repeated `poll failed` lines in `arraysense logs`. The usual causes are a wrong
dongle address or serial, and dongle firmware with no port 8000, next.

### Nothing answers on port 8000

Some dongle firmware has no port 8000 at all, and Ethernet dongles never exposed
it. A dongle that is otherwise reachable but refuses the connection on 8000 is
this problem, and there is no way to re-enable the port. The durable path is
wired RS485; see the full section below and
[docs/raspberry-pi.md](raspberry-pi.md).

### Serial polls fail with "No such file or directory"

The adapter is plugged in and the device node exists on the host, but every poll
fails with `could not open port ... No such file or directory` — an error that
points nowhere near the real cause. Two things must both be true for a serial
installation: the service user is a member of the `dialout` group, and the unit
runs with `PrivateDevices=false` so the adapter's device node is visible inside
the sandbox. Both are already in the shipped unit, so a serial install works out
of the box; a unit that predates them, or was tightened back for a dongle-only
install, fails exactly this way. See [docs/raspberry-pi.md](raspberry-pi.md).

### The database fails with "attempt to write a readonly database"

The unit runs with `ProtectSystem=strict`, which makes the whole filesystem
read-only to the service apart from its own state directory. A database on a
separate disk — a USB SSD, say — is outside that one writable directory, so every
write fails with `attempt to write a readonly database` while the file is plainly
there. The fix is a `ReadWritePaths` drop-in naming the database's directory. See
[docs/raspberry-pi.md](raspberry-pi.md).

### The log fills with CRC errors from the first poll

The dongle accepts exactly one TCP client, and anything else that connects — the
vendor's app, another collector, even a passive listener — evicts this one
mid-read. That is why yield mode exists, and why the first-hour advice is to run
one client. The full story is the first section below.

## Connection drops repeatedly, or the log shows CRC errors

The WiFi dongle accepts one TCP connection at a time. When a second client connects,
the dongle closes the first. Two clients polling in a loop will evict each other
continuously, and each eviction truncates a read partway through, which surfaces as a
CRC error.

On the reference system this produced roughly 484 CRC errors per day, caused by the
EG4 monitoring app and another collector both polling the same dongle.

Stop every other client. That includes the EG4 app, Solar Assistant, Home Assistant
integrations, and any scripts. Passive listening does not avoid the problem, because
a passive listener still occupies the single connection slot.

## Connection refused, or nothing answers on port 8000

Recent dongle firmware removes access to port 8000. Ethernet dongles never exposed
it.

If your dongle was working and stopped after a firmware update, this is the likely
cause. There is no way to re-enable the port. The durable alternative is wired
RS485, which is a supported transport — the reference installation reads the
inverter that way. See [docs/raspberry-pi.md](raspberry-pi.md).

## Battery data is missing or shows no modules

Per-module battery values come from the inverter, which populates them from the CAN
bus. If the batteries are not in closed-loop CAN communication with the inverter,
those registers stay empty.

Check that the CAN cable is connected between inverter and battery, that the battery
protocol is set to match the inverter, and that the inverter's own display shows
per-battery information. If the inverter cannot see the modules individually, neither
can this software.

Missing battery data is recorded as absent, not as zero. A module reading `0%` is a
real measurement; a module with no data will be shown as unavailable.

## The pages load but the numbers stopped moving

The web server and the collector run in one process, so the pages can serve
perfectly while collection has stopped. Check what the collector says about
itself rather than trusting that the site is up:

```bash
curl -s http://<host>/api/status
```

`last_success` is the thing to read. If it is minutes old while `last_failure`
is not moving either, the poll loop is stuck rather than failing, and the
watchdog will restart the service within twenty minutes. If `last_failure` *is*
moving, the inverter is not answering and the loop is doing its job — see the
connection sections above.

`total_samples` resetting to a small number means the service restarted; that is
expected after a deploy and after a watchdog stall.

## Gaps in the charts

Gaps are recorded deliberately when the inverter could not be reached, and rendered
as breaks rather than smoothed over. A gap means data was genuinely missing for that
period.

Frequent short gaps usually mean connection contention. See the first section.

## I need to use the vendor app for a firmware update

Firmware updates go through the EG4 app, which needs the dongle's single connection
slot. The dashboard has a control that releases the connection for a set period and
reconnects afterwards — the *Release 5 min* button — so this does not require
stopping the service.

## The database is growing faster than expected

Check `poll_interval`. At the default eleven seconds the database grows about
5.3 MB per day across all tiers (measured on the reference installation).
Halving the interval roughly doubles that.

If the database is on a Raspberry Pi SD card, move it to a USB SSD. Sustained
database writes will eventually wear a card out.

## The nightly backup

The nightly backup has nothing on the dashboard: no page says a backup is due,
ran, or failed, so every failure below is something to go looking for rather
than something that announces itself. The settings page is where it is
configured — whether it runs, where it writes, how many copies it keeps, and
when — and how the pieces fit together, with the measured cost to the card, is
[docs/raspberry-pi.md](raspberry-pi.md). This section is the failures.

### The backup never runs, and nothing says so

A scheduled backup that fails leaves no trace on any page. Its failures go only
to the journal of the backup unit, which is a different unit from the
collector's — the `arraysense logs` shortcut forwards to the collector's unit
only. The command that shows them is the plain one:

```bash
journalctl -u arraysense-backup.service
```

When the timer last fired and when it will fire next is a separate question:

```bash
systemctl list-timers arraysense-backup.timer
```

The timer fires every fifteen minutes and asks the settings whether a backup is
due — enabled, the configured time passed, today's archive missing — and a
firing that decides there is nothing to do returns silently. So a recent `LAST`
means the timer fired, not that a backup was written; the journal, and the
archive's own date, are what say whether anything actually happened.

The trap is the manual test. `sudo arraysense backup` runs as root with no
sandbox, and root can write anywhere. The timer's run runs as the `arraysense`
user under `ProtectSystem=strict`, which allows it only its declared paths. So a
hand-run backup can succeed, write a real archive, and print the restore command
while the scheduled one fails every night. The manual test passes, the owner
concludes backups work, and nothing is backing up. The journal is the only
witness to the difference.

### Read-only file system on the lock or the working copy

The backup unit runs under `ProtectSystem=strict`, which makes the whole
filesystem read-only apart from its declared writable paths. The compressed
archive goes to the backup directory, which the unit can write. But the working
copy and the lock are written *beside the database*, not in the backup
directory — the working copy because it is the full uncompressed database, and
writing that full-size copy to the card would undo the reason the database was
moved off it in the first place. A database that lives outside the unit's
writable set therefore fails every night with `Read-only file system`, on the
lock or the working copy, while the filesystem permissions are perfectly fine:
the sandbox is the read-only thing, and no bit on the directory records it.

The reference installation keeps its database on a USB SSD at
`/mnt/ssd/arraysense`. The fix is a drop-in naming that directory:

    # /etc/systemd/system/arraysense-backup.service.d/ssd.conf
    [Service]
    ReadWritePaths=/mnt/ssd/arraysense

then `systemctl daemon-reload`. This is the same carve-out the collector service
needs for its own writes — both units run under the same sandbox.

This was found on a real machine, and before it was fixed the failure did not
even look like this: the journal said `another backup is running` when no backup
was running, which sent somebody looking for a process that did not exist. The
`ReadWritePaths` line was the fix there too, and it is the phrase to search for
when a backup reports nothing written.

### A destination the settings page refused

Changing the backup directory on the settings page does not just store the path:
the service first proves it could write there, by creating and removing a real
file, under its own sandbox. A destination that only fails at the configured
hour fails unattended, and this is the one moment a person is present to read
the remedy. The page names which of three causes it hit, and the three look
alike from a distance while needing different fixes:

1. **The directory does not exist.** The page says so and prints the create
   command. `sudo install -d -o arraysense -g arraysense -m 0750 /path/to/backups`
   creates it owned by the service account, so whichever side runs first — root
   by hand, the service on the timer — the directory does not end up owned by
   the wrong user.

2. **The service user cannot write there.** The page says the path exists but
   the service cannot write to it. `sudo chown arraysense:arraysense /path/to/backups`
   hands the directory to the service account. Permissions are the whole story.

3. **The directory is outside the unit's `ReadWritePaths`.** The path exists and
   the owner is right, and the write still fails, because `ProtectSystem=strict`
   mounts it read-only to the service and the kernel answers `Read-only file
   system` — a cause no filesystem bit records. The fix is a drop-in naming the
   directory on *both* units — `arraysense-backup.service` writes the archive
   there, and `arraysense.service` is the process running the check when you
   save — then `systemctl daemon-reload`.

The wrong remedy is worse than none: a chown will not fix a read-only mount, and
no amount of `ReadWritePaths` will fix an owner.

### Restoring a backup

The restore command is `arraysense restore`, pointed at one of the archives:

```bash
arraysense restore /var/backups/arraysense/arraysense-2026-08-12.db.gz
```

A successful backup prints the exact restore command for that archive, so it
does not have to be remembered. What `restore` guarantees is what a shell
one-liner cannot: the live database is not touched until the unpacked archive
has been proven to be a real database with rows in it. It unpacks to a temporary
file beside the live database, checks that the file is non-empty, has database
pages, has the expected table, and has rows — naming which check failed — and
only then stops the service, preserves the current database as `.prev`, moves
the restored file into place, starts the service, and waits for it to answer.
The `.prev` is kept until the new file has started, so a restore that goes wrong
still has the pre-restore database on disk.

Do not hand-roll this with `gunzip` and a redirect. Measured on a real machine,
that recipe destroyed a live database: a corrupt archive made `gunzip` write
nothing, the shell redirect created a zero-byte file, `PRAGMA quick_check`
reported `ok` on zero bytes, and the `mv` replaced 1000 rows of history with an
empty file. The step that should have caught it silently did nothing, because
`sqlite3` was not installed on that machine at all.

### What a backup does not protect against

A backup is a copy of the database on the same machine. The point is that it
sits on a different disk from the database, so the database disk failing takes
the original and leaves the copy. But the machine itself takes both. A Pi that
dies — the board, the power supply, the whole box — carries the SSD and the card
together, and the copy dies with the original. It is a defence against the
database's disk failing, not against the machine failing, and it should not be
described as more than that.

## I forgot the access password

The Access section on the Settings page can set, change and clear the password,
but a forgotten one is exactly the case where the page cannot help — it asks
for the current password before changing or clearing anything. The recovery is
on the machine itself:

```bash
arraysense --clear-password
```

It removes the stored password and lets every write through again, and it says
which it did — cleared, or already off. It needs shell access to the box, which
is a stronger credential than the web password it clears.

## Finding your dongle serial

It appears on the dongle's label, in your router's DHCP client list, and as the name
of the WiFi access point the dongle broadcasts. It is ten characters and usually
starts with `BA`, `BJ`, `BG`, `BE` or `DJ`.
