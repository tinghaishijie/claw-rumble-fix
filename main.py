"""Claw Rumble Fix - Decky Loader backend.

The MSI Claw A8 built-in controller (USB 0db0:190x) can lose rumble after a
number of suspend/resume cycles on SteamOS. Findings on real hardware:

  * the USB link, the gamepad mode and the stored rumble intensity are all
    intact while rumble is dead; only a controller MCU reset revives it
    (re-sending settings, USB resets and restarting InputPlumber do not);
  * something else on the system talks to the controller MCU behind the
    kernel driver's back after every wake-up (seen: the HueSync plugin's raw
    RGB commands). Whether that is what breaks rumble is not established.

So this backend resets the MCU on request, records what the reset changed in
the MCU's profile memory, and keeps a timeline of everything that writes to
the controller.

Everything here is Python 3.9 compatible and uses the standard library only.
"""
import asyncio
import collections
import errno
import fcntl
import glob
import json
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

# Window after a wake-up in which unexpected MCU ACKs are counted.
WAKE_WINDOW = 45.0
# Optional rumble check after a wake-up: a short pulse the user can feel.
WAKE_BUZZ_DELAY = 2.0
WAKE_BUZZ_SECONDS = 0.25
DEFAULT_SETTINGS = {"wake_buzz": False}


class State:
    def __init__(self):
        self.settings = dict(DEFAULT_SETTINGS)
        self.history = collections.deque(maxlen=300)
        self.resume_count = 0
        self.last_handled = -1e9
        self.last_wake = None
        self.lock = None
        self.watch_task = None


STATE = State()


def _log(msg):
    STATE.history.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    TRACE.add("plugin: " + msg)
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
# Controller actions (blocking; run in a worker thread)
# --------------------------------------------------------------------------

def act_mcu_reset():
    """Reset the controller MCU (hid-msi 'reset' attribute).

    This is the software counterpart of the EC reset and the only action seen
    to bring rumble back. The reset may bring the MCU up with other settings,
    so they are captured first and written back when they differ.
    """
    ctrl = find_controller()
    if not ctrl:
        return _res(False, "controller not found on USB")
    path = os.path.join(ctrl["cfg"] or "", "reset")
    if not os.path.exists(path):
        return _res(False, "kernel driver exposes no reset attribute")
    before = read_mcu_state(ctrl["cfg"])
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
    for name in ("rumble_intensity_left", "rumble_intensity_right"):
        old, new = before[name], after[name]
        if old is None or new is None or new == old or not old.isdigit():
            continue
        try:
            _write(os.path.join(back["cfg"], name), old)
            time.sleep(0.5)  # let the flash commit retire before the next command
            notes.append("%s restored %s -> %s" % (name, new, old))
        except OSError as exc:
            notes.append("%s restore failed: %s" % (name, exc))
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
    return _res(True, "MCU reset done" + ("; " + "; ".join(notes) if notes else ""),
                changed=True)


def act_fix_rumble():
    """MCU reset followed by a test pulse so the result can be felt.

    The MCU profile memory is captured before and after: if the reset repairs
    rumble by reloading that memory from flash, the difference shows which
    bytes were wrong.
    """
    before, before_error = capture_profile()
    reset = act_mcu_reset()
    if not reset["ok"]:
        return reset
    after, after_error = capture_profile()
    record = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "before": before.hex() if before else None,
              "after": after.hex() if after else None,
              "error": before_error or after_error}
    save_fix_record(record)
    if before and after:
        ranges = diff_profiles(before, after)
        note = ("profile memory unchanged by the reset" if not ranges else
                "reset changed %d profile range(s): %s"
                % (len(ranges), ", ".join(r.split(":")[0] for r in ranges[:6])))
        for line in ranges:
            _log("profile diff " + line)
    else:
        note = "profile memory not captured (%s)" % (before_error or after_error)
    pulse = act_test_rumble()
    return _res(pulse["ok"], "%s; %s; %s" % (reset["msg"], note, pulse["msg"]), changed=True)


# --------------------------------------------------------------------------
# MCU profile memory
# --------------------------------------------------------------------------
#
# The MCU keeps its configuration (button mappings, rumble intensity, RGB
# frames, ...) in a "profile" that the kernel driver reads and writes in
# small pieces. Reading it does not change anything. The request is
#   0f 00 00 3c 04 <profile> <addr hi> <addr lo> <length>
# and the reply, on the same interface,
#   10 00 00 3c 05 <profile> <addr hi> <addr lo> <length> <data...>

