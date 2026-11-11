// The Settings page's Query editor section: one switch per setting of
// editorSettings.ts, stored on this computer through /api/settings/editor.
// SettingsPage.tsx is its own chunk (lazy.tsx), so the store and this UI stay
// out of the entry chunk.
import { Fragment, useEffect, useRef, useState } from "react";
import { Checkbox } from "./kit";
import { MAC } from "./platform";
import {
  EDITOR_SETTING_KEYS,
  EDITOR_SETTING_LABELS,
  setEditorSetting,
  useEditorSettings,
  type EditorSettingKey,
} from "./editorSettings";

/** The extra line under a switch; most settings are clear from their label. */
const HINTS: Partial<Record<EditorSettingKey, string>> = {
  multi_cursor: MAC ? "Option-click adds a cursor." : "Alt+click adds a cursor.",
  completion_auto: "Ctrl+Space always shows suggestions.",
};
const SAVE_ERROR = "Could not save this setting. Try again.";

export function QueryEditorSettings() {
  const settings = useEditorSettings();
  const [saving, setSaving] = useState<Partial<Record<EditorSettingKey, boolean>>>({});
  const [failed, setFailed] = useState<Partial<Record<EditorSettingKey, boolean>>>({});
  const requests = useRef<Partial<Record<EditorSettingKey, number>>>({});
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  function save(setting: EditorSettingKey, on: boolean) {
    const request = (requests.current[setting] ?? 0) + 1;
    requests.current[setting] = request;
    const current = () => mounted.current && requests.current[setting] === request;
    setFailed(previous => ({ ...previous, [setting]: false }));
    setSaving(previous => ({ ...previous, [setting]: true }));
    setEditorSetting(setting, on)
      .catch(() => { if (current()) setFailed(previous => ({ ...previous, [setting]: true })); })
      .finally(() => { if (current()) setSaving(previous => ({ ...previous, [setting]: false })); });
  }
  return (
    <div className="settings-editor-group">
      <span className="section-label">Query editor</span>
      {EDITOR_SETTING_KEYS.map((setting) => {
        const note = HINTS[setting] || "";
        const errorId = `editor-setting-${setting}-error`;
        const hintId = `editor-setting-${setting}-hint`;
        return (
          <Fragment key={setting}>
            <div className="settings-option-row">
              <Checkbox className="check-field session-toggle" checked={settings[setting]}
                busy={!!saving[setting]} describedBy={[note ? hintId : "", failed[setting] ? errorId : ""].filter(Boolean).join(" ") || undefined}
                onChange={(on) => save(setting, on)}>
                {EDITOR_SETTING_LABELS[setting]}
              </Checkbox>
            </div>
            {note && (
              <p className="hint" id={hintId}>{note}</p>
            )}
            <p className={failed[setting] ? "hint error" : "visually-hidden"} id={errorId} role="alert">{failed[setting] ? SAVE_ERROR : ""}</p>
          </Fragment>
        );
      })}
    </div>
  );
}
