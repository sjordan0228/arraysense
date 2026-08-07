# Troubleshooting

> Solar ArraySense is not yet functional. The problems below are properties of the
> inverter and its dongle, documented here because they are the ones people actually
> hit. They apply whatever software you use to read the inverter.

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
cause. There is no way to re-enable the port. The alternative is a wired RS485
connection to the dongle port's pins 14 (B) and 15 (A), which is planned but not yet
implemented.

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
slot. Solar ArraySense will provide a control to release the connection for a set
period and reconnect afterwards, so this does not require stopping the service.

## The database is growing faster than expected

Check `poll_interval`. At the default eleven seconds, expect roughly 9 MB per day
including the rollup tiers. Halving the interval roughly doubles that.

If the database is on a Raspberry Pi SD card, move it to a USB SSD. Sustained
database writes will eventually wear a card out.

## Finding your dongle serial

It appears on the dongle's label, in your router's DHCP client list, and as the name
of the WiFi access point the dongle broadcasts. It is ten characters and usually
starts with `BA`, `BJ`, `BG`, `BE` or `DJ`.
