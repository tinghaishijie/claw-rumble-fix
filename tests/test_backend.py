"""Backend tests against a fake sysfs tree (no hardware required)."""
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
        self.logger = types.SimpleNamespace(info=lambda *a: None, error=lambda *a: None)
        self.DECKY_USER = "nobody"
        self.DECKY_USER_HOME = tempfile.mkdtemp()
        self.DECKY_HOME = tempfile.mkdtemp()
        self.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp()
        self.DECKY_PLUGIN_LOG_DIR = tempfile.mkdtemp()


decky = FakeDecky()
sys.modules["decky"] = decky
sys.path.insert(0, ROOT)
import main  # noqa: E402


def put(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(value) + "\n")


class Base(unittest.TestCase):
    """A Claw A8 as seen on the affected device: XInput mode, intensity 50/50."""

    def setUp(self):
        self.sys = tempfile.mkdtemp()
        main.SYS = self.sys
        main.DEV = "/dev"
        main.STATE = main.State()
        self.writes = []
        self._sleep = main.time.sleep
        main.time.sleep = lambda _s: None

        def fake_write(path, value):
            self.writes.append((os.path.relpath(path, self.sys), str(value)))
            put(path, value)

        self._write = main._write
        main._write = fake_write
        self.usb = os.path.join(self.sys, "bus/usb/devices/3-3")
        self.cfg = os.path.join(self.usb, "3-3:1.1/0003:0DB0:1901.0001")
        for name, value in (("idVendor", "0db0"), ("idProduct", "1901"), ("busnum", "3"),
                            ("devnum", "2"), ("bcdDevice", "0313"), ("quirks", "0x0")):
            put(os.path.join(self.usb, name), value)
        for name, value in (("gamepad_mode", "xinput"), ("mkeys_function", "macro"),
                            ("rumble_intensity_left", "50"), ("rumble_intensity_right", "50"),
                            ("reset", "")):
            put(os.path.join(self.cfg, name), value)
        # Decoys: a root hub and an interface entry.
        put(os.path.join(self.sys, "bus/usb/devices/usb3/idVendor"), "1d6b")
        put(os.path.join(self.sys, "bus/usb/devices/3-3:1.1/idVendor"), "0db0")

    def tearDown(self):
        main.time.sleep = self._sleep
        main._write = self._write

    def attr(self, name):
        return main._read(os.path.join(self.cfg, name))


class Discovery(Base):
    def test_finds_controller_and_config_interface(self):
        ctrl = main.find_controller()
        self.assertEqual((ctrl["busid"], ctrl["pid_mode"], ctrl["cfg"]),
                         ("3-3", "xinput", self.cfg))

    def test_absent_controller(self):
        put(os.path.join(self.usb, "idVendor"), "1234")
        self.assertIsNone(main.find_controller())
        self.assertIsNone(main.build_status()["controller"])

    def test_status_says_whether_a_reset_is_possible(self):
        self.assertTrue(main.build_status()["controller"]["can_reset"])
        os.unlink(os.path.join(self.cfg, "reset"))
        self.assertFalse(main.build_status()["controller"]["can_reset"])


