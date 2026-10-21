// The workspace sidebar: places (where you go), open plans (what is open),
// and one status indicator with Settings (which also opens About myflames).
// Below 650 px it becomes a compact top bar whose places and open plans open
// in a drawer.
import { Fragment, useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { ReactNode } from "react";
import { AboutDialog } from "./About";
import { time, type Plan } from "./api";
import { Icon, type IconName } from "./components";
import { Checkbox, useFocusScope } from "./kit";
import { NewsSetting } from "./HomeNews";
import type { SaveStatus } from "./session";

export type Place = "home" | "server" | "workload" | "replication" | "cluster" | "proxysql" | "tuning" | "query" | "plans" | "investigations" | "learn";
export const PLACES: { key: Place; label: string; icon: IconName }[] = [
  { key: "home", label: "Home", icon: "home" },
  { key: "server", label: "Server", icon: "server" },
  { key: "workload", label: "Workload", icon: "statements" },
  { key: "replication", label: "Replication", icon: "replication" },
  { key: "cluster", label: "Cluster", icon: "cluster" },
  { key: "proxysql", label: "ProxySQL", icon: "proxy" },
  { key: "tuning", label: "Tuning", icon: "tuning" },
  { key: "query", label: "Query editor", icon: "query" },
  { key: "plans", label: "Plans", icon: "plan" },
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
    case "saving":
      return "Saving…";
    case "saved":
      return "All changes saved";
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
  queries = [],
  activeQueryId,
  onOpenQuery,
  onRemoveQuery,
  expanded,
  onExpandedChange,
  onNewSession,
  placeBadges,
  placeMuted,
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
  queries?: { id: string; name: string }[];
  activeQueryId?: string;
  onOpenQuery?: (id: string) => void;
  onRemoveQuery?: (id: string) => void;
  onNewSession?: () => void;
  placeBadges?: Partial<Record<Place, ReactNode>>;
  /** Places that have nothing for the connection in use (Cluster on a server
   * that is not part of one), with the sentence that says so: the item looks
   * disabled but stays focusable and opens the place, which explains. */
  placeMuted?: Partial<Record<Place, string>>;
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
  const [theme, setTheme] = useState<"system" | "light" | "dark">(() => {
    try {
      const saved = localStorage.getItem("myflames.theme");
      if (saved === "light" || saved === "dark" || saved === "system") return saved;
    } catch {}
    return "system";
  });
  const selectTheme = (next: "system" | "light" | "dark") => {
    setTheme(next);
    try {
      localStorage.setItem("myflames.theme", next);
    } catch {}
    if (next === "system") {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = next;
    }
  };
  useEffect(() => {
    try {
      const saved = localStorage.getItem("myflames.theme");
      if (saved === "light" || saved === "dark") {
        document.documentElement.dataset.theme = saved;
      } else {
        delete document.documentElement.dataset.theme;
      }
    } catch {}
  }, []);
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
        <div className="brand-group">
          <button className="brand" onClick={() => go("home")} aria-label="myflames Home">
            <img src={logo} alt="" width="22" height="22" />
            <span className="brand-name">myflames</span>
          </button>
        </div>
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
            <Fragment key={key}>
            {key === "query" && <h2 className="nav-group-label">SQL workspace</h2>}
            {key === "investigations" && <h2 className="nav-group-label">Library</h2>}
            <button
              key={key}
              ref={(el) => (navRefs.current[index] = el)}
              data-place={key}
              className={[place === key ? "active" : "", placeMuted?.[key] ? "is-muted" : ""].filter(Boolean).join(" ")}
              aria-current={place === key ? "page" : undefined}
              title={placeMuted?.[key] || undefined}
              data-muted={placeMuted?.[key] ? "true" : undefined}
              onKeyDown={(e) => onNavKey(e, index)}
              onClick={() => go(key)}
            >
              <Icon name={icon} />
              {label}
              {placeMuted?.[key] && <span className="visually-hidden">, {placeMuted[key]}</span>}
              {placeBadges?.[key]}
            </button>
            </Fragment>
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
        {queries.length > 0 && <section className="open-queries" aria-labelledby="open-queries-label">
          <h2 className="section-label" id="open-queries-label">Open queries <span className="count">{queries.length}</span></h2>
          <ul className="query-document-list">
            {queries.map(query => {
              const current = place === "query" && query.id === activeQueryId;
              return (
                <li key={query.id} className={`query-item${current ? " selected" : ""}`}>
                  <button type="button" className="query-document" aria-current={current ? "true" : undefined}
                    onClick={() => { setDrawerOpen(false); onOpenQuery?.(query.id); }}>
                    <Icon name="query" /><span title={query.name}>{query.name}</span>
                  </button>
                  {onRemoveQuery && (
                    <button
                      type="button"
                      className="icon-button remove"
                      aria-label={`Close ${query.name}`}
                      title={`Close ${query.name}`}
                      onClick={() => onRemoveQuery(query.id)}
                    >
                      <Icon name="close" size={12} />
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </section>}
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
            {/* A menu-style popover: Escape and a click outside close it. */}
            <h2 id="settings-title" className="visually-hidden">Settings</h2>
            <div className="settings-theme-group">
              <span className="section-label">Appearance</span>
              <div className="segmented theme-segmented" role="group" aria-label="Appearance theme">
                <button
                  type="button"
                  className={theme === "system" ? "active" : ""}
                  aria-pressed={theme === "system"}
                  onClick={() => selectTheme("system")}
                >
                  System
                </button>
                <button
                  type="button"
                  className={theme === "light" ? "active" : ""}
                  aria-pressed={theme === "light"}
                  onClick={() => selectTheme("light")}
                >
                  Light
                </button>
                <button
                  type="button"
                  className={theme === "dark" ? "active" : ""}
                  aria-pressed={theme === "dark"}
                  onClick={() => selectTheme("dark")}
                >
                  Dark
                </button>
              </div>
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
                : "Off clears the saved workspace. Passwords are never part of the workspace."}
            </p>
            <NewsSetting />
            <div className="settings-menu">
              {onNewSession && (
                <button
                  type="button"
                  className="settings-item"
                  onClick={() => {
                    setSettingsOpen(false);
                    setDrawerOpen(false);
                    onNewSession();
                  }}
                >
                  <Icon name="refresh" size={16} />
                  New session
                </button>
              )}
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
