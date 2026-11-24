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
        self.logger = types.SimpleNamespace(info=lambda *a: None, error=lambda *a: None)
        self.DECKY_USER = "nobody"
        self.DECKY_USER_HOME = tempfile.mkdtemp()
        self.DECKY_HOME = tempfile.mkdtemp()
        self.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp()


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
        self.assertEqual(ctrl["busid"], "3-3")
        self.assertEqual(ctrl["pid_mode"], "xinput")
        self.assertEqual(ctrl["cfg"], self.cfg)

    def test_absent_controller(self):
        put(os.path.join(self.usb, "idVendor"), "045e")
        self.assertIsNone(main.find_controller())
        self.assertIsNone(main.build_status()["controller"])

    def test_status_reports_controller_state(self):
        status = main.build_status()
        self.assertTrue(status["controller"]["has_reset"])
        self.assertEqual(status["controller"]["state"]["gamepad_mode"], "xinput")
        self.assertEqual(status["controller"]["state"]["rumble_intensity_left"], "50")
        self.assertFalse(status["quiet_udev"])


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
        self._wait = main.wait_reenumerated
        main.wait_reenumerated = lambda devnum, timeout=0: main.find_controller()

    def tearDown(self):
        main.wait_reenumerated = self._wait
        super().tearDown()

    def test_reset_that_keeps_settings_writes_nothing_else(self):
        result = main.act_mcu_reset()
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual([name for name, _ in self.writes], [os.path.relpath(
            os.path.join(self.cfg, "reset"), self.sys)])

    def test_reset_puts_changed_settings_back(self):
        self.after_reset = {"mkeys_function": "macro", "rumble_intensity_left": "100",
                            "gamepad_mode": "dinput"}
        result = main.act_mcu_reset()
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual(self.attr("mkeys_function"), "combination")
        self.assertEqual(self.attr("gamepad_mode"), "xinput")
        self.assertEqual(self.attr("rumble_intensity_left"), "50")
        self.assertIn("mkeys_function restored macro -> combination", result["msg"])

    def test_missing_reset_attribute_fails_cleanly(self):
        os.unlink(os.path.join(self.cfg, "reset"))
        result = main.act_fix_rumble()
        self.assertFalse(result["ok"])
        self.assertEqual(self.writes, [])

    def test_fix_reports_a_failed_test_pulse(self):
        # No rumble-capable node in the fake tree: the reset works, the pulse cannot.
        result = main.act_fix_rumble()
        self.assertFalse(result["ok"])
        self.assertIn("MCU reset done", result["msg"])


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


class Diagnostics(Base):
    def test_report_is_written_without_system_tools(self):
        event = os.path.join(self.usb, "3-3:1.0/input/input20/event7")
        put(os.path.join(event, "device/capabilities/abs"), "3003f")
        put(os.path.join(event, "device/name"), "Fake Pad")
        path = main.write_diagnostics()
        with open(path) as f:
            report = f.read()
        self.assertIn("controller evdev nodes (pad_exposed=", report)
        self.assertIn("name=Fake Pad", report)
        self.assertIn("gamepad_mode = xinput", report)
        self.assertIn("inputplumber udev rule (installed)", report)


