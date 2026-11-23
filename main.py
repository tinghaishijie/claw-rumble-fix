"""Claw Rumble Fix - Decky Loader backend.

The MSI Claw A8 built-in controller (USB 0db0:190x) can lose rumble after a
number of suspend/resume cycles on SteamOS; so far only an EC reset was known
to bring it back. This backend:

  * detects every wake-up (CLOCK_BOOTTIME vs CLOCK_MONOTONIC drift, plus a
    notification from the Steam frontend),
  * verifies the controller MCU state after wake and repairs it,
  * exposes a ladder of manual recovery actions, from light to heavy,
  * offers a rumble test that talks to the hardware directly, bypassing
    InputPlumber and Steam Input, and
  * writes a diagnostics report so the failing layer can be identified.

Everything here is Python 3.9 compatible and uses the standard library only.
"""
import asyncio
import collections
import fcntl
import glob
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import time
from datetime import datetime

import decky

SYS = "/sys"
DEV = "/dev"
RUN = "/run"

VENDOR = "0db0"
PID_MODES = {"1901": "xinput", "1902": "dinput", "1903": "desktop", "1904": "bios"}
SWITCHABLE_MODES = ("xinput", "dinput")
MKEY_MODES = ("macro", "disabled", "combination")

USB_QUIRK_RESET_RESUME = 0x2
USBDEVFS_RESET = 0x5514   # _IO('U', 20)
EVIOCSFF = 0x40304580     # _IOW('E', 0x80, struct ff_effect), 48 bytes on x86-64
EVIOCRMFF = 0x40044581    # _IOW('E', 0x81, int)
EV_FF = 0x15
FF_RUMBLE = 0x50

AUTO_LEVELS = ("off", "intensity", "mode", "usb", "mcu")
DEFAULT_SETTINGS = {
    # What to do after every wake-up:
    #   off       - nothing
    #   intensity - read rumble intensity back, rewrite it only if it changed
    #   mode      - re-send the gamepad mode (USB re-enumeration), then intensity
    #   usb       - USB port reset, then intensity
    #   mcu       - controller MCU reset (the only step confirmed to bring
    #               rumble back on real hardware), then intensity
    "auto_level": "intensity",
    # Seconds to wait after wake before touching the MCU.
    "resume_delay": 3.0,
    # Desired rumble intensity (0-100). None = adopt the hardware values.
    "target_left": None,
    "target_right": None,
    # Ask the kernel to reset the controller's USB port on every resume.
    "reset_resume_quirk": False,
}


class State:
    def __init__(self):
        self.settings = dict(DEFAULT_SETTINGS)
        self.history = collections.deque(maxlen=300)
        self.resume_count = 0
        self.last_handled = -1e9
        self.last_result = None
        self.lock = None
        self.watch_task = None


STATE = State()


def _log(msg):
    STATE.history.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    decky.logger.info(msg)


def _res(ok, msg, **extra):
    out = {"ok": bool(ok), "msg": msg, "changed": False}
    out.update(extra)
    return out


# --------------------------------------------------------------------------
# Low level helpers
# --------------------------------------------------------------------------

def _read(path):
    with open(path, "r", encoding="ascii", errors="replace") as f:
        return f.read().strip()


def _write(path, value):
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, (str(value) + "\n").encode("ascii"))
    finally:
        os.close(fd)


