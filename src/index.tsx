import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useCallback, useEffect, useState } from "react";
import { MdVibration } from "react-icons/md";

declare const SteamClient: any;

interface Status {
  controller: {
    pid: string;
    pid_mode: string;
    has_reset: boolean;
    state: { gamepad_mode?: string | null };
  } | null;
  inputplumber: string;
  kernel: string;
  root: boolean;
  quiet_udev: boolean;
  quiet_udev_active: boolean;
  quiet_udev_msg: string | null;
  writers: string[];
  trace_notes: string[];
  flash_acks: number | null;
  resume_count: number;
  last_wake: string | null;
  history: string[];
}

interface Result {
  ok: boolean;
  msg: string;
}

const getStatus = callable<[], Status>("get_status");
const fixRumble = callable<[], Result>("fix_rumble");
const testRumble = callable<[], Result>("test_rumble");
const setQuietUdev = callable<[enable: boolean], Result>("set_quiet_udev");
const saveDiagnostics = callable<[], Result>("save_diagnostics");
const notifyResume = callable<[], void>("notify_resume");

const TITLE = "Claw Rumble Fix";

function Content() {
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string>("");

  const refresh = useCallback(async () => {
    try {
      setStatus(await getStatus());
    } catch (e) {
      setMessage(`Status failed: ${e}`);
    }
  }, []);

  useEffect(() => {
    refresh();
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

  const ctrl = status?.controller ?? null;
  const disabled = busy !== null;

  return (
    <>
      <PanelSection title="Prevention">
        {status && !status.root && (
          <PanelSectionRow>
            <Field
              label="Backend is not running as root"
              description="Nothing below will work. Reinstall the plugin and restart Decky."
              focusable
            />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ToggleField
            label="Stop redundant flash writes"
            description={
              status?.quiet_udev && !status.quiet_udev_active
                ? `NOT ACTIVE: ${status.quiet_udev_msg ?? "the override could not be installed"}`
                : status?.quiet_udev_active
                  ? (status.quiet_udev_msg ?? "Active: udev only writes controller settings that actually changed.")
                  : "InputPlumber rewrites unchanged settings into the controller flash after every wake-up. Suspected cause of the rumble loss."
            }
            checked={status?.quiet_udev ?? false}
            disabled={disabled}
            onChange={(value) => run("quiet", () => setQuietUdev(value))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label="Flash commits since boot"
            description={status?.last_wake ?? "Counted from the kernel log."}
            focusable
          >
            {status?.flash_acks ?? "?"}
          </Field>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Who writes to the controller">
        {(status?.trace_notes ?? []).map((line, index) => (
          <PanelSectionRow key={`note-${index}`}>
            <Field label={line} focusable />
          </PanelSectionRow>
        ))}
        {(status?.writers ?? []).length === 0 ? (
          <PanelSectionRow>
            <Field label="No writes seen since the plugin started" focusable />
          </PanelSectionRow>
        ) : (
          (status?.writers ?? []).map((line, index) => (
            <PanelSectionRow key={`writer-${index}`}>
              <div style={{ fontSize: "12px", wordBreak: "break-word" }}>{line}</div>
            </PanelSectionRow>
          ))
        )}
      </PanelSection>

      <PanelSection title="Rumble gone?">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={disabled || !ctrl?.has_reset}
            description="Resets the controller MCU and pulses the motors. Steam may then list an extra Xbox 360 controller until you reboot."
            onClick={() => run("fix", fixRumble)}
          >
            {busy === "fix" ? "Working..." : "Fix rumble now"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={disabled || !ctrl}
            description="Drives both motors directly, bypassing InputPlumber and Steam Input."
            onClick={() => run("test", testRumble)}
          >
            Test rumble
          </ButtonItem>
        </PanelSectionRow>
        {message && (
          <PanelSectionRow>
            <Field label={message} focusable />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Diagnostics">
        <PanelSectionRow>
          <Field
            label="Controller"
            description={`Kernel ${status?.kernel ?? "?"}, InputPlumber ${status?.inputplumber ?? "?"}, ${status?.resume_count ?? 0} wake-up(s) seen`}
            focusable
          >
            {ctrl
              ? `${ctrl.state.gamepad_mode ?? ctrl.pid_mode} (0db0:${ctrl.pid})`
              : status
                ? "not found"
                : "..."}
          </Field>
        </PanelSectionRow>
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

  return {
    name: TITLE,
    titleView: <div className={staticClasses.Title}>{TITLE}</div>,
    content: <Content />,
    icon: <MdVibration />,
    onDismount() {
      resumeHook?.unregister?.();
    },
  };
});
