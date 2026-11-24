# Claw Rumble Fix

A [Decky Loader](https://decky.xyz) plugin for the **MSI Claw A8** (board
`MS-1T8K`) on SteamOS. Rumble sometimes disappears after a suspend/resume
cycle: buttons and sticks keep working, the motors stay silent.

The cause is not known yet. Resetting the controller MCU brings rumble back,
so the plugin has one button that does exactly that.

## Fix rumble

1. Records the controller state while rumble is still broken.
2. Resets the controller MCU (hid-msi `reset` attribute) and puts the gamepad
   mode and back-button mode back if the reset changed them. The controller
   reconnects for a few seconds.
3. Records the state again and sends a short full-strength pulse, so you can
   feel that rumble is back.

The whole thing takes about 15 seconds.

While the controller reconnects, the plugin keeps the physical pad hidden
with two temporary udev rules in `/run/udev/rules.d`, so Steam does not pick
it up as an extra "Xbox 360 Controller". The rules are removed once
InputPlumber has hidden the pad again.

## Captures

Every fix writes a folder to `~/claw-rumble-captures/<timestamp>/` with a
`1-before-reset` and a `2-after-reset` record:

- the USB traffic of a rumble pulse (does the command reach the controller?),
- the controller MCU's profile RAM (`.mcu`),
- the embedded controller's RAM (`.ec`),
- the filtered kernel log, InputPlumber journal and input device list.

Comparing "before" (broken) with "after" (working) shows which layer drops
the rumble command. Capturing is read-only apart from the rumble pulse.

## Install

Install Decky Loader first. Then, in Desktop Mode, either:

**Installer script** (recommended). Run in Konsole:

```bash
curl -fsSL https://raw.githubusercontent.com/tinghaishijie/claw-rumble-fix/main/install.sh | bash
```

It downloads the newest release, verifies its checksum and restarts Decky. To
install an archive you already have, pass its path:
`bash install.sh ~/Downloads/claw-rumble-fix.zip`.

**Decky's ZIP installer.** Decky settings -> General -> enable **Developer
mode**; then Developer -> **Install plugin from ZIP file**, pick the zip and
confirm the install prompt.

The backend runs as root (required for sysfs writes and USB resets).

### The plugin does not show up

- Use `claw-rumble-fix.zip` from the release **assets**. GitHub's "Source
  code (zip)" has no built frontend and will not load.
- Check the loader log: `journalctl -u plugin_loader -b --no-pager | tail -n 80`

## Requirements

- The in-kernel `hid-msi` driver with its `reset` attribute.
- The backend runs as root (sysfs, usbmon, EC debugfs).
- Nothing persistent is changed; no EC reset, no firmware access.

## Development

```bash
pnpm install
pnpm run build      # frontend -> dist/index.js
pnpm test           # backend unit tests (fake sysfs, no hardware needed)
pnpm run package    # -> out/claw-rumble-fix.zip
```

## License

MIT