class UdevOverride(Base):
    V078 = ('ACTION=="add|change|bind", ATTRS{idVendor}=="0db0", ATTRS{idProduct}=="190[1-3]", '
            'SUBSYSTEM=="hid", DRIVER=="hid-msi", ATTR{button_m1}="KEY_RIGHTBRACE", '
            'ATTR{button_m2}="KEY_LEFTBRACE", GOTO="claw_desktop"')
    MATCH = ('ACTION=="add|change|bind", ATTRS{idVendor}=="0db0", ATTRS{idProduct}=="190[1-3]", '
             'SUBSYSTEM=="hid", DRIVER=="hid-msi"')

    def setUp(self):
        super().setUp()
        self.saved = (main.RULES_SOURCE_DIR, main.RULES_ETC_DIR, main.RULES_OVERRIDE_DIR,
                      main._run_cmd)
        main.RULES_SOURCE_DIR = tempfile.mkdtemp()
        main.RULES_ETC_DIR = tempfile.mkdtemp()
        main.RULES_OVERRIDE_DIR = os.path.join(tempfile.mkdtemp(), "rules.d")
        self.commands = []
        main._run_cmd = lambda args, timeout=0: (self.commands.append(args), (0, ""))[1]
        self.source = os.path.join(main.RULES_SOURCE_DIR, main.RULES_NAME)
        self.override = os.path.join(main.RULES_OVERRIDE_DIR, main.RULES_NAME)

    def tearDown(self):
        (main.RULES_SOURCE_DIR, main.RULES_ETC_DIR, main.RULES_OVERRIDE_DIR,
         main._run_cmd) = self.saved
        super().tearDown()

    def test_unconditional_writes_become_conditional(self):
        self.assertEqual(main.soften_rule(self.V078), [
            self.MATCH + ', ATTR{button_m1}!="KEY_RIGHTBRACE", ATTR{button_m1}="KEY_RIGHTBRACE"',
            self.MATCH + ', ATTR{button_m2}!="KEY_LEFTBRACE", ATTR{button_m2}="KEY_LEFTBRACE"',
            self.MATCH + ', GOTO="claw_desktop"',
        ])

    def test_existing_conditions_are_kept(self):
        line = ('ACTION=="add", DRIVER=="hid-msi", ATTR{gamepad_mode}=="desktop", '
                'ATTR{gamepad_mode}="xinput", GOTO="end"')
        self.assertEqual(main.soften_rule(line), [
            'ACTION=="add", DRIVER=="hid-msi", ATTR{gamepad_mode}=="desktop", '
            'ATTR{gamepad_mode}!="xinput", ATTR{gamepad_mode}="xinput"',
            'ACTION=="add", DRIVER=="hid-msi", ATTR{gamepad_mode}=="desktop", GOTO="end"',
        ])

    def test_unrelated_lines_are_untouched(self):
        for line in ("# MSI Claw Settings", "", 'LABEL="claw_desktop"',
                     'ACTION=="add", DRIVER=="asus_rog_ally", ATTR{gamepad_mode}="1"',
                     'ACTION=="add", DRIVER=="hid-msi", GOTO="end"'):
            self.assertEqual(main.soften_rule(line), [line])

    def test_enable_writes_runtime_override_and_reloads(self):
        put(self.source, "# header\n" + self.V078 + '\nLABEL="claw_desktop"')
        result = main.act_quiet_udev(True)
        self.assertTrue(result["ok"], result["msg"])
        with open(self.override) as f:
            text = f.read()
        self.assertIn('ATTR{button_m1}!="KEY_RIGHTBRACE", ATTR{button_m1}="KEY_RIGHTBRACE"', text)
        self.assertIn('LABEL="claw_desktop"', text)
        self.assertNotIn(self.V078, text)
        self.assertEqual(self.commands, [["udevadm", "control", "--reload"]])
        self.assertTrue(main.build_status()["quiet_udev_active"])
        self.assertTrue(main.act_quiet_udev(False)["ok"])
        self.assertFalse(os.path.exists(self.override))

    def test_user_override_in_etc_is_respected(self):
        put(self.source, self.V078)
        put(os.path.join(main.RULES_ETC_DIR, main.RULES_NAME), "# mine")
        self.assertFalse(main.act_quiet_udev(True)["ok"])
        self.assertFalse(os.path.exists(self.override))

    def test_missing_or_irrelevant_rule_file_fails_cleanly(self):
        self.assertFalse(main.act_quiet_udev(True)["ok"])
        put(self.source, "# nothing for the claw here")
        self.assertFalse(main.act_quiet_udev(True)["ok"])
        self.assertFalse(os.path.exists(self.override))


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


class Resume(Base):
    def setUp(self):
        super().setUp()
        self.saved = (main.WAKE_SETTLE, main.WAKE_WINDOW, main.count_flash_acks)
        main.WAKE_SETTLE = main.WAKE_WINDOW = 0
        self.acks = iter([100, 106, 106, 106])
        main.count_flash_acks = lambda: next(self.acks)

    def tearDown(self):
        main.WAKE_SETTLE, main.WAKE_WINDOW, main.count_flash_acks = self.saved
        super().tearDown()

    def test_wake_is_counted_once_and_only_observed(self):
        async def scenario():
            await main.handle_resume("clock", 30.0)
            await main.handle_resume("steam")  # duplicate report of the same wake

        asyncio.run(scenario())
        self.assertEqual(main.STATE.resume_count, 1)
        self.assertEqual(self.writes, [])  # a wake-up never writes to the controller
        self.assertIn("6 flash commit(s)", main.STATE.last_wake)
        history = " | ".join(msg for _, msg in main.STATE.history)
        self.assertIn("controller state mode=xinput mkeys=macro intensity=L50/R50", history)


class Settings(Base):
    def test_obsolete_keys_are_dropped_on_load(self):
        put(main.settings_path(), '{"auto_level": "mcu", "target_left": 50, "quiet_udev": true}')
        main.load_settings()
        self.assertEqual(main.STATE.settings, {"quiet_udev": True})


if __name__ == "__main__":
    unittest.main()
