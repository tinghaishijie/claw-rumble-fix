# Claw Rumble Fix

A [Decky Loader](https://decky.xyz) plugin for the **MSI Claw A8** (AMD Ryzen Z2
Extreme, board `MS-1T8K`) running SteamOS. It targets one problem: rumble
disappears after a number of suspend/resume cycles and, until now, only an EC
reset (holding the power button for ~25 s) brought it back.

> Status: on one MSI Claw A8 the rumble loss was reproduced and **only the
> controller MCU reset (step 6) brought rumble back**; rewriting the intensity,
> re-sending the gamepad mode, USB resets and restarting InputPlumber did not.
> That reset is what the plugin now automates. The automatic after-wake path
> is unit-tested against a simulated sysfs tree but has not been confirmed on
> hardware yet, and the underlying MCU bug is still unexplained.

## What it does

- **Detects every wake-up** on its own (it compares `CLOCK_BOOTTIME` with
  `CLOCK_MONOTONIC`) and also listens to Steam's resume notification.
- **Automatic fix after wake-up** (configurable):
  - *Check & restore intensity* (default) - reads the rumble intensity stored
    in the controller MCU and rewrites it only if it changed. Healthy wake-ups
    cause no writes.
  - *Re-send gamepad mode* - makes the controller re-enumerate on USB.
  - *USB port reset* - resets the controller's USB port.
  - *Controller MCU reset* - resets the controller MCU after every wake-up.
    Pick this level if only step 6 fixes rumble for you. The controller
    reconnects for a few seconds after each wake.
- **Fix rumble now** - one tap: MCU reset, settings put back, test pulse.
- **Kernel reset-on-resume** (experimental toggle) - sets the
  `USB_QUIRK_RESET_RESUME` quirk for the controller at runtime, the same fix
  that was merged into the OpenGamingCollective kernel for Claw controllers
  wedging after s2idle. Nothing is written to the boot configuration.
- **Test rumble (direct)** - drives both motors straight through the kernel
  (evdev force feedback in XInput mode, HID report in DInput mode), bypassing
  InputPlumber and Steam Input.
- **Manual recovery ladder** - six actions ordered from light to heavy.
- **Diagnostics report** - one button writes
  `~/claw-rumble-fix-diag-<timestamp>.txt` with the controller state, filtered
  kernel log, InputPlumber journal and the plugin's wake-up history.

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

## Finding out what actually fixes your device

The next time rumble is gone, **do not EC-reset yet**. Open the plugin and:

1. Press **Save diagnostics report** (captures the broken state).
2. Press **Test rumble (direct)**.
   - It vibrates: the hardware is fine and the loss is in the software stack.
     Step 5 (*Restart InputPlumber*) is the likely fix.
   - It does not vibrate: the controller MCU lost state. Continue below.
3. Walk through **Manual recovery** from 1 to 6, pressing *Test rumble* after
   each step, and note the first step that brings rumble back.
4. Set **Automatic fix** to the matching level so it runs after every wake-up.

Also look at the *Rumble intensity* line while broken: `L 0 / R 0` or
`unreadable` means the stored intensity was lost, which the default automatic
level already repairs.

| Step | What it does | Side effects |
| --- | --- | --- |
| 1. Rewrite rumble intensity | Writes the left/right intensity to the MCU | One flash commit in the MCU |
| 2. Re-send gamepad mode | Sends the current mode again | Controller reconnects (~1-2 s) |
| 3. USB port reset | `USBDEVFS_RESET` on the controller | Short input hiccup |
| 4. USB re-plug | Power-cycles the hub port (or rebinds the device) | Controller reconnects; reboot if it does not return |
| 5. Restart InputPlumber | `systemctl restart inputplumber` | Virtual controller reconnects in Steam |
| 6. Controller MCU reset | hid-msi `reset` attribute | Controller reboots; gamepad mode, back-button mode and intensity are put back afterwards |

## An extra "Xbox 360 Controller" appears in Steam

That entry is the Claw's own physical pad (USB `0db0:1901`, XInput mode).
InputPlumber normally hides it and shows Steam a virtual controller instead.
A controller reset makes the pad re-enumerate on USB, and it can come back
without being hidden again. The plugin detects this after an MCU reset and
restarts InputPlumber when needed; the panel also shows a warning while the
pad is visible. Manual way out: *Restart InputPlumber* (step 5) or reboot.

## Requirements and limits

- Steps 1, 2, 6 and the intensity check need the in-kernel `hid-msi` driver
  with its sysfs attributes (`gamepad_mode`, `rumble_intensity_left/right`,
  `reset`). On a kernel without them those steps report *not exposed by
  kernel*; the USB-level steps, the rumble test and diagnostics still work.
- The plugin never performs an EC reset and never touches firmware.
- Intensity writes are committed to the controller's flash, which also affects
  Windows on the same device.
- Uninstalling the plugin does not revert the intensity stored in the
  controller. The reset-on-resume quirk is runtime-only and is gone after a
  reboot once the toggle is off.

## Development

```bash
pnpm install
pnpm run build      # frontend -> dist/index.js
pnpm test           # backend unit tests (fake sysfs, no hardware needed)
pnpm run package    # -> out/claw-rumble-fix.zip
```

Layout: `main.py` (backend, standard library only), `src/index.tsx`
(quick-access panel), `tests/test_backend.py`.

## Background

- `hid-msi` suspend/resume fix for wedged Claw controllers:
  https://github.com/OpenGamingCollective/linux/pull/133
- Claw A8 controller failing after resume: https://github.com/ShadowBlip/InputPlumber/issues/725
- Related plugin for back buttons and rumble strength:
  https://github.com/sekirofly/msi-claw-a8-back-buttons-rumble

## License

MIT