class McuReset(Base):
    def setUp(self):
        super().setUp()
        put(os.path.join(self.cfg, "mkeys_function"), "combination")
        base_write = main._write
        self.after_reset = {}

        def write_with_mcu(path, value):
            base_write(path, value)
            if path.endswith("/reset"):  # the MCU comes back with these settings
                for name, new in self.after_reset.items():
                    put(os.path.join(self.cfg, name), new)

        main._write = write_with_mcu
        self._saved = (main.wait_reenumerated, main.inputplumber_active, main._run_cmd,
                       main.RULES_DIR)
        main.wait_reenumerated = lambda devnum, timeout=0: main.find_controller()
        main.inputplumber_active = lambda: False
        main._run_cmd = lambda args, timeout=20: (0, "")
        main.RULES_DIR = tempfile.mkdtemp()

    def tearDown(self):
        (main.wait_reenumerated, main.inputplumber_active, main._run_cmd,
         main.RULES_DIR) = self._saved
        super().tearDown()

    def test_reset_that_keeps_settings_writes_nothing_else(self):
        result = main.mcu_reset()
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual([name for name, _ in self.writes], [os.path.relpath(
            os.path.join(self.cfg, "reset"), self.sys)])

    def test_reset_puts_changed_settings_back(self):
        self.after_reset = {"mkeys_function": "macro", "gamepad_mode": "dinput"}
        result = main.mcu_reset()
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual(self.attr("mkeys_function"), "combination")
        self.assertEqual(self.attr("gamepad_mode"), "xinput")
        self.assertIn("mkeys_function restored macro -> combination", result["msg"])

    def test_missing_reset_attribute_fails_cleanly(self):
        os.unlink(os.path.join(self.cfg, "reset"))
        result = main.mcu_reset()
        self.assertFalse(result["ok"])
        self.assertEqual(self.writes, [])

    def test_hidden_pad_stays_hidden_during_the_reset(self):
        # A hidden pad plus a running InputPlumber: the temporary rules must be
        # in place while the controller re-enumerates and gone afterwards.
        main.DEV = tempfile.mkdtemp()
        event = os.path.join(self.usb, "3-3:1.0/input/input20/event7")
        put(os.path.join(event, "device/capabilities/abs"), "3003f")
        node = os.path.join(main.DEV, "input/event7")
        put(node, "")
        os.chmod(node, 0)
        put(os.path.join(main.RULES_DIR, "50-inputplumber-hide-event7-early.rules"),
            'KERNELS=="input20"')
        main.inputplumber_active = lambda: True
        seen = []
        main.wait_reenumerated = lambda devnum, timeout=0: (
            seen.append(sorted(n for n in os.listdir(main.RULES_DIR) if "claw" in n)),
            main.find_controller())[1]
        result = main.mcu_reset()
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual(seen, [sorted(main.HIDE_RULES)])
        self.assertEqual([n for n in os.listdir(main.RULES_DIR) if "claw" in n], [])
        self.assertNotIn("extra", result["msg"])

    def test_visible_pad_after_the_reset_is_reported(self):
        main.DEV = tempfile.mkdtemp()
        event = os.path.join(self.usb, "3-3:1.0/input/input20/event7")
        put(os.path.join(event, "device/capabilities/abs"), "3003f")
        node = os.path.join(main.DEV, "input/event7")
        put(node, "")
        os.chmod(node, 0)
        main.inputplumber_active = lambda: True
        main.wait_reenumerated = lambda devnum, timeout=0: (
            os.chmod(node, 0o660), main.find_controller())[1]
        clock = iter(range(0, 10000, 3))
        saved, main.time.monotonic = main.time.monotonic, lambda: next(clock)
        try:
            result = main.mcu_reset()
        finally:
            main.time.monotonic = saved
        self.assertTrue(result["ok"], result["msg"])
        self.assertIn("extra Xbox 360 controller", result["msg"])


class ModeSwitch(Base):
    def setUp(self):
        super().setUp()
        self._saved = (main.wait_reenumerated, main.inputplumber_active, main._run_cmd,
                       main.RULES_DIR)
        main.wait_reenumerated = lambda devnum, timeout=0: main.find_controller()
        main.inputplumber_active = lambda: False
        main._run_cmd = lambda args, timeout=20: (0, "")
        main.RULES_DIR = tempfile.mkdtemp()

    def tearDown(self):
        (main.wait_reenumerated, main.inputplumber_active, main._run_cmd,
         main.RULES_DIR) = self._saved
        super().tearDown()

    def test_switch_writes_the_mode_and_confirms_it(self):
        result = main.set_gamepad_mode("dinput")
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual(self.attr("gamepad_mode"), "dinput")
        self.assertEqual(self.writes, [(os.path.relpath(
            os.path.join(self.cfg, "gamepad_mode"), self.sys), "dinput")])

    def test_unknown_mode_is_rejected_without_a_write(self):
        self.assertFalse(main.set_gamepad_mode("desktop")["ok"])
        self.assertEqual(self.writes, [])

    def test_mode_that_did_not_stick_is_a_failure(self):
        base_write = main._write
        main._write = lambda path, value: base_write(path, "xinput")  # MCU ignores it
        result = main.set_gamepad_mode("dinput")
        self.assertFalse(result["ok"])
        self.assertIn("reports mode 'xinput'", result["msg"])

    def test_hidden_pad_stays_hidden_during_the_switch(self):
        main.DEV = tempfile.mkdtemp()
        event = os.path.join(self.usb, "3-3:1.0/input/input20/event7")
        put(os.path.join(event, "device/capabilities/abs"), "3003f")
        node = os.path.join(main.DEV, "input/event7")
        put(node, "")
        os.chmod(node, 0)
        put(os.path.join(main.RULES_DIR, "50-inputplumber-hide-event7-early.rules"),
            'KERNELS=="input20"')
        main.inputplumber_active = lambda: True
        seen = []
        main.wait_reenumerated = lambda devnum, timeout=0: (
            seen.append(sorted(n for n in os.listdir(main.RULES_DIR) if "claw" in n)),
            main.find_controller())[1]
        self.assertTrue(main.set_gamepad_mode("dinput")["ok"])
        self.assertEqual(seen, [sorted(main.HIDE_RULES)])
        self.assertEqual([n for n in os.listdir(main.RULES_DIR) if "claw" in n], [])