PROFILE_ID = 0x01
PROFILE_END = 0x0340      # past the last RGB frame (0x024a + 8 frames)
PROFILE_CHUNK = 0x20
PACKET_SIZE = 64


def profile_request(addr, length):
    return bytes([0x0F, 0x00, 0x00, 0x3C, 0x04, PROFILE_ID,
                  (addr >> 8) & 0xFF, addr & 0xFF, length]).ljust(PACKET_SIZE, b"\0")


def parse_profile_reply(report, addr):
    """Payload of a READ_PROFILE reply for addr, or None for any other report."""
    if (len(report) < 9 or report[0] != 0x10 or report[3] != 0x3C or report[4] != 0x05
            or report[5] != PROFILE_ID or report[6] != (addr >> 8) & 0xFF
            or report[7] != addr & 0xFF):
        return None
    return bytes(report[9:9 + report[8]])


def read_profile_fd(fd, end=PROFILE_END, timeout=0.3):
    """Read profile memory [0, end) through an open hidraw fd."""
    while select.select([fd], [], [], 0)[0]:   # drop stale input reports
        os.read(fd, PACKET_SIZE)
    data = bytearray()
    for addr in range(0, end, PROFILE_CHUNK):
        chunk = None
        for _attempt in range(2):
            os.write(fd, profile_request(addr, PROFILE_CHUNK))
            deadline = time.monotonic() + timeout
            while chunk is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    break
                chunk = parse_profile_reply(os.read(fd, PACKET_SIZE), addr)
            if chunk is not None:
                break
        if chunk is None or len(chunk) != PROFILE_CHUNK:
            raise OSError("no valid reply for profile address 0x%04x" % addr)
        data.extend(chunk)
    return bytes(data)


def capture_profile():
    """(profile bytes, None) or (None, reason). Never raises."""
    try:
        ctrl = find_controller()
        if not ctrl or not ctrl["cfg"]:
            return None, "controller config interface not found"
        nodes = sorted(glob.glob(os.path.join(ctrl["cfg"], "hidraw/hidraw*")))
        if not nodes:
            return None, "no hidraw node for the config interface"
        fd = _open_chardev(nodes[0], os.O_RDWR | os.O_NONBLOCK)
        try:
            return read_profile_fd(fd), None
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        return None, str(exc)


def diff_profiles(old, new):
    """Differing byte ranges as 'start-end: old -> new' lines."""
    lines, start = [], None
    length = min(len(old), len(new))
    for index in range(length + 1):
        differs = index < length and old[index] != new[index]
        if differs and start is None:
            start = index
        elif not differs and start is not None:
            a, b = old[start:index], new[start:index]
            cut_at = 24
            lines.append("0x%04x-0x%04x: %s%s -> %s%s" % (
                start, index - 1, a[:cut_at].hex(" "), " ..." if len(a) > cut_at else "",
                b[:cut_at].hex(" "), " ..." if len(b) > cut_at else ""))
            start = None
    return lines


def hexdump(data):
    return "\n".join("%04x  %s" % (offset, data[offset:offset + 16].hex(" "))
                     for offset in range(0, len(data), 16))


def fix_record_path():
    return os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "fix-records.json")


def load_fix_records():
    try:
        with open(fix_record_path(), "r", encoding="utf-8") as f:
            records = json.load(f)
        return records if isinstance(records, list) else []
    except (OSError, ValueError):
        return []


def save_fix_record(record):
    records = (load_fix_records() + [record])[-10:]
    try:
        os.makedirs(os.path.dirname(fix_record_path()), exist_ok=True)
        with open(fix_record_path(), "w", encoding="utf-8") as f:
            json.dump(records, f)
    except OSError as exc:
        decky.logger.error("failed to save fix record: %s" % exc)


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


# --------------------------------------------------------------------------
# Kernel log
# --------------------------------------------------------------------------

RULES_OVERRIDE_DIR = "/run/udev/rules.d"
OVERRIDE_TAG = "Generated by Claw Rumble Fix"
ACK_MESSAGE = "unexpected ACK from MCU"


def remove_old_overrides():
    """Remove udev overrides left by versions before 0.5 (they had no effect)."""
    removed = 0
    for path in glob.glob(os.path.join(RULES_OVERRIDE_DIR, "*.rules")):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                ours = OVERRIDE_TAG in f.readline()
            if ours:
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    if removed:
        _run_cmd(["udevadm", "control", "--reload"], 15)
    return removed