def _run_cmd(args, timeout=20):
    # Decky ships a bundled Python whose LD_LIBRARY_PATH breaks system binaries.
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, env=env)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def find_controller():
    """Locate the built-in Claw controller on the USB bus (None if absent)."""
    for dev in sorted(glob.glob(os.path.join(SYS, "bus/usb/devices/*"))):
        busid = os.path.basename(dev)
        if ":" in busid:
            continue
        try:
            if _read(os.path.join(dev, "idVendor")).lower() != VENDOR:
                continue
            pid = _read(os.path.join(dev, "idProduct")).lower()
        except OSError:
            continue
        if pid not in PID_MODES:
            continue
        info = {"busid": busid, "path": dev, "pid": pid, "pid_mode": PID_MODES[pid]}
        for key, attr in (("busnum", "busnum"), ("devnum", "devnum"),
                          ("bcd", "bcdDevice"), ("quirks", "quirks")):
            try:
                info[key] = _read(os.path.join(dev, attr))
            except OSError:
                info[key] = None
        # hid-msi registers its config attributes on one HID interface only.
        cfg = glob.glob(os.path.join(dev, busid + ":*", "0003:*", "gamepad_mode"))
        info["cfg"] = os.path.dirname(cfg[0]) if cfg else None
        return info
    return None


def quirk_active(ctrl):
    try:
        return bool(int(ctrl["quirks"], 16) & USB_QUIRK_RESET_RESUME)
    except (TypeError, ValueError):
        return False


def has_rumble_attrs(cfg):
    return bool(cfg) and all(
        os.path.exists(os.path.join(cfg, "rumble_intensity_" + side))
        for side in ("left", "right"))


def read_intensity(cfg):
    return (int(_read(os.path.join(cfg, "rumble_intensity_left"))),
            int(_read(os.path.join(cfg, "rumble_intensity_right"))))


def event_nodes(ctrl):
    """sysfs paths of every evdev node that belongs to the controller."""
    base = os.path.join(ctrl["path"], ctrl["busid"] + ":*")
    return sorted(glob.glob(os.path.join(base, "input/input*/event*")) +
                  glob.glob(os.path.join(base, "0003:*/input/input*/event*")))


def pad_exposed(ctrl):
    """True when the physical gamepad node is usable by regular users.

    InputPlumber hides the built-in pad (permissions 000, or the node moved
    away) and shows Steam a virtual controller instead. If the pad stays
    visible, Steam lists it as an extra "Xbox 360 Controller".
    """
    for event in event_nodes(ctrl):
        try:
            abs_caps = int(_read(os.path.join(event, "device/capabilities/abs")).split()[-1], 16)
            if abs_caps & 0x3 != 0x3:  # needs ABS_X and ABS_Y: a stick, not a keyboard
                continue
            st = os.stat(os.path.join(DEV, "input", os.path.basename(event)))
        except (OSError, ValueError, IndexError):
            continue
        if stat.S_IMODE(st.st_mode) != 0:
            return True
    return False


def inputplumber_state():
    _, out = _run_cmd(["systemctl", "is-active", "inputplumber.service"], 5)
    return out.splitlines()[0] if out else "unknown"


def read_mcu_state(cfg):
    """Snapshot of the settings the MCU keeps (None where unreadable)."""
    state = {}
    for name in ("gamepad_mode", "mkeys_function",
                 "rumble_intensity_left", "rumble_intensity_right"):
        try:
            state[name] = _read(os.path.join(cfg, name))
        except OSError:
            state[name] = None
    return state


def describe_state(state):
    return "mode=%s mkeys=%s intensity=L%s/R%s" % (
        state["gamepad_mode"], state["mkeys_function"],
        state["rumble_intensity_left"], state["rumble_intensity_right"])


def effective_targets(settings):
    left, right = settings.get("target_left"), settings.get("target_right")
    return (100 if left is None else int(left), 100 if right is None else int(right))


def wait_reenumerated(old_devnum, timeout=10.0):
    """Wait until the controller disconnected and came back on the USB bus."""
    deadline = time.monotonic() + timeout
    seen_gone = False
    while time.monotonic() < deadline:
        ctrl = find_controller()
        if ctrl is None:
            seen_gone = True
        elif seen_gone or ctrl["devnum"] != old_devnum:
            return ctrl
        time.sleep(0.25)
    return None


def wait_cfg(timeout=6.0):
    """Wait until hid-msi has registered its config attributes again."""
    deadline = time.monotonic() + timeout
    ctrl = None
    while time.monotonic() < deadline:
        ctrl = find_controller()
        if ctrl and ctrl["cfg"]:
            return ctrl
        time.sleep(0.25)
    return ctrl


