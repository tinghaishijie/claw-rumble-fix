# Claw Rumble Fix

A [Decky Loader](https://decky.xyz) plugin for the **MSI Claw A8** (AMD Ryzen Z2
Extreme, board `MS-1T8K`) running SteamOS. It targets one problem: rumble
disappears after a number of suspend/resume cycles and, until now, only an EC
reset (holding the power button for ~25 s) brought it back.

> Status: investigated on one MSI Claw A8 (SteamOS 3.9.5, kernel 7.2.7-valve2,
> InputPlumber 0.78.0). Resetting the controller MCU is **confirmed** to bring
> rumble back. What breaks rumble is **not established**; the current suspect
> is another plugin sending raw commands to the controller after every
> wake-up (see [Findings](#findings)).

## What it does

- **Fix rumble now**. Resets the controller MCU, writes its settings back if
  the reset changed them, and pulses the motors. It also reads the MCU's
  profile memory before and after the reset and records the difference, to
  find out what the reset actually repairs.
- **Test rumble**. Drives both motors straight through the kernel, bypassing
  InputPlumber and Steam Input.
- **Buzz after every wake-up** (optional, off by default). Sends a short pulse
  two seconds after each wake-up. A missing buzz dates the rumble loss to that
  very sleep, so a report saved right then shows what happened around it.
- **Unexpected MCU ACKs**. Counts `hid-msi: Got unexpected ACK from MCU` lines
  in the kernel log, in total and for the first 45 s after each wake-up. Each
  one is the MCU answering a command the kernel driver did not send, or one
  of the driver's own flash commits.
- **Who writes to the controller**. Lists every process that writes to the
  controller's sysfs settings (fanotify).
- **Save diagnostics report**. Writes a `claw-rumble-fix-diag-*.txt` file to
  the home folder with the controller state, the profile memory, a timeline
  of writes, udev events and kernel messages, and the InputPlumber journal.

The plugin does not touch the controller on its own, except for that optional
buzz. After a wake-up it otherwise only reads the kernel log. The timeline is
also written to the plugin's log file, so it survives a plugin restart.

## Findings

While rumble is dead:

- the USB link is healthy (no disconnects or errors across dozens of suspends),
- the gamepad mode and the stored rumble intensity are unchanged,
- the kernel accepts rumble commands, but the motors stay silent.

So the controller MCU itself is in a bad state. Only the MCU reset (hid-msi
`reset` attribute) revived rumble. These did **not** help: rewriting the
rumble intensity, re-sending the gamepad mode, a USB port reset, a hub port
power cycle, restarting InputPlumber.

### Ruled out: InputPlumber's udev rule

An earlier version blamed the rule in `99-inputplumber-device-setup.rules`
that rewrites the back-button mappings on every device event, and overrode
it. With the override verified active, the kernel messages after each wake-up
did not change and rumble was lost again. That option has been removed.

### Current suspect: raw RGB commands from HueSync

The affected device runs the [HueSync](https://github.com/honjow/huesync)
plugin. For the Claw it writes RGB data straight to the controller's hidraw
node (profile write command `0x21`), bypassing `hid-msi`. Five seconds after
every resume it re-applies its settings, which matches the kernel log
exactly: a pair of unexpected ACKs 5.7 s after each `PM: suspend exit`, then
usually two more pairs.

That explains the kernel messages. Whether these writes are what eventually
breaks rumble is **unproven**. The direct test would be to run without
HueSync for a few days; that has not been done. Instead the plugin collects
evidence with HueSync running: which wake-up loses rumble (the optional
buzz), what happened around it (the timeline), and what the MCU reset
changes in the profile memory.

### Side effect of the MCU reset

After a reset Steam may list an extra "Xbox 360 Controller". That entry is the
Claw's own physical pad (USB `0db0:1901`), which InputPlumber normally hides
behind its virtual controller. The reset makes the pad re-enumerate on USB;
the likely explanation (not verified) is that Steam opens it before
InputPlumber hides it again and keeps it open. Restarting InputPlumber does
not remove it; a reboot or a Steam restart should.

## Install

Install Decky Loader first. Then, in Desktop Mode, run in Konsole:

```bash
curl -fsSL https://raw.githubusercontent.com/tinghaishijie/claw-rumble-fix/main/install.sh | bash
```

It downloads the newest release, verifies its checksum and restarts Decky. To
install an archive you already have, pass its path:
`bash install.sh ~/Downloads/claw-rumble-fix.zip`.

Alternatively use Decky's own installer: Decky settings -> General -> enable
**Developer mode**; then Developer -> **Install plugin from ZIP file**. Use
`claw-rumble-fix.zip` from the release **assets**, not GitHub's "Source code"
download, which has no built frontend.

If the plugin does not show up, check the loader log:
`journalctl -u plugin_loader -b --no-pager | tail -n 80`

## Requirements and limits

- Needs the in-kernel `hid-msi` driver with its sysfs attributes (present in
  SteamOS 3.9.5).
- The backend runs as root (sysfs writes, hidraw access, kernel log).
- The plugin never performs an EC reset and never touches firmware.
- Reading the profile memory sends read-only commands to the MCU through
  hidraw. It only happens when you press *Fix rumble now* or save a report.

## Development

```bash
pnpm install
pnpm run build      # frontend -> dist/index.js
pnpm test           # backend unit tests (fake sysfs, no hardware needed)
pnpm run package    # -> out/claw-rumble-fix.zip
```

Layout: `main.py` (backend, standard library only), `src/index.tsx`
(quick-access panel), `tests/test_backend.py`.

## License

MIT