class Capture(Base):
    def test_fix_captures_before_and_after_even_without_hardware_access(self):
        # No usbmon, hidraw or EC here: every probe fails, the fix still runs
        # and both records are written.
        saved = (main.wait_reenumerated, main.inputplumber_active, main._run_cmd)
        main.wait_reenumerated = lambda devnum, timeout=0: main.find_controller()
        main.inputplumber_active = lambda: False
        main._run_cmd = lambda args, timeout=20: (1, "")
        decky.DECKY_USER_HOME = tempfile.mkdtemp()
        try:
            result = main.fix_rumble()
        finally:
            main.wait_reenumerated, main.inputplumber_active, main._run_cmd = saved
        self.assertTrue(result["ok"], result["msg"])
        files = sorted(os.listdir(result["capture"]))
        self.assertEqual(files, ["1-before-reset.txt", "2-after-reset.txt"])
        text = main._read_quiet(os.path.join(result["capture"], files[0]))
        self.assertIn("mode=xinput mkeys=macro intensity=L50/R50", text)
        self.assertIn("FAILED", text)
        self.assertEqual(main.build_status()["captures"], 1)

    def test_hexdump(self):
        self.assertEqual(main.hexdump(bytes(range(18))),
                         "0000: 00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f\n0010: 10 11")

    def test_hide_rules_match_the_pad_only(self):
        for text in main.HIDE_RULES.values():
            self.assertIn('ATTRS{id/vendor}=="0db0"', text)
            self.assertIn('ATTRS{capabilities/abs}!="0"', text)  # not the keyboard node
            self.assertTrue(text.endswith("\n"))


class Exposure(Base):
    def test_detects_unhidden_physical_pad(self):
        main.DEV = tempfile.mkdtemp()
        event = os.path.join(self.usb, "3-3:1.0/input/input20/event7")
        put(os.path.join(event, "device/capabilities/abs"), "3003f")
        node = os.path.join(main.DEV, "input/event7")
        put(node, "")
        ctrl = main.find_controller()
        os.chmod(node, 0o660)
        self.assertTrue(main.pad_exposed(ctrl))
        os.chmod(node, 0)  # hidden by InputPlumber
        self.assertFalse(main.pad_exposed(ctrl))
        os.unlink(node)    # or moved away
        self.assertFalse(main.pad_exposed(ctrl))

    def test_keyboard_interface_is_not_a_pad(self):
        main.DEV = tempfile.mkdtemp()
        event = os.path.join(self.usb, "3-3:1.2/input/input21/event8")
        put(os.path.join(event, "device/capabilities/abs"), "100000000")
        put(os.path.join(main.DEV, "input/event8"), "")
        self.assertFalse(main.pad_exposed(main.find_controller()))


class Formats(unittest.TestCase):
    def test_kernel_struct_sizes(self):
        self.assertEqual(struct.calcsize("=HhHHHHH2xHH28x"), 48)  # struct ff_effect
        self.assertEqual(struct.calcsize("=qqHHi"), 24)           # struct input_event
        self.assertEqual(main.EVIOCSFF, (1 << 30) | (48 << 16) | (ord("E") << 8) | 0x80)
        self.assertEqual(main.EVIOCRMFF, (1 << 30) | (4 << 16) | (ord("E") << 8) | 0x81)

    def test_ff_capability_bitmap(self):
        root = tempfile.mkdtemp()
        put(os.path.join(root, "device/capabilities/ff"), "107030000 0")  # xpad
        self.assertTrue(main._has_ff_rumble(root))
        put(os.path.join(root, "device/capabilities/ff"), "0")
        self.assertFalse(main._has_ff_rumble(root))

if __name__ == "__main__":
    unittest.main()