def _open_chardev(sys_node, flags):
    """Open the character device behind a /sys/class-style node directory.

    InputPlumber hides the device nodes it manages, so look in its source
    directory too and fall back to a private temporary node.
    """
    name = os.path.basename(sys_node)
    major, minor = _read(os.path.join(sys_node, "dev")).split(":")
    rdev = os.makedev(int(major), int(minor))
    for directory in (os.path.join(DEV, "input"), DEV,
                      os.path.join(DEV, "inputplumber/sources")):
        node = os.path.join(directory, name)
        try:
            st = os.stat(node)
        except OSError:
            continue
        if stat.S_ISCHR(st.st_mode) and st.st_rdev == rdev:
            return os.open(node, flags)
    tmp = os.path.join(RUN, "claw-rumble-fix-" + name)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    os.mknod(tmp, 0o600 | stat.S_IFCHR, rdev)
    try:
        return os.open(tmp, flags)
    finally:
        os.unlink(tmp)


# --------------------------------------------------------------------------
# Recovery actions (blocking; run in a worker thread)
# --------------------------------------------------------------------------

def act_intensity(targets, force):
    """Verify the MCU rumble intensity and rewrite it when lost (or forced)."""
    ctrl = find_controller()
    if not ctrl:
        return _res(False, "controller not found on USB")
    cfg = ctrl["cfg"]
    if not has_rumble_attrs(cfg):
        msg = "kernel driver exposes no rumble_intensity_* attributes"
        return _res(not force, "skipped: " + msg, skipped=True)
    try:
        before = read_intensity(cfg)
    except (OSError, ValueError) as exc:
        before = None
        _log("intensity read failed: %s" % exc)
    if before == tuple(targets) and not force:
        return _res(True, "intensity intact (L%d/R%d)" % before)
    shown = "unreadable" if before is None else "L%d/R%d" % before
    last_error = None
    for _ in range(3):
        try:
            # Each store ends with a fire-and-forget flash commit; give the MCU
            # time to retire it before the next command.
            _write(os.path.join(cfg, "rumble_intensity_left"), targets[0])
            time.sleep(0.5)
            _write(os.path.join(cfg, "rumble_intensity_right"), targets[1])
            time.sleep(0.5)
            after = read_intensity(cfg)
            if after == tuple(targets):
                return _res(True, "intensity rewritten: %s -> L%d/R%d" % ((shown,) + after),
                            changed=True)
            last_error = "read back L%d/R%d" % after
        except (OSError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(1.0)
    return _res(False, "intensity rewrite failed (was %s): %s" % (shown, last_error))


def act_resend_mode():
    """Re-send the current gamepad mode; the MCU re-enumerates on USB."""
    ctrl = find_controller()
    if not ctrl:
        return _res(False, "controller not found on USB")
    if not ctrl["cfg"]:
        return _res(False, "kernel driver exposes no gamepad_mode attribute")
    path = os.path.join(ctrl["cfg"], "gamepad_mode")
    try:
        mode = _read(path)
    except OSError:
        mode = ctrl["pid_mode"]
    if mode not in SWITCHABLE_MODES:
        mode = ctrl["pid_mode"] if ctrl["pid_mode"] in SWITCHABLE_MODES else "xinput"
    try:
        _write(path, mode)
    except OSError as exc:
        return _res(False, "gamepad_mode write failed: %s" % exc)
    back = wait_reenumerated(ctrl["devnum"], 8.0)
    if not back:
        return _res(True, "mode '%s' re-sent (no USB re-enumeration observed)" % mode,
                    changed=True)
    wait_cfg()
    return _res(True, "mode '%s' re-sent, controller re-enumerated" % mode, changed=True)


def act_usb_reset():
    """USB port reset (same thing the kernel reset-resume quirk does)."""
    ctrl = find_controller()
    if not ctrl:
        return _res(False, "controller not found on USB")
    try:
        node = os.path.join(DEV, "bus/usb", "%03d" % int(ctrl["busnum"]),
                            "%03d" % int(ctrl["devnum"]))
        fd = os.open(node, os.O_WRONLY)
        try:
            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError) as exc:
        return _res(False, "USB port reset failed: %s" % exc)
    time.sleep(1.5)
    back = wait_cfg()
    if not back:
        return _res(False, "USB port reset sent, controller did not come back")
    return _res(True, "USB port reset done", changed=True)


