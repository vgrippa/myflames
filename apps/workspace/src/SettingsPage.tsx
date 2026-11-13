// The Settings page: a page in the main area (not a place in the sidebar's
// navigation), opened from the sidebar's gear, ⌘, (Ctrl+, on Windows and
// Linux) or the command palette. The section list on the left is the Server
// place's sub-navigation (kit.tsx SourceList); the section's controls sit on
// the right, and at 720 px and below the list becomes a compact list above
// them. Audit log and About myflames, at the bottom of the list, open what
// they have always opened. Every setting keeps its own storage and behavior;
// this page only lays the controls out. main.tsx loads it lazily, so the
// settings UI and editorSettings.ts stay out of the entry chunk.
import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { AboutDialog } from "./About";
import { ClientToolsDialog } from "./ClientTools";
import { Icon, type IconName } from "./components";
import { Checkbox, Select, SourceList } from "./kit";
import { loadUiPreferences, setUiPreference, useUiPreferences } from "./uiPreferences";
import { NewsSetting } from "./HomeNews";
import { QueryEditorSettings } from "./QueryEditorSettings";
import { AutoUpdateSetting, UpdateMenuItem } from "./UpdateBadge";
import { applyTheme, appliedThemeId, type ThemeId } from "./themes";
import { THEMES } from "./theme-picker";
import "./settings.css";

/** The sections with controls, in list order. Audit log and About myflames
 * are actions at the bottom of the list, not sections. */
export type SettingsSection = "appearance" | "editor" | "workspace" | "updates" | "clients";

const SECTIONS: { id: SettingsSection; label: string; icon: IconName }[] = [
  { id: "appearance", label: "Appearance", icon: "view" },
  { id: "editor", label: "Query editor", icon: "query" },
  { id: "workspace", label: "Workspace", icon: "home" },
  { id: "updates", label: "Updates and news", icon: "refresh" },
  { id: "clients", label: "Client tools", icon: "wrench" },
];
const sectionLabel = (id: SettingsSection) => SECTIONS.find((s) => s.id === id)?.label || "Appearance";

/** Appearance: a theme picker with one card per registry theme (themes.ts
 * applies and stores the choice). The cards draw their previews from the
 * registry's inline swatches, so each card shows the theme's own colors
 * whatever the current theme is. */
const THEME_GROUPS: { label: string; ids: ThemeId[] }[] = [
  { label: "myflames", ids: ["system", "light", "dark", "fire", "digital-rain", "neon-grid"] },
  { label: "Dark", ids: ["dracula", "nord", "solarized-dark", "tokyo-night", "gruvbox-dark", "catppuccin-mocha", "github-dark", "one-dark"] },
  { label: "Light", ids: ["solarized-light", "catppuccin-latte", "github-light"] },
];
const THEME_ORDER = THEME_GROUPS.flatMap(group => group.ids);

function ThemeCard({ id, checked, onSelect }: { id: ThemeId; checked: boolean; onSelect: (id: ThemeId) => void }) {
  const theme = THEMES.find(t => t.id === id)!;
  const accent = theme.swatches[theme.swatches.length - 1];
  return (
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      tabIndex={checked ? 0 : -1}
      className={checked ? "theme-card selected" : "theme-card"}
      data-theme-id={theme.id}
      onClick={() => onSelect(theme.id)}
    >
      <span className="theme-card-preview" style={{ background: theme.swatches[0] }} aria-hidden="true">
        <span className="theme-card-surface" style={{ background: theme.swatches[1] }}>
          <span className="theme-card-line" style={{ background: accent }} />
          {theme.syntax.map(color => <span key={color} className="theme-card-dot" style={{ background: color }} />)}
        </span>
      </span>
      <span className="theme-card-name">{theme.name}</span>
      <span className="theme-card-credit">{theme.credit}</span>
      {checked && (
        <span className="theme-card-check" style={{ background: accent, color: theme.checkInk }} aria-hidden="true">
          <Icon name="check" size={12} />
        </span>
      )}
    </button>
  );
}

