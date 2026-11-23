import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  toaster,
} from "@decky/api";
import { useCallback, useEffect, useRef, useState } from "react";
import { MdVibration } from "react-icons/md";

declare const SteamClient: any;

interface ControllerStatus {
  pid: string;
  pid_mode: string;
  busid: string;
  bcd: string | null;
  quirk_active: boolean;
  has_cfg: boolean;
  has_rumble_attrs: boolean;
  has_reset: boolean;
  gamepad_mode: string | null;
  left: number | null;
  right: number | null;
}

interface Status {
  controller: ControllerStatus | null;
  inputplumber: string;
  kernel: string;
  settings: { auto_level: string; reset_resume_quirk: boolean };
  targets: [number, number];
  resume_count: number;
  last_result: string | null;
  history: string[];
}

interface Result {
  ok: boolean;
  msg: string;
  changed: boolean;
}

const getStatus = callable<[], Status>("get_status");
const runAction = callable<[name: string], Result>("run_action");
const setTargets = callable<[left: number, right: number], Result>("set_targets");
const setAutoLevel = callable<[level: string], Result>("set_auto_level");
const setQuirk = callable<[enable: boolean], Result>("set_quirk");
const saveDiagnostics = callable<[], Result>("save_diagnostics");
const notifyResume = callable<[], void>("notify_resume");

const TITLE = "Claw Rumble Fix";
const RESULT_EVENT = "claw_rumble_fix_result";

const AUTO_OPTIONS = [
  { data: "off", label: "Do nothing" },
  { data: "intensity", label: "Check & restore intensity" },
  { data: "mode", label: "Re-send gamepad mode" },
  { data: "usb", label: "USB port reset" },
];

// Ordered from the lightest to the heaviest intervention.
const RECOVERY_ACTIONS = [
  { id: "intensity", label: "1. Rewrite rumble intensity" },
  { id: "resend_mode", label: "2. Re-send gamepad mode" },
  { id: "usb_reset", label: "3. USB port reset" },
  { id: "usb_replug", label: "4. USB re-plug (port power cycle)" },
  { id: "restart_inputplumber", label: "5. Restart InputPlumber" },
  { id: "mcu_reset", label: "6. Controller MCU reset" },
];

