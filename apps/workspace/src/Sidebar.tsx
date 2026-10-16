// The workspace sidebar: places (where you go), open plans (what is open),
// and one status indicator with Settings (which also opens About myflames).
// Below 650 px it becomes a compact top bar whose places and open plans open
// in a drawer.
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { AboutDialog } from "./About";
import { time, type Plan } from "./api";
import { Icon, type IconName } from "./components";
import { Checkbox, useFocusScope } from "./kit";
import type { SaveStatus } from "./session";

export type Place = "home" | "plans" | "query" | "server" | "investigations" | "learn";
export const PLACES: { key: Place; label: string; icon: IconName }[] = [
  { key: "home", label: "Home", icon: "home" },
  { key: "plans", label: "Plans", icon: "plan" },
  { key: "query", label: "Query editor", icon: "query" },
  { key: "server", label: "Server", icon: "server" },
  { key: "investigations", label: "Investigations", icon: "investigation" },
  { key: "learn", label: "Learn", icon: "learn" },
];

export function statusText(status: SaveStatus) {
  switch (status.kind) {
    case "loading":
      return "Opening workspace…";
    case "off":
      return status.reason;
    case "pending":
      return "Workspace save pending";
    case "saving":
      return "Saving…";
    case "saved":
      return "Workspace autosaved";
    case "error":
      return `Not saved: ${status.reason}`;
  }
}

