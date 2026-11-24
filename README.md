# Claw Rumble Fix

A [Decky Loader](https://decky.xyz) plugin for the **MSI Claw A8** (AMD Ryzen Z2
Extreme, board `MS-1T8K`) running SteamOS. It targets one problem: rumble
disappears after a number of suspend/resume cycles and, until now, only an EC
reset (holding the power button for ~25 s) brought it back.

> Status: investigated on one MSI Claw A8 (SteamOS 3.9.5, kernel 7.2.7-valve2,
> InputPlumber 0.78.0). Resetting the controller MCU is **confirmed** to bring
> rumble back. The prevention option targets the suspected trigger and is
> **not confirmed yet**.

## What it does

- **Stop redundant flash writes** (prevention). Installs a runtime copy of
  InputPlumber's udev rule in `/run/udev/rules.d` in which every controller
  setting is only written when it differs from the wanted value. Nothing
  persistent is changed: the copy is regenerated from the installed rule at
  every start and is gone after a reboot once the option is off.
- **Flash commits since boot**. Counts the controller flash commits in the
  kernel log, and how many arrive in the first 45 s after each wake-up, so the
  effect of the option above can be checked.
- **Fix rumble now** (recovery). Resets the controller MCU, writes its
  settings back if the reset changed them, and pulses the motors.
- **Test rumble**. Drives both motors straight through the kernel, bypassing
  InputPlumber and Steam Input.
- **Save diagnostics report**. Writes `~/claw-rumble-fix-diag-<timestamp>.txt`
  with the controller state, input device nodes, filtered kernel log,
  InputPlumber journal and rules, and the plugin's wake-up history.

The plugin never writes to the controller on its own. After a wake-up it only
reads the controller state and the kernel log.

## Findings

While rumble is dead:

- the USB link is healthy (no disconnects or errors across ~19 suspends),
- the gamepad mode and the stored rumble intensity are unchanged,
- the kernel accepts rumble commands, but the motors stay silent.

So the controller MCU itself is in a bad state. Of everything tried, only the
MCU reset (hid-msi `reset` attribute) revived rumble. These did **not** help:
rewriting the rumble intensity, re-sending the gamepad mode, a USB port reset,
a hub port power cycle, restarting InputPlumber.

### Suspected trigger

The kernel log shows a burst of `hid-msi: Got unexpected ACK from MCU` after
every wake-up, roughly 170 in two days. Each one is the MCU acknowledging a
**flash commit**. They come from InputPlumber's udev rule
(`99-inputplumber-device-setup.rules`), which assigns `button_m1` and
`button_m2` on every add/change/bind event of the `hid-msi` device whether or
not the value changed; the kernel driver commits the controller flash on each
assignment. Upstream fixed a related hazard in the kernel
([OpenGamingCollective/linux#133](https://github.com/OpenGamingCollective/linux/pull/133)),
which is not in the SteamOS kernel tested here.

This is a correlation, not a proven cause. With **Stop redundant flash
writes** on, the counter should stop growing after wake-ups; if the theory
holds, rumble should stop disappearing.

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
  SteamOS 3.9.5) and InputPlumber's udev rule file.
- The backend runs as root (sysfs writes, udev rules, kernel log).
- The plugin never performs an EC reset and never touches firmware.
- If `/etc/udev/rules.d` already overrides InputPlumber's rule file, the
  prevention option leaves it alone and reports that.

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
