import { ButtonItem, Field, PanelSection, PanelSectionRow, staticClasses } from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useState } from "react";
import { MdVibration } from "react-icons/md";

interface Status {
  controller: { pid_mode: string; bcd: string | null; can_reset: boolean } | null;
  last_result: string | null;
  captures: number;
  capture_dir: string;
}

interface Result {
  ok: boolean;
  msg: string;
  capture?: string;
}

const getStatus = callable<[], Status>("get_status");
const fixRumble = callable<[], Result>("fix");

const TITLE = "Claw Rumble Fix";

function Content() {
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const refresh = async () => {
    try {
      setStatus(await getStatus());
    } catch (error) {
      setMessage(`Backend error: ${error}`);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const fix = async () => {
    setBusy(true);
    setMessage("");
    try {
      const result = await fixRumble();
      setMessage(
        result.ok ? "Done. You should have felt a short pulse just now." : `Failed: ${result.msg}`,
      );
    } catch (error) {
      setMessage(`Failed: ${error}`);
    }
    setBusy(false);
    refresh();
  };

  const ctrl = status?.controller;
  const ready = !!ctrl && ctrl.can_reset;
  let hint = "Resets the controller MCU. The controller reconnects for a few seconds.";
  if (status && !ctrl) hint = "Controller not found on USB.";
  else if (status && !ready) hint = "This kernel's hid-msi driver has no reset attribute.";

  return (
    <PanelSection>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || !ready} description={hint} onClick={fix}>
          {busy ? "Working (about 15 s)..." : "Fix rumble"}
        </ButtonItem>
      </PanelSectionRow>
      {message && (
        <PanelSectionRow>
          <Field label={message} focusable />
        </PanelSectionRow>
      )}
      {status?.last_result && (
        <PanelSectionRow>
          <Field label="Last fix" description={status.last_result} focusable />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <Field
          label={`Captures: ${status?.captures ?? 0}`}
          description={`Each fix records the controller state before and after the reset in ${
            status?.capture_dir ?? "~/claw-rumble-captures"
          }`}
          focusable
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

export default definePlugin(() => ({
  name: TITLE,
  titleView: <div className={staticClasses.Title}>{TITLE}</div>,
  content: <Content />,
  icon: <MdVibration />,
  onDismount() {},
}));