function AppearanceSettings() {
  const [theme, setTheme] = useState<ThemeId>(appliedThemeId);
  const group = useRef<HTMLDivElement>(null);
  // Palette theme actions change the same root attribute.
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(appliedThemeId()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  const selectTheme = (next: ThemeId) => {
    applyTheme(next);
    setTheme(appliedThemeId());
  };
  // One tab stop on the checked card; the arrows, Home and End move and
  // select, wrapping through the visual order. Space and Enter click.
  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const keys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"];
    if (!keys.includes(event.key)) return;
    const card = (event.target as HTMLElement).closest<HTMLElement>("[role=radio]");
    const id = card?.dataset.themeId as ThemeId | undefined;
    if (!id) return;
    event.preventDefault();
    const index = THEME_ORDER.indexOf(id);
    const next = event.key === "Home" ? 0
      : event.key === "End" ? THEME_ORDER.length - 1
      : (index + (event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1) + THEME_ORDER.length) % THEME_ORDER.length;
    selectTheme(THEME_ORDER[next]);
    group.current?.querySelector<HTMLElement>(`[data-theme-id="${THEME_ORDER[next]}"]`)?.focus();
  };
  return (
    <div className="settings-theme-group">
      <div className="theme-picker" role="radiogroup" aria-label="Theme" ref={group} onKeyDown={onKeyDown}>
        {THEME_GROUPS.map(section => (
          <div className="theme-group" key={section.label}>
            <h3 className="theme-group-label">{section.label}</h3>
            <div className="theme-group-cards">
              {section.ids.map(id => (
                <ThemeCard key={id} id={id} checked={theme === id} onSelect={selectTheme} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function SettingsPage({
  logo,
  section,
  onSection,
  onBack,
  ready,
  restoreEnabled,
  skipRestore,
  onToggleRestore,
  onStartFresh,
  onAuditLog,
}: {
  logo: string;
  section: SettingsSection;
  onSection: (section: SettingsSection) => void;
  /** Esc returns to where the user was (the shell's Back). */
  onBack: () => void;
  ready: boolean;
  restoreEnabled: boolean;
  skipRestore: boolean;
  onToggleRestore: (on: boolean) => void;
  onStartFresh: () => void;
  /** Open the Audit log (writes sent through myflames, kept on this computer). */
  onAuditLog: () => void;
}) {
  const [aboutOpen, setAboutOpen] = useState(false);
  const [clientOpen, setClientOpen] = useState(false);
  const preferences = useUiPreferences();
  const [preferencesReady, setPreferencesReady] = useState(false);
  const [zoomError, setZoomError] = useState("");
  const zoomRequest = useRef(0);
  useEffect(() => {
    let active = true;
    void loadUiPreferences().then(() => { if (active) setPreferencesReady(true); }, () => {
      if (active) setZoomError("Could not load diagram zoom speed. Reopen Settings to try again.");
    });
    return () => { active = false; zoomRequest.current++; };
  }, []);
  function saveZoomSpeed(value: string) {
    const request = ++zoomRequest.current;
    setZoomError("");
    void setUiPreference("diagram_zoom_speed", Number(value)).catch(() => {
      if (request === zoomRequest.current) setZoomError("Could not save diagram zoom speed. Try again.");
    });
  }
  const aboutButton = useRef<HTMLButtonElement>(null);
  const clientButton = useRef<HTMLButtonElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  // A section change moves focus to the section's heading.
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
  }, [section]);
  // Esc returns to where the user was; an open dialog keeps its keys.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
      event.preventDefault();
      onBack();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onBack]);
  // Leaving the page (Back, Esc, a place) returns focus to the gear when the
  // page held it.
  useEffect(
    () => () => {
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !active.isConnected || active === document.body)
        document.querySelector<HTMLElement>(".settings-button")?.focus({ preventScroll: true });
    },
    [],
  );
  return (
    <div className="settings-page" data-section={section}>
      <div className="settings-nav">
        <SourceList
          label="Settings sections"
          groups={[{ label: "Settings", items: SECTIONS }]}
          current={section}
          onSelect={(id) => onSection(id as SettingsSection)}
          idPrefix="settings-section-"
        />
        <div className="source-group settings-nav-more">
          <h3 id="settings-nav-more-label">This computer</h3>
          <ul aria-labelledby="settings-nav-more-label">
            <li>
              <button type="button" className="source-item" onClick={onAuditLog}>
                <Icon name="audit" />
                <span className="source-label">Audit log</span>
              </button>
            </li>
            <li>
              <button
                ref={aboutButton}
                type="button"
                className="source-item"
                aria-haspopup="dialog"
                onClick={() => setAboutOpen(true)}
              >
                <Icon name="info" />
                <span className="source-label">About myflames</span>
              </button>
            </li>
          </ul>
        </div>
      </div>
      <section className="settings-panel" aria-labelledby="settings-section-title">
        <h1 className="visually-hidden">Settings</h1>
        <h2 id="settings-section-title" className="settings-panel-title" tabIndex={-1} ref={heading}>
          {sectionLabel(section)}
        </h2>
        {section === "appearance" && <AppearanceSettings />}
        {section === "editor" && <QueryEditorSettings />}
        {section === "workspace" && (
          <>
            <div className="settings-option-row">
              <Checkbox
                className="check-field session-toggle"
                checked={restoreEnabled}
                disabled={!ready}
                describedBy="session-toggle-hint"
                onChange={onToggleRestore}
              >
                Restore workspace on startup
              </Checkbox>
            </div>
            <p className="hint" id="session-toggle-hint">
              {skipRestore
                ? "Off for this launch (--no-restore); the saved workspace is kept."
                : "Off clears the saved workspace. Passwords are never saved in it."}
            </p>
            <div className="settings-option-row settings-select-row">
              <label htmlFor="diagram-zoom-speed">Diagram wheel zoom speed</label>
              <Select id="diagram-zoom-speed" value={preferences.diagram_zoom_speed}
                options={[[25, "Slowest"], [50, "Slow"], [100, "Normal"], [150, "Fast"], [200, "Fastest"]]}
                disabled={!ready || !preferencesReady} onChange={saveZoomSpeed}
                describedBy="diagram-zoom-speed-hint diagram-zoom-speed-error" />
            </div>
            <p className="hint" id="diagram-zoom-speed-hint">How far one mouse-wheel step zooms the Database diagram and the Mermaid preview.</p>
            <p className={zoomError ? "hint error" : "visually-hidden"} id="diagram-zoom-speed-error" role="alert">{zoomError}</p>
            <div className="settings-option-row settings-action-row">
              <button type="button" className="button" disabled={!ready}
                aria-haspopup="dialog" onClick={onStartFresh}>
                <Icon name="plus" size={16} />Start fresh
              </button>
            </div>
          </>
        )}
        {section === "updates" && (
          <>
            <AutoUpdateSetting />
            <NewsSetting />
            <UpdateMenuItem onChoose={() => {}} />
          </>
        )}
        {section === "clients" && (
          <>
            <p className="hint settings-lead">Which programs run queries, dumps, restores and MySQL Shell jobs for the server in use, and the choice per engine.</p>
            <div className="settings-option-row settings-action-row">
              <button
                ref={clientButton}
                type="button"
                className="button"
                aria-haspopup="dialog"
                onClick={() => setClientOpen(true)}
              >
                <Icon name="wrench" size={16} />
                Client tools…
              </button>
            </div>
          </>
        )}
      </section>
      {aboutOpen && (
        <AboutDialog
          logo={logo}
          returnFocus={aboutButton}
          onClose={() => setAboutOpen(false)}
        />
      )}
      {clientOpen && (
        <ClientToolsDialog
          returnFocus={clientButton}
          onClose={() => setClientOpen(false)}
        />
      )}
    </div>
  );
}