function Content() {
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string>("");
  const [left, setLeft] = useState(100);
  const [right, setRight] = useState(100);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await getStatus();
      setStatus(next);
      setLeft(next.targets[0]);
      setRight(next.targets[1]);
    } catch (e) {
      setMessage(`Status failed: ${e}`);
    }
  }, []);

  useEffect(() => {
    refresh();
    const listener = addEventListener<[ok: boolean, msg: string]>(RESULT_EVENT, () => {
      refresh();
    });
    return () => {
      removeEventListener(RESULT_EVENT, listener);
      if (debounce.current) clearTimeout(debounce.current);
    };
  }, [refresh]);

  const run = async (id: string, call: () => Promise<Result>) => {
    setBusy(id);
    setMessage("Working...");
    try {
      const result = await call();
      setMessage(`${result.ok ? "OK" : "FAILED"}: ${result.msg}`);
    } catch (e) {
      setMessage(`FAILED: ${e}`);
    }
    setBusy(null);
    await refresh();
  };

  // Every intensity write is committed to the controller flash: wait until
  // the slider settles before sending it.
  const queueTargets = (nextLeft: number, nextRight: number) => {
    setLeft(nextLeft);
    setRight(nextRight);
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(() => {
      run("targets", () => setTargets(nextLeft, nextRight));
    }, 600);
  };

  const ctrl = status?.controller ?? null;
  const intensity =
    ctrl && ctrl.left !== null && ctrl.right !== null
      ? `L ${ctrl.left} / R ${ctrl.right}`
      : ctrl?.has_rumble_attrs
        ? "unreadable"
        : "not exposed by kernel";
  const disabled = busy !== null;

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <Field label="Controller" focusable>
            {ctrl
              ? `${ctrl.gamepad_mode ?? ctrl.pid_mode} (0db0:${ctrl.pid})`
              : status
                ? "not found"
                : "..."}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Rumble intensity" focusable>
            {intensity}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Wake-ups seen" description={status?.last_result ?? undefined} focusable>
            {status?.resume_count ?? 0}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={disabled}
            description="Drives both motors directly, bypassing InputPlumber and Steam Input."
            onClick={() => run("test_rumble", () => runAction("test_rumble"))}
          >
            Test rumble (direct)
          </ButtonItem>
        </PanelSectionRow>
        {message && (
          <PanelSectionRow>
            <Field label={message} focusable />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="After wake-up">
        <PanelSectionRow>
          <DropdownItem
            label="Automatic fix"
            rgOptions={AUTO_OPTIONS}
            selectedOption={status?.settings.auto_level ?? "intensity"}
            disabled={disabled}
            onChange={(option) => run("auto", () => setAutoLevel(option.data))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Kernel reset-on-resume"
            description={
              ctrl?.quirk_active
                ? "Active: the controller USB port is reset on every resume."
                : "Experimental. Reconnects the controller once to take effect."
            }
            checked={status?.settings.reset_resume_quirk ?? false}
            disabled={disabled}
            onChange={(value) => run("quirk", () => setQuirk(value))}
          />
        </PanelSectionRow>
      </PanelSection>

      {ctrl?.has_rumble_attrs && (
        <PanelSection title="Rumble intensity">
          <PanelSectionRow>
            <SliderField
              label="Left"
              value={left}
              min={0}
              max={100}
              step={5}
              showValue
              disabled={disabled}
              onChange={(value) => queueTargets(value, right)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <SliderField
              label="Right"
              value={right}
              min={0}
              max={100}
              step={5}
              showValue
              disabled={disabled}
              onChange={(value) => queueTargets(left, value)}
            />
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="Manual recovery">
        <PanelSectionRow>
          <Field
            label="Rumble gone?"
            description="Go top to bottom and test rumble after each step. Note which step brings it back."
            focusable
          />
        </PanelSectionRow>
        {RECOVERY_ACTIONS.map((action) => (
          <PanelSectionRow key={action.id}>
            <ButtonItem
              layout="below"
              disabled={disabled}
              onClick={() => run(action.id, () => runAction(action.id))}
            >
              {busy === action.id ? "Working..." : action.label}
            </ButtonItem>
          </PanelSectionRow>
        ))}
      </PanelSection>

      <PanelSection title="Diagnostics">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={disabled}
            description="Writes a report to your home folder. Best taken while rumble is broken."
            onClick={() => run("diag", saveDiagnostics)}
          >
            Save diagnostics report
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label={`Kernel ${status?.kernel ?? "?"}`}
            description={`InputPlumber: ${status?.inputplumber ?? "?"}`}
            focusable
          />
        </PanelSectionRow>
        {(status?.history ?? []).map((line, index) => (
          <PanelSectionRow key={index}>
            <div style={{ fontSize: "11px", opacity: 0.7, wordBreak: "break-word" }}>{line}</div>
          </PanelSectionRow>
        ))}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // Second wake-up signal next to the backend clock watcher.
  const resumeHook = SteamClient?.System?.RegisterForOnResumeFromSuspend?.(() => {
    notifyResume().catch(() => undefined);
  });

  const listener = addEventListener<[ok: boolean, msg: string]>(RESULT_EVENT, (ok, msg) => {
    toaster.toast({ title: ok ? `${TITLE}: repaired` : `${TITLE}: failed`, body: msg });
  });

  return {
    name: TITLE,
    titleView: <div className={staticClasses.Title}>{TITLE}</div>,
    content: <Content />,
    icon: <MdVibration />,
    onDismount() {
      removeEventListener(RESULT_EVENT, listener);
      resumeHook?.unregister?.();
    },
  };
});
