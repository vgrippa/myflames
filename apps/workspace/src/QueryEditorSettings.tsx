// The Settings popover's Query editor group: one switch per setting of
// editorSettings.ts, stored on this computer through /api/settings/editor.
// Sidebar.tsx loads this group as its own chunk (lazy.tsx), so the store and
// this UI stay out of the entry chunk.
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
  // The row being saved, and the row whose last save failed ("" for none).
  const [saving, setSaving] = useState<EditorSettingKey | "">("");
  const [failed, setFailed] = useState<EditorSettingKey | "">("");
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  function save(setting: EditorSettingKey, on: boolean) {
    setFailed("");
    setSaving(setting);
    setEditorSetting(setting, on)
      .catch(() => { if (mounted.current) setFailed(setting); })
      .finally(() => { if (mounted.current) setSaving(""); });
  }
  return (
    <div className="settings-editor-group">
      <span className="section-label">Query editor</span>
      {EDITOR_SETTING_KEYS.map((setting) => {
        const note = failed === setting ? SAVE_ERROR : HINTS[setting] || "";
        const hintId = `editor-setting-${setting}-hint`;
        return (
          <Fragment key={setting}>
            <div className="settings-option-row">
              <Checkbox className="check-field session-toggle" checked={settings[setting]}
                busy={saving === setting} describedBy={note ? hintId : undefined}
                onChange={(on) => save(setting, on)}>
                {EDITOR_SETTING_LABELS[setting]}
              </Checkbox>
            </div>
            {note && (
              <p className={failed === setting ? "hint error" : "hint"} id={hintId}
                role={failed === setting ? "alert" : undefined}>{note}</p>
            )}
          </Fragment>
        );
      })}
    </div>
  );
}