def act_usb_replug():
    """Simulated unplug/replug: power-cycle the hub port, or rebind the device."""
    ctrl = find_controller()
    if not ctrl:
        return _res(False, "controller not found on USB")
    port_disable = os.path.join(os.path.realpath(os.path.join(ctrl["path"], "port")),
                                "disable")
    how = "hub port power cycle"
    try:
        if os.path.exists(port_disable):
            try:
                _write(port_disable, 1)
                time.sleep(1.5)
            finally:
                _write(port_disable, 0)
        else:
            how = "USB driver rebind"
            drv = os.path.join(SYS, "bus/usb/drivers/usb")
            _write(os.path.join(drv, "unbind"), ctrl["busid"])
            time.sleep(1.5)
            _write(os.path.join(drv, "bind"), ctrl["busid"])
    except OSError as exc:
        return _res(False, "%s failed: %s" % (how, exc))
    back = wait_reenumerated(ctrl["devnum"], 12.0) or find_controller()
    if not back:
        return _res(False, "%s done, controller did not come back (reboot to recover)" % how)
    wait_cfg()
    return _res(True, "%s done, controller is back" % how, changed=True)


def act_mcu_reset():
    """Reset the controller MCU (hid-msi 'reset' attribute).

    The reset may bring the MCU back with different settings, so the gamepad
    mode and the back-button mode are captured first and put back afterwards.
    Rumble intensity is handled separately by act_intensity().
    """
    ctrl = find_controller()
    if not ctrl:
        return _res(False, "controller not found on USB")
    path = os.path.join(ctrl["cfg"] or "", "reset")
    if not os.path.exists(path):
        return _res(False, "kernel driver exposes no reset attribute")
    before = read_mcu_state(ctrl["cfg"])
    hidden_before = not pad_exposed(ctrl)
    try:
        _write(path, 1)
    except OSError as exc:
        return _res(False, "MCU reset write failed: %s" % exc)
    if not wait_reenumerated(ctrl["devnum"], 12.0):
        return _res(False, "MCU reset sent, no USB re-enumeration observed")
    back = wait_cfg()
    if not back or not back["cfg"]:
        return _res(True, "MCU reset done, config interface not back yet", changed=True)
    time.sleep(1.0)
    after = read_mcu_state(back["cfg"])
    _log("MCU reset: before [%s] after [%s]" % (describe_state(before), describe_state(after)))
    notes = []
    for name, valid in (("gamepad_mode", SWITCHABLE_MODES), ("mkeys_function", MKEY_MODES)):
        old, new = before[name], after[name]
        if old not in valid or new is None or new == old:
            continue
        current = find_controller()
        if not current or not current["cfg"]:
            notes.append("%s not restored (controller missing)" % name)
            continue
        try:
            # Both attributes send SWITCH_MODE, which re-enumerates the device.
            _write(os.path.join(current["cfg"], name), old)
        except OSError as exc:
            notes.append("%s restore failed: %s" % (name, exc))
            continue
        wait_reenumerated(current["devnum"], 8.0)
        wait_cfg()
        time.sleep(0.5)
        notes.append("%s restored %s -> %s" % (name, new, old))
    # The re-enumerated pad must end up hidden behind InputPlumber again,
    # otherwise Steam shows it as a second controller.
    if hidden_before and inputplumber_state() == "active":
        deadline = time.monotonic() + 6.0
        exposed = True
        while exposed and time.monotonic() < deadline:
            current = find_controller()
            exposed = bool(current) and pad_exposed(current)
            if exposed:
                time.sleep(0.5)
        if exposed:
            notes.append("built-in pad was left visible; " + act_restart_inputplumber()["msg"])
    return _res(True, "MCU reset done" + ("; " + "; ".join(notes) if notes else ""),
                changed=True)