def count_unexpected_acks():
    """'unexpected ACK' lines since boot: commands sent to the MCU behind the
    kernel driver's back, plus the driver's own flash commits (None if unknown)."""
    code, out = _run_cmd(["dmesg"], 15)
    if code != 0:
        return None
    return sum(1 for line in out.splitlines() if ACK_MESSAGE in line)


# --------------------------------------------------------------------------
# Activity trace: what touches the controller, and when
# --------------------------------------------------------------------------
#
# One timeline of (a) writes to the controller's sysfs attributes together
# with the process that made them, (b) udev events on the controller's HID
# devices, (c) hid-msi and suspend messages from the kernel log and (d) this
# plugin's own log. It answers "who commits the controller flash".

PROC = "/proc"
KMSG_PATTERN = re.compile(r"hid-msi|PM: suspend (entry|exit)")
FAN_EVENT = struct.Struct("=IBBHQii")   # struct fanotify_event_metadata
FAN_CLOEXEC = 0x1
FAN_NONBLOCK = 0x2
FAN_MARK_ADD = 0x1
FAN_MODIFY = 0x2
AT_FDCWD = -100


def parse_fan_events(data):
    """Split a fanotify read buffer into (mask, fd, pid) tuples."""
    events, offset = [], 0
    while offset + FAN_EVENT.size <= len(data):
        length, _vers, _reserved, _meta, mask, fd, pid = FAN_EVENT.unpack_from(data, offset)
        if length < FAN_EVENT.size:
            break
        events.append((mask, fd, pid))
        offset += length
    return events


def parse_kmsg(record):
    """Message text of one /dev/kmsg record ("prio,seq,time,flags;message")."""
    return record.decode("utf-8", "replace").partition(";")[2].split("\n", 1)[0]


def parse_udev_block(lines):
    """(action, devpath, synthetic) for one 'udevadm monitor --property' block."""
    match = re.match(r"^\w+\[[\d.]+\]\s+(\S+)\s+(\S+)\s+\(\S+\)", lines[0]) if lines else None
    if not match:
        return None
    return (match.group(1), match.group(2),
            any(line.startswith("SYNTH_UUID=") for line in lines[1:]))