export function Sidebar({
  logo,
  place,
  plans,
  activeId,
  baselineId,
  saveStatus,
  ready,
  restoreEnabled,
  skipRestore,
  onGo,
  onOpenPlan,
  onRemovePlan,
  onToggleRestore,
  onAuditLog,
  expanded,
  onExpandedChange,
}: {
  /** Optional shell-controlled sidebar/drawer; legacy callers remain uncontrolled. */
  expanded?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
  logo: string;
  /** The place on screen; null while the workspace opens. */
  place: Place | null;
  plans: Plan[];
  /** The open plan shown in Plans (highlighted only while Plans is on screen). */
  activeId: string;
  baselineId: string;
  saveStatus: SaveStatus;
  ready: boolean;
  restoreEnabled: boolean;
  skipRestore: boolean;
  onGo: (place: Place) => void;
  onOpenPlan: (id: string) => void;
  onRemovePlan: (id: string) => void;
  onToggleRestore: (on: boolean) => void;
  /** Open the Audit log (writes sent through myflames, kept on this computer). */
  onAuditLog: () => void;
}) {
  const [localOpen, setLocalOpen] = useState(false);
  const drawerOpen = expanded ?? localOpen;
  const setDrawerOpen = (open: boolean) => {
    setLocalOpen(open);
    if (window.innerWidth <= 650) onExpandedChange?.(open);
  };
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);
  const sidebar = useRef<HTMLElement>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const drawer = useRef<HTMLDivElement>(null);
  const settingsButton = useRef<HTMLButtonElement>(null);
  const settings = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(() => window.innerWidth <= 650);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 650px)");
    const change = () => setNarrow(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  useFocusScope(sidebar, {
    open: drawerOpen && narrow,
    onClose: () => setDrawerOpen(false),
    initialFocus: () => drawer.current?.querySelector<HTMLElement>("button"),
  });
  useFocusScope(settings, {
    open: settingsOpen, onClose: () => setSettingsOpen(false),
    initialFocus: () => settings.current?.querySelector<HTMLInputElement>("input"),
    returnFocus: () => settingsButton.current,
  });
  const navRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (!settingsOpen) return;
    const outside = (event: MouseEvent) => {
      const target = event.target as Node;
      if (
        !settings.current?.contains(target) &&
        !settingsButton.current?.contains(target)
      )
        setSettingsOpen(false);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [settingsOpen]);
  useEffect(() => {
    if (!drawerOpen || window.innerWidth > 650) return;
    const outside = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!sidebar.current?.contains(target) && !menuButton.current?.contains(target) && !document.getElementById("window-sidebar-toggle")?.contains(target))
        setDrawerOpen(false);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [drawerOpen]);

  function closeSettings() {
    setSettingsOpen(false);
  }
  /** About replaces the Settings menu, like a desktop app's About window;
   * closing it returns focus to Settings. */
  function openAbout() {
    setSettingsOpen(false);
    setAboutOpen(true);
  }
  function go(next: Place) {
    setDrawerOpen(false);
    onGo(next);
  }
  /** Up and Down arrows move between places; Home and End jump to the ends. */
  function onNavKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const last = PLACES.length - 1;
    const target =
      event.key === "ArrowDown"
        ? Math.min(last, index + 1)
        : event.key === "ArrowUp"
          ? Math.max(0, index - 1)
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : -1;
    if (target < 0) return;
    event.preventDefault();
    navRefs.current[target]?.focus();
  }
  const status = statusText(saveStatus);
  return (
    <aside ref={sidebar} id="workspace-sidebar" className="sidebar" aria-label="Workspace" tabIndex={-1}>
      <div className="sidebar-head">
        <button className="brand" onClick={() => go("home")} aria-label="myflames Home">
          <img src={logo} alt="" width="22" height="22" />
          <span>
            myflames
          </span>
        </button>
        <button
          ref={menuButton}
          className="icon-button menu-toggle"
          aria-label="Menu"
          aria-expanded={drawerOpen}
          aria-controls="sidebar-drawer"
          onClick={() => setDrawerOpen(!drawerOpen)}
        >
          <Icon name={drawerOpen ? "close" : "menu"} size={16} />
        </button>
      </div>
      <div
        id="sidebar-drawer"
        ref={drawer}
        className={`sidebar-drawer${drawerOpen ? " open" : ""}`}

      >
        <nav className="main-nav" aria-label="Places">
          {PLACES.map(({ key, label, icon }, index) => (
            <button
              key={key}
              ref={(el) => (navRefs.current[index] = el)}
              className={place === key ? "active" : ""}
              aria-current={place === key ? "page" : undefined}
              onKeyDown={(e) => onNavKey(e, index)}
              onClick={() => go(key)}
            >
              <Icon name={icon} />
              {label}
            </button>
          ))}
        </nav>
        <section className="open-plans" aria-labelledby="open-plans-label">
          <h2 className="section-label" id="open-plans-label">
            Open plans <span className="count">{plans.length}</span>
          </h2>
          {!plans.length && (
            <p className="sidebar-empty">
              Plans you import or capture appear here.
            </p>
          )}
          <ul className="plan-list">
            {plans.map((p) => {
              const current = place === "plans" && p.id === activeId;
              return (
                <li className={`plan-item${current ? " selected" : ""}`} key={p.id}>
                  <button
                    className="plan-select"
                    aria-current={current ? "true" : undefined}
                    onClick={() => {
                      setDrawerOpen(false);
                      onOpenPlan(p.id);
                    }}
                  >
                    <Icon name="plan" />
                    <strong title={p.name}>{p.name}</strong>
                    <small>
                      {p.capture?.mode === "explain"
                        ? "Estimated plan"
                        : time(p.analysis.plan_summary.total_time_ms)}
                      {plans.length > 1 && p.id === baselineId && (
                        <span className="plan-baseline" title="Baseline"><Icon name="baseline" size={12} /><span className="visually-hidden">Baseline</span></span>
                      )}
                    </small>
                  </button>
                  <button
                    className="icon-button remove"
                    aria-label={`Remove ${p.name}`}
                    title={`Remove ${p.name}`}
                    onClick={() => onRemovePlan(p.id)}
                  >
                    <Icon name="close" size={12} />
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      </div>
      <div className="sidebar-footer">
        <p
          className={`session-status ${saveStatus.kind === "pending" ? "" : saveStatus.kind}`}
          role="status"
          aria-live="polite"
          data-state={saveStatus.kind}
          title={status}
        >
          <span>{status}</span>
        </p>
        <button
          ref={settingsButton}
          className="icon-button settings-button"
          aria-label="Settings"
          title="Settings"
          aria-haspopup="dialog"
          aria-expanded={settingsOpen}
          aria-controls="settings-popover"
          onClick={() => setSettingsOpen((open) => !open)}
        >
          <Icon name="settings" size={16} />
        </button>
        {settingsOpen && (
          <div
            id="settings-popover"
            ref={settings}
            className="settings-popover"
            role="dialog"
            tabIndex={-1}
            aria-labelledby="settings-title"

          >
            <div className="settings-heading">
              <h2 id="settings-title">Settings</h2>
              <button
                className="icon-button"
                aria-label="Close settings"
                onClick={closeSettings}
              >
                <Icon name="close" size={12} />
              </button>
            </div>
            <Checkbox
              className="check-field session-toggle"
              checked={restoreEnabled}
              disabled={!ready}
              describedBy="session-toggle-hint"
              onChange={onToggleRestore}
            >
              Restore workspace on startup
            </Checkbox>
            <p className="hint" id="session-toggle-hint">
              {skipRestore
                ? "Started with --no-restore. The saved workspace is kept for the next launch."
                : "Reopens open plans, query tabs, the connection in use, and where you were. Saved on this computer only; passwords are never saved. Turning it off stops autosave and clears the saved workspace."}
            </p>
            <div className="settings-menu">
              <button
                type="button"
                className="settings-item"
                onClick={() => {
                  setSettingsOpen(false);
                  setDrawerOpen(false);
                  onAuditLog();
                }}
              >
                <Icon name="audit" size={16} />
                Audit log
              </button>
              <button type="button" className="settings-item" aria-haspopup="dialog" onClick={openAbout}>
                <Icon name="info" size={16} />
                About myflames
              </button>
            </div>
          </div>
        )}
        {aboutOpen && (
          <AboutDialog
            logo={logo}
            returnFocus={settingsButton}
            onClose={() => setAboutOpen(false)}
          />
        )}
      </div>
    </aside>
  );
}
