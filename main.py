"""Claw Rumble Fix - Decky Loader backend.

The MSI Claw A8 built-in controller (USB 0db0:190x) can lose rumble after a
suspend/resume cycle on SteamOS: input keeps working, the kernel accepts
rumble commands, the motors stay silent. Why is not known yet. Resetting the
controller MCU brings rumble back, so that is the one thing this plugin does.

The fix button also records the controller state right before and right
after the reset. The "before" record is taken while rumble is still broken,
which is the evidence needed to find the actual cause.

The controller can also be switched between its XInput and DInput modes, to
find out whether the rumble loss depends on the mode.

Python 3.9 compatible, standard library only.
"""
import asyncio
import fcntl
import glob
import os
import re
import select
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

EVIOCSFF = 0x40304580     # _IOW('E', 0x80, struct ff_effect), 48 bytes on x86-64
EVIOCRMFF = 0x40044581    # _IOW('E', 0x81, int)
EV_FF = 0x15
FF_RUMBLE = 0x50

RULES_DIR = "/run/udev/rules.d"
CAPTURE_DIR_NAME = "claw-rumble-captures"
MCU_RAM_TOP = 0x480       # profile RAM that is dumped, in bytes
MCU_CHUNK = 0x10          # usbmon's text interface shows 32 bytes per packet


class State:
    def __init__(self):
        self.lock = None
        self.last_result = None


STATE = State()


def _log(msg):
    decky.logger.info(msg)


def _res(ok, msg, **extra):
    out = {"ok": bool(ok), "msg": msg}
    out.update(extra)
    return out


# --------------------------------------------------------------------------
# Low level helpers
# --------------------------------------------------------------------------

def _read(path):
    with open(path, "r", encoding="ascii", errors="replace") as f:
        return f.read().strip()