def describe_pid(pid):
    """(short, long) description of a process; survives the process being gone."""
    if pid == os.getpid():
        return "this plugin", "this plugin"

    def proc(target, name):
        try:
            with open(os.path.join(PROC, str(target), name), "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return None

    comm = proc(pid, "comm")
    if comm is None:
        return "exited process", "pid %d (already exited)" % pid
    comm = comm.strip()
    cmdline = (proc(pid, "cmdline") or "").replace("\0", " ").strip()[:120]
    parent = "?"
    match = re.search(r"^PPid:\s+(\d+)", proc(pid, "status") or "", re.MULTILINE)
    if match:
        parent = (proc(match.group(1), "comm") or "?").strip()
    return ("%s (started by %s)" % (comm, parent),
            "%s[%d] cmd='%s' parent=%s" % (comm, pid, cmdline, parent))


def trace_targets(ctrl):
    """Writable sysfs attributes of the controller's config and LED devices.

    Raw commands sent through /dev/hidraw* cannot be watched this way: the
    kernel delivers no fanotify events for character devices. Those show up
    as "unexpected ACK" lines from hid-msi instead.
    """
    if not ctrl or not ctrl["cfg"]:
        return set()
    targets = set()
    for directory in [ctrl["cfg"]] + sorted(glob.glob(os.path.join(ctrl["cfg"], "leds/*"))):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_mode & 0o222:
                targets.add((path, st.st_ino))
    return targets


class Trace:
    udev_subsystem = "hid"

    def __init__(self):
        self.events = collections.deque(maxlen=800)
        self.writers = collections.Counter()
        self.notes = []
        self.loop = None
        self.libc = None
        self.fan_fd = None
        self.kmsg_fd = None
        self.udev_proc = None
        self.udev_task = None
        self.marked = set()

    # -- timeline ----------------------------------------------------------

    def add(self, msg):
        now = time.time()
        if self.events and self.events[-1][1] == msg and now - self.events[-1][0] < 2:
            self.events[-1][2] += 1
            return
        self.events.append([now, msg, 1])
        if not msg.startswith("plugin: "):  # those are logged by _log() already
            decky.logger.info("trace: " + msg)  # survives a plugin restart

    def lines(self):
        out = []
        for stamp, msg, count in self.events:
            when = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            out.append("%s  %s%s" % (when, msg, "  (x%d)" % count if count > 1 else ""))
        return out

    def top_writers(self, limit=6):
        return ["%dx %s" % (count, who) for who, count in self.writers.most_common(limit)]

    # -- sources -----------------------------------------------------------

    def start(self, loop):
        self.loop = loop
        for name, starter in (("write watcher", self._start_fanotify),
                              ("kernel log watcher", self._start_kmsg)):
            try:
                starter()
            except Exception as exc:  # ImportError, OSError, AttributeError...
                self.notes.append("%s unavailable: %s" % (name, exc))
                self.add("%s unavailable: %s" % (name, exc))
        self.udev_task = asyncio.ensure_future(self._run_udev_monitor())
        self.refresh()

    def stop(self):
        for fd in (self.fan_fd, self.kmsg_fd):
            if fd is not None:
                try:
                    self.loop.remove_reader(fd)
                    os.close(fd)
                except (OSError, ValueError):
                    pass
        self.fan_fd = self.kmsg_fd = None
        if self.udev_task:
            self.udev_task.cancel()
        if self.udev_proc and self.udev_proc.returncode is None:
            try:
                self.udev_proc.terminate()
            except ProcessLookupError:
                pass

    def _start_fanotify(self):
        import ctypes  # not guaranteed to be in Decky's bundled Python
        libc = ctypes.CDLL(None, use_errno=True)
        libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
        libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64,
                                       ctypes.c_int, ctypes.c_char_p]
        fd = libc.fanotify_init(FAN_CLOEXEC | FAN_NONBLOCK, os.O_RDONLY | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "fanotify_init failed")
        self.libc, self.fan_fd, self._errno = libc, fd, ctypes.get_errno
        self.loop.add_reader(fd, self._on_fanotify)

    def refresh(self):
        """(Re)mark the controller attributes; they are recreated on re-enumeration."""
        if self.fan_fd is None:
            return
        targets = trace_targets(find_controller())
        if targets == self.marked:
            return
        failed = 0
        for path, _inode in sorted(targets - self.marked):
            if self.libc.fanotify_mark(self.fan_fd, FAN_MARK_ADD, FAN_MODIFY, AT_FDCWD,
                                       path.encode()) != 0:
                failed += 1
                self.add("cannot watch %s: errno %d" % (os.path.basename(path), self._errno()))
        self.marked = targets
        self.add("watching %d controller attribute(s) for writes%s"
                 % (len(targets) - failed, ", %d failed" % failed if failed else ""))

    def _on_fanotify(self):
        try:
            data = os.read(self.fan_fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            self.add("write watcher stopped: %s" % exc)
            self.loop.remove_reader(self.fan_fd)
            return
        for mask, fd, pid in parse_fan_events(data):
            if fd < 0:
                continue
            try:
                path = os.readlink("/proc/self/fd/%d" % fd)
            except OSError:
                path = "?"
            finally:
                os.close(fd)
            if not mask & FAN_MODIFY:
                continue
            name = os.path.basename(path)
            if os.path.basename(os.path.dirname(os.path.dirname(path))) == "leds":
                name = "led/" + name
            short, full = describe_pid(pid)
            self.add("write %s by %s" % (name, full))
            self.writers["%s <- %s" % (name, short)] += 1

    def _start_kmsg(self):
        fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
        os.lseek(fd, 0, os.SEEK_END)
        self.kmsg_fd = fd
        self.loop.add_reader(fd, self._on_kmsg)

    def _on_kmsg(self):
        for _ in range(200):
            try:
                record = os.read(self.kmsg_fd, 8192)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno == errno.EPIPE:  # records were overwritten; keep going
                    continue
                self.add("kernel log watcher stopped: %s" % exc)
                self.loop.remove_reader(self.kmsg_fd)
                return
            message = parse_kmsg(record)
            if KMSG_PATTERN.search(message):
                self.add("kernel: " + message)

    async def _run_udev_monitor(self):
        env = dict(os.environ)
        env.pop("LD_LIBRARY_PATH", None)
        try:
            self.udev_proc = await asyncio.create_subprocess_exec(
                "udevadm", "monitor", "--kernel", "--property",
                "--subsystem-match=" + self.udev_subsystem,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env)
        except OSError as exc:
            self.notes.append("udev monitor unavailable: %s" % exc)
            return
        block = []
        while True:
            raw = await self.udev_proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line:
                block.append(line)
                continue
            self.on_udev_block(block)
            block = []
        self.add("udev monitor ended")

    def on_udev_block(self, block):
        parsed = parse_udev_block(block)
        if parsed and VENDOR.upper() in parsed[1].upper():
            action, devpath, synthetic = parsed
            self.add("udev %s %s%s" % (action, os.path.basename(devpath),
                                       " (synthetic)" if synthetic else ""))


TRACE = Trace()


# --------------------------------------------------------------------------
# Status and diagnostics
# --------------------------------------------------------------------------

def build_status():
    ctrl = find_controller()
    controller = None
    if ctrl:
        state = read_mcu_state(ctrl["cfg"]) if ctrl["cfg"] else {}
        controller = {
            "pid": ctrl["pid"], "pid_mode": ctrl["pid_mode"], "bcd": ctrl["bcd"],
            "has_reset": bool(ctrl["cfg"]) and os.path.exists(os.path.join(ctrl["cfg"], "reset")),
            "state": state,
        }
    return {
        "controller": controller,
        "inputplumber": inputplumber_state(),
        "kernel": os.uname().release,
        "root": os.geteuid() == 0,
        "writers": TRACE.top_writers(),
        "trace_notes": list(TRACE.notes),
        "unexpected_acks": count_unexpected_acks(),
        "resume_count": STATE.resume_count,
        "last_wake": STATE.last_wake,
        "wake_buzz": bool(STATE.settings["wake_buzz"]),
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
        nodes = []
        for event in event_nodes(ctrl):
            entry = [os.path.basename(event)]
            for attr in ("device/name", "device/capabilities/abs", "device/capabilities/ff"):
                try:
                    entry.append("%s=%s" % (attr.rsplit("/", 1)[-1], _read(os.path.join(event, attr))))
                except OSError:
                    pass
            try:
                st = os.stat(os.path.join(DEV, "input", os.path.basename(event)))
                entry.append("node mode=%o uid=%d gid=%d" % (stat.S_IMODE(st.st_mode),
                                                              st.st_uid, st.st_gid))
            except OSError as exc:
                entry.append("node: %s" % exc)
            nodes.append("  ".join(entry))
        section("controller evdev nodes (pad_exposed=%s)" % pad_exposed(ctrl), "\n".join(nodes))
    # What the rest of the system sees: needed to tell whether the physical
    # pad is hidden behind InputPlumber's virtual controller.
    try:
        section("/proc/bus/input/devices", _read("/proc/bus/input/devices"))
    except OSError as exc:
        section("/proc/bus/input/devices", str(exc))
    for title, args in (
            ("ls -l /dev/input", ["ls", "-l", "/dev/input"]),
            ("ls -l /dev/inputplumber/sources", ["ls", "-l", "/dev/inputplumber/sources"]),
            ("ls -l /dev/hidraw*", ["sh", "-c", "ls -l /dev/hidraw*"]),
            ("inputplumber version", ["sh", "-c", "inputplumber --version; pacman -Q inputplumber"]),
            ("inputplumber dbus tree", ["busctl", "tree", "org.shadowblip.InputPlumber"]),
            ("inputplumber hide rules", ["sh", "-c", "ls -l /run/udev/rules.d; "
                                         "cat /run/udev/rules.d/*inputplumber* 2>/dev/null | head -n 80"]),
            ("inputplumber Claw configs", ["sh", "-c", "for f in /usr/share/inputplumber/devices/*claw* "
                                           "/etc/inputplumber/devices.d/*; do echo \"## $f\"; "
                                           "cat \"$f\"; done 2>&1 | head -n 400"])):
        section(title, _run_cmd(args, 15)[1])
    profile, error = capture_profile()
    records = load_fix_records()
    section("MCU profile memory (now)", hexdump(profile) if profile else "not captured: %s" % error)
    healthy = next((bytes.fromhex(r["after"]) for r in reversed(records) if r.get("after")), None)
    if profile and healthy:
        section("profile: now vs. right after the last MCU reset",
                "\n".join(diff_profiles(healthy, profile)) or "identical")
    for record in records:
        if record.get("before") and record.get("after"):
            body = "\n".join(diff_profiles(bytes.fromhex(record["before"]),
                                           bytes.fromhex(record["after"]))) or "identical"
        else:
            body = "not captured: %s" % record.get("error")
        section("MCU reset at %s: profile before -> after" % record.get("time"), body)
    section("controller writers since plugin start", "\n".join(TRACE.top_writers(40)))
    section("activity trace", "\n".join(TRACE.lines()))
    section("decky plugins", _run_cmd(["ls", "-1", os.path.join(decky.DECKY_HOME, "plugins")], 5)[1])
    _, dmesg = _run_cmd(["dmesg"], 20)
    pattern = re.compile(r"hid-msi|msi|0db0|xpad|usb \d|xhci|PM: |suspend|resume|inputplumber",
                         re.IGNORECASE)
    section("dmesg (filtered, last 400)",
            "\n".join([ln for ln in dmesg.splitlines() if pattern.search(ln)][-400:]))
    _, journal = _run_cmd(["journalctl", "-b", "-u", "inputplumber.service",
                           "--no-pager", "-n", "300"], 30)
    section("inputplumber journal (last 300)", journal)
    section("plugin history", "\n".join("%s  %s" % item for item in STATE.history))
    section("plugin log files (tail)", read_log_tail())

    name = "claw-rumble-fix-diag-%s.txt" % datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(decky.DECKY_USER_HOME, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    try:
        shutil.chown(path, user=decky.DECKY_USER, group=decky.DECKY_USER)
    except (OSError, LookupError):
        pass
    return path


def read_log_tail(files=3, lines=400):
    """Tail of the newest plugin log files; they outlive plugin restarts."""
    directory = getattr(decky, "DECKY_PLUGIN_LOG_DIR", None)
    if not directory:
        return "log directory unknown"
    try:
        paths = sorted((os.path.join(directory, name) for name in os.listdir(directory)),
                       key=os.path.getmtime)[-files:]
    except OSError as exc:
        return str(exc)
    out = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                out.append("## %s\n%s" % (path, "".join(f.readlines()[-lines:]).rstrip()))
        except OSError as exc:
            out.append("## %s\n%s" % (path, exc))
    return "\n\n".join(out)


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
    except (OSError, ValueError, AttributeError):
        pass
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
    before = await asyncio.to_thread(count_unexpected_acks)
    waited = 0.0
    if STATE.settings["wake_buzz"]:
        # A pulse the user can feel: its absence dates the rumble loss to
        # this very wake-up. Nothing else touches the controller here.
        await asyncio.sleep(WAKE_BUZZ_DELAY)
        waited = WAKE_BUZZ_DELAY
        result = await _locked(act_test_rumble, WAKE_BUZZ_SECONDS)
        _log("wake #%d: check buzz %s (%s)" % (
            count, "sent" if result["ok"] else "FAILED", result["msg"]))
    await asyncio.sleep(max(0.0, WAKE_WINDOW - waited))
    after = await asyncio.to_thread(count_unexpected_acks)
    if before is not None and after is not None:
        STATE.last_wake = "wake #%d: %d unexpected MCU ACK(s) in the first %ds" % (
            count, after - before, WAKE_WINDOW)
        _log(STATE.last_wake)


async def watch_resume():
    last = suspend_offset()
    while True:
        await asyncio.sleep(2)
        try:
            TRACE.refresh()
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
        _log("plugin started, wake-up check buzz %s"
             % ("on" if STATE.settings["wake_buzz"] else "off"))
        TRACE.start(asyncio.get_event_loop())
        STATE.watch_task = asyncio.ensure_future(watch_resume())
        removed = await _locked(remove_old_overrides)
        if removed:
            _log("removed %d udev override(s) left by an older version" % removed)

    async def _unload(self):
        if STATE.watch_task:
            STATE.watch_task.cancel()
            STATE.watch_task = None
        TRACE.stop()
        _log("plugin unloaded")

    async def _uninstall(self):
        pass

    async def get_status(self):
        return await _locked(build_status)

    async def notify_resume(self):
        asyncio.ensure_future(handle_resume("steam"))

    async def fix_rumble(self):
        result = await _locked(act_fix_rumble)
        _log("fix rumble: %s" % result["msg"])
        return result

    async def test_rumble(self):
        result = await _locked(act_test_rumble)
        _log("test rumble: %s" % result["msg"])
        return result

    async def set_wake_buzz(self, enable):
        STATE.settings["wake_buzz"] = bool(enable)
        save_settings()
        _log("wake-up check buzz %s" % ("on" if enable else "off"))
        return _res(True, "wake-up check buzz %s" % ("on" if enable else "off"))

    async def save_diagnostics(self):
        try:
            path = await _locked(write_diagnostics)
        except OSError as exc:
            return _res(False, "diagnostics failed: %s" % exc)
        _log("diagnostics written to %s" % path)
        return _res(True, path)