def act_mcu_fix(targets, pulse):
    """The confirmed recovery: MCU reset, intensity check, optional test pulse."""
    steps = [act_mcu_reset()]
    if steps[0]["ok"]:
        steps.append(act_intensity(targets, force=False))
        if pulse:
            steps.append(act_test_rumble())
    return _res(all(s["ok"] for s in steps), "; ".join(s["msg"] for s in steps),
                changed=True)


def act_restart_inputplumber():
    code, out = _run_cmd(["systemctl", "restart", "inputplumber.service"], 30)
    if code != 0:
        return _res(False, "InputPlumber restart failed: %s" % (out or code))
    return _res(True, "InputPlumber restarted", changed=True)


def _has_ff_rumble(event_node):
    try:
        text = _read(os.path.join(event_node, "device/capabilities/ff"))
    except OSError:
        return False
    bits = 0
    for word in text.split():  # highest machine word first
        bits = (bits << 64) | int(word, 16)
    return bool(bits & (1 << FF_RUMBLE))


def _ff_pulse(fd, seconds):
    effect = bytearray(struct.pack("=HhHHHHH2xHH28x", FF_RUMBLE, -1, 0, 0, 0,
                                   int(seconds * 1000), 0, 0xFFFF, 0xFFFF))
    fcntl.ioctl(fd, EVIOCSFF, effect, True)
    effect_id = struct.unpack_from("=h", effect, 2)[0]
    try:
        os.write(fd, struct.pack("=qqHHi", 0, 0, EV_FF, effect_id, 1))
        time.sleep(seconds + 0.1)
        os.write(fd, struct.pack("=qqHHi", 0, 0, EV_FF, effect_id, 0))
    finally:
        fcntl.ioctl(fd, EVIOCRMFF, effect_id)