def _read_quiet(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


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
        for key, attr in (("busnum", "busnum"), ("devnum", "devnum"), ("bcd", "bcdDevice")):
            try:
                info[key] = _read(os.path.join(dev, attr))
            except OSError:
                info[key] = None
        # hid-msi registers its config attributes on one HID interface only.
        cfg = glob.glob(os.path.join(dev, busid + ":*", "0003:*", "gamepad_mode"))
        info["cfg"] = os.path.dirname(cfg[0]) if cfg else None
        return info
    return None


def event_nodes(ctrl):
    """sysfs paths of every evdev node that belongs to the controller."""
    base = os.path.join(ctrl["path"], ctrl["busid"] + ":*")
    return sorted(glob.glob(os.path.join(base, "input/input*/event*")) +
                  glob.glob(os.path.join(base, "0003:*/input/input*/event*")))


def pad_exposed(ctrl):
    """True when the physical gamepad node is usable by regular users.

    InputPlumber hides the built-in pad (permissions 000) and shows Steam a
    virtual controller instead. If the pad stays visible, Steam lists it as an
    extra "Xbox 360 Controller".
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


def inputplumber_active():
    _, out = _run_cmd(["systemctl", "is-active", "inputplumber.service"], 5)
    return out.splitlines()[:1] == ["active"]


def read_mcu_state(cfg):
    """The settings hid-msi exposes (None where unreadable)."""
    state = {}
    for name in ("gamepad_mode", "mkeys_function",
                 "rumble_intensity_left", "rumble_intensity_right"):
        try:
            state[name] = _read(os.path.join(cfg, name))
        except (OSError, TypeError):
            state[name] = None
    return state


def describe_state(state):
    return "mode=%s mkeys=%s intensity=L%s/R%s" % (
        state["gamepad_mode"], state["mkeys_function"],
        state["rumble_intensity_left"], state["rumble_intensity_right"])


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
    """Open the character device behind a /sys/class-style node directory."""
    name = os.path.basename(sys_node)
    major, minor = _read(os.path.join(sys_node, "dev")).split(":")
    rdev = os.makedev(int(major), int(minor))
    for directory in (os.path.join(DEV, "input"), DEV):
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
# Rumble pulse, straight to the hardware
# --------------------------------------------------------------------------

def _has_ff_rumble(event_node):
    try:
        text = _read(os.path.join(event_node, "device/capabilities/ff"))
    except OSError:
        return False
    bits = 0
    for word in text.split():  # highest machine word first
        bits = (bits << 64) | int(word, 16)
    return bool(bits & (1 << FF_RUMBLE))


def rumble_pulse(seconds=0.6):
    """Drive both motors at full strength. Returns a description, raises OSError."""
    ctrl = find_controller()
    if not ctrl:
        raise OSError("controller not found on USB")
    for event in event_nodes(ctrl):
        if not _has_ff_rumble(event):
            continue
        fd = _open_chardev(event, os.O_RDWR)
        try:
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
        finally:
            os.close(fd)
        return "evdev %s (%s mode)" % (os.path.basename(event), ctrl["pid_mode"])
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
            return "hidraw %s (dinput mode)" % os.path.basename(nodes[0])
    raise OSError("no rumble-capable interface found (%s mode)" % ctrl["pid_mode"])


# --------------------------------------------------------------------------
# State capture (read-only, apart from one rumble pulse)
# --------------------------------------------------------------------------
#
# Three layers can swallow a rumble command; each leaves different evidence:
#   kernel  - the rumble packet never reaches the USB wire      -> usbmon
#   MCU     - a value in the controller's profile RAM changed   -> RAM dump
#   EC      - the embedded controller left the pad in a bad
#             power state                                       -> EC RAM dump

class UsbMon:
    """Text usbmon reader for the controller's bus."""

    def __init__(self, ctrl):
        _run_cmd(["modprobe", "usbmon"], 10)
        self.bus, self.dev = int(ctrl["busnum"]), int(ctrl["devnum"])
        self.tag = "%d:%03d:" % (self.bus, self.dev)
        self.fd = os.open(os.path.join(SYS, "kernel/debug/usb/usbmon/%du" % self.bus),
                          os.O_RDONLY | os.O_NONBLOCK)
        self.buf = b""

    def lines(self, timeout):
        """Yield the controller's usbmon lines as they arrive, for `timeout` seconds."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not select.select([self.fd], [], [], 0.02)[0]:
                continue
            try:
                self.buf += os.read(self.fd, 65536)
            except BlockingIOError:
                continue
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                line = raw.decode("ascii", "replace")
                if self.tag in line:
                    yield line

    def close(self):
        os.close(self.fd)


def dump_mcu_ram(ctrl):
    """Read the controller's profile RAM. Returns (bytes, unanswered chunk count).

    hid-msi swallows read replies before they reach hidraw, so the requests go
    out through hidraw and the replies are picked up from usbmon.
    """
    nodes = glob.glob(os.path.join(ctrl["cfg"] or "", "hidraw/hidraw*"))
    if not nodes:
        raise OSError("config hidraw node not found")
    mon = UsbMon(ctrl)
    fd = _open_chardev(nodes[0], os.O_RDWR)
    out, missing = bytearray(), 0
    try:
        for addr in range(0, MCU_RAM_TOP, MCU_CHUNK):
            data = None
            for _ in range(3):
                os.write(fd, bytes([0x0F, 0, 0, 0x3C, 0x04, 0x01, addr >> 8, addr & 0xFF,
                                    MCU_CHUNK]).ljust(64, b"\0"))
                for line in mon.lines(0.3):
                    fields = line.split()
                    if len(fields) < 8 or fields[2] != "C" or not fields[3].startswith("Ii:") \
                            or "=" not in fields:
                        continue
                    try:
                        reply = bytes.fromhex("".join(fields[fields.index("=") + 1:]))
                    except ValueError:
                        continue
                    if (len(reply) >= 9 + MCU_CHUNK and reply[0] == 0x10 and reply[4] == 0x05
                            and (reply[6] << 8 | reply[7]) == addr):
                        data = reply[9:9 + MCU_CHUNK]
                        break
                if data is not None:
                    break
                time.sleep(0.05)
            if data is None:
                missing += 1
                data = b"\xEE" * MCU_CHUNK
            out += data
            time.sleep(0.01)
    finally:
        os.close(fd)
        mon.close()
    return bytes(out), missing


def dump_ec_ram():
    _run_cmd(["modprobe", "ec_sys"], 10)
    with open(os.path.join(SYS, "kernel/debug/ec/ec0/io"), "rb") as f:
        return f.read()


def pulse_usb_trace(ctrl):
    """Fire a rumble pulse and return what went over USB for it."""
    mon = UsbMon(ctrl)
    try:
        list(mon.lines(0.05))  # start reading before the pulse
        how = rumble_pulse(0.6)
        lines = [ln for ln in mon.lines(0.3)
                 if " Ii:" not in ln]  # interrupt-IN lines are stick/button reports
    finally:
        mon.close()
    return how, lines


def hexdump(data):
    rows = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        rows.append("%04x: %s" % (offset, " ".join("%02x" % b for b in chunk)))
    return "\n".join(rows)


def capture(folder, tag):
    """Record the controller state into folder/<tag>.*; never raises."""
    lines = ["Claw Rumble Fix capture: %s" % tag, "time: %s" % datetime.now().isoformat(), ""]

    def section(title, body):
        lines.extend(["==== %s ====" % title, body if body else "(empty)", ""])

    def attempt(title, func):
        try:
            section(title, func())
        except Exception as exc:  # a capture must never block the fix
            section(title, "FAILED: %r" % exc)

    ctrl = find_controller()
    if not ctrl:
        section("controller", "not found on USB")
    else:
        section("controller", "busid=%s devnum=%s pid=%s bcd=%s exposed=%s" % (
            ctrl["busid"], ctrl["devnum"], ctrl["pid"], ctrl["bcd"], pad_exposed(ctrl)))
        section("hid-msi settings", describe_state(read_mcu_state(ctrl["cfg"])))

        def usb():
            how, trace = pulse_usb_trace(ctrl)
            return "pulse via %s\n%s" % (how, "\n".join(trace) or
                                         "NO OUTPUT PACKET ON THE WIRE")
        attempt("usb traffic during a rumble pulse", usb)

        def mcu():
            data, missing = dump_mcu_ram(ctrl)
            with open(os.path.join(folder, tag + ".mcu"), "wb") as f:
                f.write(data)
            return "unanswered chunks: %d\n%s" % (missing, hexdump(data))
        attempt("controller MCU profile RAM", mcu)

    def ec():
        data = dump_ec_ram()
        with open(os.path.join(folder, tag + ".ec"), "wb") as f:
            f.write(data)
        return hexdump(data)
    attempt("EC RAM", ec)

    pattern = re.compile(r"hid-msi|0db0|xpad|usb 3-|PM: suspend|inputplumber", re.IGNORECASE)
    _, kernel = _run_cmd(["journalctl", "-k", "-b", "-o", "short-precise", "--no-pager"], 30)
    section("kernel log (filtered, last 300)",
            "\n".join([ln for ln in kernel.splitlines() if pattern.search(ln)][-300:]))
    _, journal = _run_cmd(["journalctl", "-b", "-u", "inputplumber.service",
                           "--no-pager", "-n", "60"], 30)
    section("inputplumber journal (last 60)", journal)
    section("/proc/bus/input/devices", _read_quiet("/proc/bus/input/devices"))
    section("uname", " ".join(os.uname()))
    try:
        with open(os.path.join(folder, tag + ".txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except OSError as exc:
        _log("capture %s not written: %s" % (tag, exc))


def new_capture_folder():
    folder = os.path.join(decky.DECKY_USER_HOME, CAPTURE_DIR_NAME,
                          datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(folder, exist_ok=True)
    return folder


def hand_over(folder):
    """Captures are written as root; give them to the user."""
    top = os.path.dirname(folder)
    for path in [top, folder] + glob.glob(os.path.join(folder, "*")):
        try:
            shutil.chown(path, user=decky.DECKY_USER, group=decky.DECKY_USER)
        except (OSError, LookupError):
            pass


# --------------------------------------------------------------------------
# MCU reset
# --------------------------------------------------------------------------
#
# InputPlumber hides the physical pad with udev rules that name one specific
# input device (KERNELS=="input31"). After an MCU reset the pad comes back as
# a new input device, visible until InputPlumber has written new rules; Steam
# can grab it in that window and then lists an extra "Xbox 360 Controller".
# These rules keep the pad hidden from the moment its nodes are created. They
# exist only for the duration of a reset.

_HIDE_MATCH = ('ACTION=="add|change", SUBSYSTEM=="input", KERNEL=="js[0-9]*|event[0-9]*", '
               'ATTRS{id/vendor}=="0db0", ATTRS{id/product}=="190[1-4]", '
               'ATTRS{capabilities/abs}!="0", ')
HIDE_RULES = {
    "50-claw-rumble-fix-hide-pad-early.rules":
        _HIDE_MATCH + 'MODE:="0000", GROUP:="root", RUN+="/usr/bin/chmod 000 /dev/input/%k"\n',
    "96-claw-rumble-fix-hide-pad-late.rules":
        _HIDE_MATCH + 'MODE="000", GROUP="root", TAG-="uaccess", '
                      'RUN+="/usr/bin/chmod 000 /dev/input/%k"\n',
}


def set_hide_rules(enable):
    changed = False
    try:
        if enable:
            os.makedirs(RULES_DIR, exist_ok=True)
        for name, text in HIDE_RULES.items():
            path = os.path.join(RULES_DIR, name)
            if enable:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("# Temporary, written by Claw Rumble Fix during an MCU reset.\n" + text)
                changed = True
            elif os.path.exists(path):
                os.unlink(path)
                changed = True
    except OSError as exc:
        _log("pad hide rules: %s" % exc)
    if changed:
        _run_cmd(["udevadm", "control", "--reload"], 15)


def wait_inputplumber_rehide(timeout=8.0):
    """Wait until InputPlumber's own hide rules cover the re-enumerated pad."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ctrl = find_controller()
        if ctrl and event_nodes(ctrl) and not pad_exposed(ctrl):
            inputs = {os.path.basename(os.path.dirname(e)) for e in event_nodes(ctrl)}
            rules = " ".join(_read_quiet(p) for p in glob.glob(
                os.path.join(RULES_DIR, "*inputplumber-hide-*")))
            if any('KERNELS=="%s"' % name in rules for name in inputs):
                return True
        time.sleep(0.5)
    return False


def mcu_reset():
    """Reset the controller MCU (hid-msi 'reset' attribute).

    The reset may bring the MCU back with different settings, so the gamepad
    mode and the back-button mode are captured first and put back afterwards.
    """
    ctrl = find_controller()
    if not ctrl:
        return _res(False, "controller not found on USB")
    path = os.path.join(ctrl["cfg"] or "", "reset")
    if not os.path.exists(path):
        return _res(False, "kernel driver exposes no reset attribute")
    before = read_mcu_state(ctrl["cfg"])
    keep_hidden = not pad_exposed(ctrl) and inputplumber_active()
    if keep_hidden:
        set_hide_rules(True)
    try:
        result = _mcu_reset(ctrl, path, before)
        if keep_hidden and not wait_inputplumber_rehide():
            current = find_controller()
            if current and pad_exposed(current):
                result["msg"] += ("; the physical pad is visible, Steam may list an extra "
                                  "Xbox 360 controller until reboot")
            else:
                _log("InputPlumber did not write new hide rules after the reset")
        return result
    finally:
        if keep_hidden:
            set_hide_rules(False)


def _mcu_reset(ctrl, path, before):
    try:
        _write(path, 1)
    except OSError as exc:
        return _res(False, "MCU reset write failed: %s" % exc)
    if not wait_reenumerated(ctrl["devnum"], 12.0):
        return _res(False, "MCU reset sent, no USB re-enumeration observed")
    back = wait_cfg()
    if not back or not back["cfg"]:
        return _res(True, "MCU reset done, config interface not back yet")
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
    return _res(True, "MCU reset done" + ("; " + "; ".join(notes) if notes else ""))


def set_gamepad_mode(mode):
    """Switch the controller between XInput and DInput; it re-enumerates on USB."""
    if mode not in SWITCHABLE_MODES:
        return _res(False, "unknown gamepad mode '%s'" % mode)
    ctrl = find_controller()
    if not ctrl:
        return _res(False, "controller not found on USB")
    if not ctrl["cfg"]:
        return _res(False, "kernel driver exposes no gamepad_mode attribute")
    keep_hidden = not pad_exposed(ctrl) and inputplumber_active()
    if keep_hidden:
        set_hide_rules(True)
    try:
        try:
            _write(os.path.join(ctrl["cfg"], "gamepad_mode"), mode)
        except OSError as exc:
            return _res(False, "gamepad_mode write failed: %s" % exc)
        wait_reenumerated(ctrl["devnum"], 8.0)
        back = wait_cfg()
        time.sleep(0.5)
        try:
            now = _read(os.path.join(back["cfg"], "gamepad_mode"))
        except (OSError, TypeError):
            now = None
        if now != mode:
            return _res(False, "controller reports mode '%s' after switching to '%s'"
                        % (now, mode))
        result = _res(True, "gamepad mode is now '%s'" % mode)
        if keep_hidden and not wait_inputplumber_rehide():
            current = find_controller()
            if current and pad_exposed(current):
                result["msg"] += ("; the physical pad is visible, Steam may list an extra "
                                  "controller until reboot")
        return result
    finally:
        if keep_hidden:
            set_hide_rules(False)


def fix_rumble():
    """Capture the broken state, reset the MCU, capture again (ends with a pulse)."""
    folder = new_capture_folder()
    capture(folder, "1-before-reset")
    result = mcu_reset()
    if result["ok"]:
        time.sleep(1.0)
    capture(folder, "2-after-reset")
    hand_over(folder)
    result["capture"] = folder
    return result


def build_status():
    ctrl = find_controller()
    captures = sorted(glob.glob(os.path.join(decky.DECKY_USER_HOME, CAPTURE_DIR_NAME, "*")))
    return {
        "controller": None if not ctrl else {
            "pid_mode": ctrl["pid_mode"], "bcd": ctrl["bcd"],
            "can_switch": bool(ctrl["cfg"]),
            "can_reset": bool(ctrl["cfg"]) and os.path.exists(os.path.join(ctrl["cfg"], "reset")),
        },
        "last_result": STATE.last_result,
        "captures": len(captures),
        "capture_dir": os.path.join("~", CAPTURE_DIR_NAME),
    }


# --------------------------------------------------------------------------
# Decky glue
# --------------------------------------------------------------------------

async def _locked(func, *args):
    """Run a blocking hardware function, one at a time."""
    if STATE.lock is None:
        STATE.lock = asyncio.Lock()
    async with STATE.lock:
        return await asyncio.to_thread(func, *args)


class Plugin:
    async def _main(self):
        # Leftovers of earlier versions (both live in /run and die with a reboot).
        stale = [os.path.join(RULES_DIR, "99-inputplumber-device-setup.rules")]
        stale += [os.path.join(RULES_DIR, name) for name in HIDE_RULES]
        removed = False
        for path in stale:
            if os.path.exists(path):
                try:
                    os.unlink(path)
                    removed = True
                except OSError:
                    pass
        if removed:
            await asyncio.to_thread(_run_cmd, ["udevadm", "control", "--reload"], 15)
        _log("plugin started")

    async def _unload(self):
        _log("plugin unloaded")

    async def _uninstall(self):
        pass

    async def get_status(self):
        return await _locked(build_status)

    async def set_mode(self, mode):
        try:
            result = await _locked(set_gamepad_mode, mode)
        except Exception as exc:
            result = _res(False, "mode switch failed: %r" % exc)
        _log("set mode %s: %s" % (mode, result["msg"]))
        return result

    async def fix(self):
        try:
            result = await _locked(fix_rumble)
        except Exception as exc:  # report instead of leaving the button spinning
            result = _res(False, "fix failed: %r" % exc)
        STATE.last_result = "%s  %s" % (datetime.now().strftime("%H:%M:%S"), result["msg"])
        _log("fix: %s (capture: %s)" % (result["msg"], result.get("capture")))
        return result
