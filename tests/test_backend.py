"""Backend tests against a fake sysfs tree (no hardware required)."""
import asyncio
import os
import struct
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeDecky(types.ModuleType):
    def __init__(self):
        super().__init__("decky")
        self.emitted = []
        self.logger = types.SimpleNamespace(info=lambda *a: None, error=lambda *a: None)
        self.DECKY_USER = "nobody"
        self.DECKY_USER_HOME = tempfile.mkdtemp()
        self.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp()

    async def emit(self, *args):
        self.emitted.append(args)


decky = FakeDecky()
sys.modules["decky"] = decky
sys.path.insert(0, ROOT)
import main  # noqa: E402


def put(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(value) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.sys = tempfile.mkdtemp()
        main.SYS = self.sys
        main.STATE = main.State()
        main.STATE.settings["resume_delay"] = 0
        decky.emitted.clear()
        self.writes = []
        self._sleep = main.time.sleep
        main.time.sleep = lambda _s: None

        def fake_write(path, value):
            self.writes.append((os.path.relpath(path, self.sys), str(value)))
            put(path, value)

        self._write = main._write
        main._write = fake_write
        self.usb = os.path.join(self.sys, "bus/usb/devices/3-3")
        self.cfg = os.path.join(self.usb, "3-3:1.0/0003:0DB0:1902.0001")
        for name, value in (("idVendor", "0db0"), ("idProduct", "1902"), ("busnum", "3"),
                            ("devnum", "4"), ("bcdDevice", "0217"), ("quirks", "0x0")):
            put(os.path.join(self.usb, name), value)
        for name, value in (("gamepad_mode", "dinput"), ("rumble_intensity_left", "100"),
                            ("rumble_intensity_right", "100"), ("reset", "")):
            put(os.path.join(self.cfg, name), value)
        # Decoys: a root hub and an interface entry.
        put(os.path.join(self.sys, "bus/usb/devices/usb3/idVendor"), "1d6b")
        put(os.path.join(self.sys, "bus/usb/devices/3-3:1.0/idVendor"), "0db0")
        put(os.path.join(self.sys, "module/usbcore/parameters/quirks"), "")

    def tearDown(self):
        main.time.sleep = self._sleep
        main._write = self._write


class Discovery(Base):
    def test_finds_controller_and_config_interface(self):
        ctrl = main.find_controller()
        self.assertEqual(ctrl["busid"], "3-3")
        self.assertEqual(ctrl["pid_mode"], "dinput")
        self.assertEqual(ctrl["cfg"], self.cfg)
        self.assertFalse(main.quirk_active(ctrl))

    def test_absent_controller(self):
        put(os.path.join(self.usb, "idVendor"), "045e")
        self.assertIsNone(main.find_controller())

    def test_quirk_bit(self):
        put(os.path.join(self.usb, "quirks"), "0x2")
        self.assertTrue(main.quirk_active(main.find_controller()))

    def test_status_adopts_hardware_intensity_as_target(self):
        put(os.path.join(self.cfg, "rumble_intensity_left"), "60")
        status = main.build_status()
        self.assertEqual(status["controller"]["left"], 60)
        self.assertEqual(status["targets"], [60, 100])

    def test_status_does_not_adopt_zeroed_intensity(self):
        put(os.path.join(self.cfg, "rumble_intensity_left"), "0")
        put(os.path.join(self.cfg, "rumble_intensity_right"), "0")
        self.assertEqual(main.build_status()["targets"], [100, 100])


class Intensity(Base):
    def test_intact_intensity_is_not_rewritten(self):
        result = main.act_intensity((100, 100), force=False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.writes, [])

    def test_lost_intensity_is_rewritten(self):
        put(os.path.join(self.cfg, "rumble_intensity_left"), "0")
        put(os.path.join(self.cfg, "rumble_intensity_right"), "0")
        result = main.act_intensity((100, 80), force=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(main.read_intensity(self.cfg), (100, 80))

    def test_forced_rewrite_writes_even_when_intact(self):
        result = main.act_intensity((100, 100), force=True)
        self.assertTrue(result["changed"])
        self.assertEqual(len(self.writes), 2)

    def test_missing_attributes_skip_quietly_in_auto_mode(self):
        os.unlink(os.path.join(self.cfg, "rumble_intensity_left"))
        self.assertTrue(main.act_intensity((100, 100), force=False)["ok"])
        self.assertFalse(main.act_intensity((100, 100), force=True)["ok"])

    def test_auto_fix_reports_change(self):
        put(os.path.join(self.cfg, "rumble_intensity_right"), "0")
        result = main.auto_fix("intensity", (100, 100))
        self.assertTrue(result["ok"] and result["changed"])


class Quirk(Base):
    OURS = "0db0:1901:b,0db0:1902:b,0db0:1903:b,0db0:1904:b"

    def test_merge_keeps_foreign_entries(self):
        self.assertEqual(main.merge_quirks("", True), self.OURS)
        self.assertEqual(main.merge_quirks("1234:5678:k", True), "1234:5678:k," + self.OURS)
        self.assertEqual(main.merge_quirks("1234:5678:k," + self.OURS, False), "1234:5678:k")
        self.assertEqual(main.merge_quirks(self.OURS, True), self.OURS)

    def test_enable_does_not_toggle_an_active_quirk(self):
        put(os.path.join(self.usb, "quirks"), "0x2")
        result = main.act_quirk(True)
        self.assertTrue(result["ok"])
        self.assertEqual(self.writes, [])

    def test_disable_removes_only_our_entries(self):
        param = main.quirk_param_path()
        put(param, "1234:5678:k," + self.OURS)
        self.assertTrue(main.act_quirk(False)["ok"])
        self.assertEqual(main._read(param), "1234:5678:k")


class Formats(unittest.TestCase):
    def test_kernel_struct_sizes(self):
        self.assertEqual(struct.calcsize("=HhHHHHH2xHH28x"), 48)  # struct ff_effect
        self.assertEqual(struct.calcsize("=qqHHi"), 24)           # struct input_event
        self.assertEqual(main.EVIOCSFF, (1 << 30) | (48 << 16) | (ord("E") << 8) | 0x80)
        self.assertEqual(main.EVIOCRMFF, (1 << 30) | (4 << 16) | (ord("E") << 8) | 0x81)
        self.assertEqual(main.USBDEVFS_RESET, (ord("U") << 8) | 20)

    def test_ff_capability_bitmap(self):
        root = tempfile.mkdtemp()
        put(os.path.join(root, "device/capabilities/ff"), "107030000 0")  # xpad
        self.assertTrue(main._has_ff_rumble(root))
        put(os.path.join(root, "device/capabilities/ff"), "0")
        self.assertFalse(main._has_ff_rumble(root))


class Resume(Base):
    def test_wake_repairs_and_notifies_once(self):
        put(os.path.join(self.cfg, "rumble_intensity_left"), "0")

        async def scenario():
            await main.handle_resume("clock", 30.0)
            await main.handle_resume("steam")  # duplicate report of the same wake

        asyncio.run(scenario())
        self.assertEqual(main.STATE.resume_count, 1)
        self.assertEqual(main.read_intensity(self.cfg), (100, 100))
        self.assertEqual(len(decky.emitted), 1)
        self.assertEqual(decky.emitted[0][0], "claw_rumble_fix_result")
        self.assertTrue(decky.emitted[0][1])

    def test_healthy_wake_is_silent(self):
        asyncio.run(main.handle_resume("clock", 30.0))
        self.assertEqual(self.writes, [])
        self.assertEqual(decky.emitted, [])

    def test_off_level_touches_nothing(self):
        main.STATE.settings["auto_level"] = "off"
        put(os.path.join(self.cfg, "rumble_intensity_left"), "0")
        asyncio.run(main.handle_resume("clock", 30.0))
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()