def act_test_rumble(seconds=0.6):
    """Drive both motors at full strength, straight to the hardware."""
    ctrl = find_controller()
    if not ctrl:
        return _res(False, "controller not found on USB")
    try:
        for event in event_nodes(ctrl):
            if not _has_ff_rumble(event):
                continue
            fd = _open_chardev(event, os.O_RDWR)
            try:
                _ff_pulse(fd, seconds)
            finally:
                os.close(fd)
            return _res(True, "rumble pulse sent via evdev (%s, %s mode)"
                        % (os.path.basename(event), ctrl["pid_mode"]))
        if ctrl["pid"] == "1902":
            nodes = glob.glob(os.path.join(ctrl["path"], ctrl["busid"] + ":1.0",
                                           "0003:*/hidraw/hidraw*"))
            if nodes:
                fd = _open_chardev(nodes[0], os.O_WRONLY)
                try:
                    # DInput rumble report: id 0x05, 0x01, 0, 0, weak, strong.
                    os.write(fd, bytes([0x05, 0x01, 0, 0, 0xFF, 0xFF, 0, 0, 0, 0, 0]))
                    time.sleep(seconds)
                    os.write(fd, bytes([0x05, 0x01, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
                finally:
                    os.close(fd)
                return _res(True, "rumble pulse sent via hidraw (%s, dinput mode)"
                            % os.path.basename(nodes[0]))
    except (OSError, ValueError) as exc:
        return _res(False, "rumble test failed: %s" % exc)
    return _res(False, "no rumble-capable interface found (%s mode)" % ctrl["pid_mode"])


ACTIONS = {
    "resend_mode": act_resend_mode,
    "usb_reset": act_usb_reset,
    "usb_replug": act_usb_replug,
    "restart_inputplumber": act_restart_inputplumber,
    "test_rumble": act_test_rumble,
}


def auto_fix(level, targets):
    """Post-wake sequence for the configured automation level."""
    ctrl = find_controller()
    if ctrl and ctrl["cfg"]:
        # Recorded on every wake so the history shows what the MCU state
        # looked like before rumble was lost.
        _log("state after wake: %s" % describe_state(read_mcu_state(ctrl["cfg"])))
    steps = []
    if level == "mode":
        steps.append(act_resend_mode())
    elif level == "usb":
        steps.append(act_usb_reset())
    elif level == "mcu":
        steps.append(act_mcu_reset())
    steps.append(act_intensity(targets, force=False))
    return _res(all(s["ok"] for s in steps), "; ".join(s["msg"] for s in steps),
                changed=any(s["changed"] for s in steps))


# --------------------------------------------------------------------------
# Kernel reset-resume quirk
# --------------------------------------------------------------------------

def quirk_param_path():
    return os.path.join(SYS, "module/usbcore/parameters/quirks")


def merge_quirks(current, enable):
    """Add or remove our reset-resume entries, keeping foreign entries intact."""
    ours = ["%s:%s:b" % (VENDOR, pid) for pid in sorted(PID_MODES)]
    entries = [e for e in current.strip().split(",") if e and e != "(null)"]
    entries = [e for e in entries if e.lower() not in ours]
    if enable:
        entries += ours
    return ",".join(entries)


def act_quirk(enable):
    ctrl = find_controller()
    try:
        current = _read(quirk_param_path())
    except OSError as exc:
        return _res(False, "usbcore quirks parameter unavailable: %s" % exc)
    present = [e.lower() for e in current.split(",")]
    ours_present = all("%s:%s:b" % (VENDOR, pid) in present for pid in PID_MODES)
    try:
        if not enable:
            if ours_present:
                _write(quirk_param_path(), merge_quirks(current, False))
            return _res(True, "reset-resume quirk entries removed (fully off after reboot)",
                        changed=ours_present)
        if ctrl and quirk_active(ctrl):
            # Already on (built into the kernel or set earlier). The 'b' flag
            # toggles, so adding it on top of a built-in quirk would turn it off.
            return _res(True, "reset-resume quirk already active")
        if not ours_present:
            _write(quirk_param_path(), merge_quirks(current, True))
    except OSError as exc:
        return _res(False, "usbcore quirks write failed: %s" % exc)
    if not ctrl:
        return _res(True, "quirk parameter set; controller not present yet", changed=True)
    # Quirks are evaluated at enumeration time only: force a re-enumeration.
    replug = act_usb_replug()
    after = find_controller()
    if after and quirk_active(after):
        return _res(True, "reset-resume quirk active", changed=True)
    return _res(False, "quirk parameter set but not active on the device (%s)"
                % replug["msg"], changed=True)


# --------------------------------------------------------------------------
# Status and diagnostics
# --------------------------------------------------------------------------

def controller_status(ctrl):
    if not ctrl:
        return None
    cfg = ctrl["cfg"]
    out = {
        "pid": ctrl["pid"], "pid_mode": ctrl["pid_mode"], "busid": ctrl["busid"],
        "bcd": ctrl["bcd"], "quirk_active": quirk_active(ctrl),
        "has_cfg": bool(cfg), "has_rumble_attrs": has_rumble_attrs(cfg),
        "has_reset": bool(cfg) and os.path.exists(os.path.join(cfg, "reset")),
        "gamepad_mode": None, "left": None, "right": None,
    }
    if cfg:
        try:
            out["gamepad_mode"] = _read(os.path.join(cfg, "gamepad_mode"))
        except OSError:
            pass
    if out["has_rumble_attrs"]:
        try:
            out["left"], out["right"] = read_intensity(cfg)
        except (OSError, ValueError):
            pass
    return out


def build_status():
    ctrl = controller_status(find_controller())
    settings = STATE.settings
    # Adopt the hardware intensity as the target the first time we see a sane one.
    if (ctrl and settings["target_left"] is None and ctrl["left"] is not None
            and (ctrl["left"], ctrl["right"]) != (0, 0)):
        settings["target_left"], settings["target_right"] = ctrl["left"], ctrl["right"]
        save_settings()
    raw = find_controller()
    return {
        "controller": ctrl,
        "inputplumber": inputplumber_state(),
        "pad_exposed": bool(raw) and pad_exposed(raw),
        "kernel": os.uname().release,
        "root": os.geteuid() == 0,
        "settings": dict(settings),
        "targets": list(effective_targets(settings)),
        "resume_count": STATE.resume_count,
        "last_result": STATE.last_result,
        "history": ["%s  %s" % (t[11:], m) for t, m in list(STATE.history)[-8:]],
    }


def write_diagnostics():
    lines = ["Claw Rumble Fix diagnostics", "generated: %s" % datetime.now().isoformat(), ""]

    def section(title, body):
        lines.extend(["==== %s ====" % title, body if body else "(empty)", ""])

    section("status", json.dumps(build_status(), indent=2, default=str))
    try:
        section("os-release", _read("/etc/os-release"))
    except OSError as exc:
        section("os-release", str(exc))
    section("uname", " ".join(os.uname()))
    ctrl = find_controller()
    if ctrl:
        dump = []
        for root in [ctrl["path"]] + ([ctrl["cfg"]] if ctrl["cfg"] else []):
            for name in sorted(os.listdir(root)):
                path = os.path.join(root, name)
                # 'reset' is write-only; 'descriptors' is binary.
                if not os.path.isfile(path) or name in ("reset", "descriptors", "uevent"):
                    continue
                try:
                    dump.append("%s/%s = %s" % (os.path.basename(root), name, _read(path)))
                except OSError as exc:
                    dump.append("%s/%s ! %s" % (os.path.basename(root), name, exc))
        section("controller sysfs", "\n".join(dump))
    try:
        section("usbcore quirks", _read(quirk_param_path()))
    except OSError as exc:
        section("usbcore quirks", str(exc))
    _, dmesg = _run_cmd(["dmesg"], 20)
    pattern = re.compile(r"hid-msi|msi|0db0|xpad|usb \d|xhci|PM: |suspend|resume|inputplumber",
                         re.IGNORECASE)
    section("dmesg (filtered, last 400)",
            "\n".join([ln for ln in dmesg.splitlines() if pattern.search(ln)][-400:]))
    _, journal = _run_cmd(["journalctl", "-b", "-u", "inputplumber.service",
                           "--no-pager", "-n", "300"], 30)
    section("inputplumber journal (last 300)", journal)
    section("plugin history", "\n".join("%s  %s" % item for item in STATE.history))

    name = "claw-rumble-fix-diag-%s.txt" % datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(decky.DECKY_USER_HOME, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    try:
        shutil.chown(path, user=decky.DECKY_USER, group=decky.DECKY_USER)
    except (OSError, LookupError):
        pass
    return path


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def settings_path():
    return os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            stored = json.load(f)
        settings.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    if settings["auto_level"] not in AUTO_LEVELS:
        settings["auto_level"] = DEFAULT_SETTINGS["auto_level"]
    STATE.settings = settings


def save_settings():
    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(STATE.settings, f, indent=2)
    except OSError as exc:
        decky.logger.error("failed to save settings: %s" % exc)


# --------------------------------------------------------------------------
# Async glue
# --------------------------------------------------------------------------

def _lock():
    if STATE.lock is None:
        STATE.lock = asyncio.Lock()
    return STATE.lock


async def _locked(func, *args):
    """Run a blocking hardware function, one at a time."""
    async with _lock():
        return await asyncio.to_thread(func, *args)


def suspend_offset():
    """Seconds spent suspended since boot (CLOCK_BOOTTIME keeps running)."""
    boottime = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
    return time.clock_gettime(boottime) - time.clock_gettime(time.CLOCK_MONOTONIC)


async def handle_resume(source, slept=None):
    try:
        await _handle_resume(source, slept)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # never let a failed wake handler go unnoticed
        _log("wake handler failed: %r" % exc)


async def _handle_resume(source, slept):
    now = time.monotonic()
    if now - STATE.last_handled < 20:
        return  # the clock watcher and the frontend both report the same wake
    STATE.last_handled = now
    STATE.resume_count += 1
    count = STATE.resume_count
    _log("wake #%d detected via %s%s" % (
        count, source, "" if slept is None else " (slept %.0fs)" % slept))
    level = STATE.settings["auto_level"]
    if level == "off":
        return
    await asyncio.sleep(float(STATE.settings["resume_delay"]))
    result = await _locked(auto_fix, level, effective_targets(STATE.settings))
    STATE.last_result = "wake #%d: %s" % (count, result["msg"])
    _log(STATE.last_result)
    # Toast on failures, and on repairs the user did not ask for explicitly;
    # the routine reset of the heavier levels would otherwise toast every wake.
    if not result["ok"] or (level == "intensity" and result["changed"]):
        await decky.emit("claw_rumble_fix_result", result["ok"], result["msg"])


async def watch_resume():
    last = suspend_offset()
    while True:
        await asyncio.sleep(2)
        try:
            offset = suspend_offset()
            slept = offset - last
            last = offset
            if slept > 0.5:
                asyncio.ensure_future(handle_resume("clock", slept))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the watcher alive
            decky.logger.error("resume watcher error: %s" % exc)


class Plugin:
    async def _main(self):
        load_settings()
        _log("plugin started, auto level '%s'" % STATE.settings["auto_level"])
        STATE.watch_task = asyncio.ensure_future(watch_resume())
        if STATE.settings["reset_resume_quirk"]:
            result = await _locked(act_quirk, True)
            _log("startup quirk: %s" % result["msg"])

    async def _unload(self):
        if STATE.watch_task:
            STATE.watch_task.cancel()
            STATE.watch_task = None
        _log("plugin unloaded")

    async def _uninstall(self):
        pass

    async def get_status(self):
        return await _locked(build_status)

    async def notify_resume(self):
        asyncio.ensure_future(handle_resume("steam"))

    async def run_action(self, name):
        targets = effective_targets(STATE.settings)
        if name == "intensity":
            result = await _locked(act_intensity, targets, True)
        elif name in ("mcu_reset", "fix_now"):
            result = await _locked(act_mcu_fix, targets, name == "fix_now")
        elif name in ACTIONS:
            result = await _locked(ACTIONS[name])
        else:
            result = _res(False, "unknown action '%s'" % name)
        _log("manual %s: %s" % (name, result["msg"]))
        return result

    async def set_targets(self, left, right):
        left, right = max(0, min(100, int(left))), max(0, min(100, int(right)))
        STATE.settings["target_left"], STATE.settings["target_right"] = left, right
        save_settings()
        result = await _locked(act_intensity, (left, right), True)
        _log("set intensity: %s" % result["msg"])
        return result

    async def set_auto_level(self, level):
        if level not in AUTO_LEVELS:
            return _res(False, "unknown level '%s'" % level)
        STATE.settings["auto_level"] = level
        save_settings()
        _log("auto level set to '%s'" % level)
        return _res(True, "auto level: %s" % level)

    async def set_quirk(self, enable):
        STATE.settings["reset_resume_quirk"] = bool(enable)
        save_settings()
        result = await _locked(act_quirk, bool(enable))
        _log("quirk %s: %s" % ("on" if enable else "off", result["msg"]))
        return result

    async def save_diagnostics(self):
        try:
            path = await _locked(write_diagnostics)
        except OSError as exc:
            return _res(False, "diagnostics failed: %s" % exc)
        _log("diagnostics written to %s" % path)
        return _res(True, path)
