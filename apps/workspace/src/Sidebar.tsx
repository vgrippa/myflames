// The workspace sidebar: places (where you go), open plans (what is open),
// and one status indicator with the Settings gear (which opens the Settings
// page in the main area). Below 650 px it becomes a compact top bar whose
// places and open plans open in a drawer.
import { Fragment, useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { ReactNode } from "react";
import { time, type Plan } from "./api";
import { Icon, type IconName } from "./components";
import { CountBadge, Popover, closePopovers, useFocusScope } from "./kit";
import { UpdateBadge } from "./UpdateBadge";
import { applySidebarWidth, clampSidebarWidth, setUiPreference, SIDEBAR_DEFAULT, SIDEBAR_MIN, SIDEBAR_MAX, useUiPreferences } from "./uiPreferences";
import type { SaveStatus } from "./session";

export type Place = "home" | "server" | "workload" | "replication" | "cluster" | "proxysql" | "readyset" | "tuning" | "files" | "query" | "digest" | "diagram" | "schema" | "plans" | "investigations" | "learn";
export const PLACES: { key: Place; label: string; icon: IconName }[] = [
  { key: "home", label: "Home", icon: "home" },
  { key: "server", label: "Server", icon: "server" },
  { key: "workload", label: "Workload", icon: "statements" },
  { key: "replication", label: "Replication", icon: "replication" },
  { key: "cluster", label: "Cluster", icon: "cluster" },
  { key: "proxysql", label: "ProxySQL", icon: "proxy" },
  { key: "readyset", label: "Readyset", icon: "readyset" },
  { key: "tuning", label: "Tuning", icon: "tuning" },
  { key: "files", label: "File transfer", icon: "files" },
  { key: "query", label: "Query editor", icon: "query" },
  { key: "digest", label: "Query digest", icon: "analysis" },
  { key: "diagram", label: "Database diagram", icon: "diagram" },
  { key: "schema", label: "Schema versions", icon: "branches" },
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
  onGo,
  onOpenPlan,
  onRemovePlan,
  onOpenSettings,
  settingsOpen,
  expanded,
  narrow,
  onExpandedChange,
  placeBadges,
  placeMuted,
}: {
  /** Optional shell-controlled sidebar/drawer; legacy callers remain uncontrolled. */
  expanded?: boolean;
  /** The shell updates this together with expansion at the rail breakpoint. */
  narrow: boolean;
  onExpandedChange?: (expanded: boolean) => void;
  logo: string;
  /** The place on screen; null while the workspace opens. */
  place: Place | null;
  plans: Plan[];
  /** The open plan shown in Plans (highlighted only while Plans is on screen). */
  activeId: string;
  baselineId: string;
  saveStatus: SaveStatus;
  onGo: (place: Place) => void;
  onOpenPlan: (id: string) => void;
  onRemovePlan: (id: string) => void;
  /** Open the Settings page in the main area. */
  onOpenSettings: () => void;
  /** The Settings page is on screen: the gear reads as the current spot. */
  settingsOpen?: boolean;
  placeBadges?: Partial<Record<Place, ReactNode>>;
  /** Places that have nothing for the connection in use (Cluster on a server
   * that is not part of one), with the sentence that says so: the item looks
   * disabled but stays focusable and opens the place, which explains. */
  placeMuted?: Partial<Record<Place, string>>;
}) {
  const preferences = useUiPreferences();
  const [width, setWidth] = useState(preferences.sidebar_width);
  const drag = useRef<{ pointer: number; x: number; width: number } | null>(null);
  useEffect(() => { setWidth(preferences.sidebar_width); }, [preferences.sidebar_width]);
  const previewWidth = (value: number) => {
    const next = clampSidebarWidth(value);
    setWidth(next);
    applySidebarWidth(next);
    return next;
  };
  const saveWidth = (value: number) => {
    void setUiPreference("sidebar_width", previewWidth(value)).catch(() => {});
  };
  const [localOpen, setLocalOpen] = useState(false);
  const drawerOpen = expanded ?? localOpen;
  const setDrawerOpen = (open: boolean) => {
    setLocalOpen(open);
    if (narrow) onExpandedChange?.(open);
  };
  const sidebar = useRef<HTMLElement>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const drawer = useRef<HTMLDivElement>(null);
  useFocusScope(sidebar, {
    open: drawerOpen && narrow,
    onClose: () => setDrawerOpen(false),
    initialFocus: () => drawer.current?.querySelector<HTMLElement>("button"),
    returnFocus: () => document.getElementById("window-sidebar-toggle"),
  });
  const navRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (!drawerOpen || !narrow) return;
    const outside = (event: MouseEvent) => {
      const target = event.target as Node;
      const toggle = document.getElementById("window-sidebar-toggle");
      if (!sidebar.current?.contains(target) && !menuButton.current?.contains(target) && !toggle?.contains(target)) {
        setDrawerOpen(false);
        requestAnimationFrame(() => {
          const focused = document.activeElement;
          if (!focused || focused === document.body || sidebar.current?.contains(focused)) toggle?.focus({ preventScroll: true });
        });
      }
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [drawerOpen, narrow]);

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
      {!narrow && <div className="sidebar-resizer" role="separator" tabIndex={0}
        aria-label="Sidebar width" aria-orientation="vertical"
        aria-valuemin={SIDEBAR_MIN} aria-valuemax={SIDEBAR_MAX} aria-valuenow={width}
        aria-valuetext={`${width} pixels`} title="Drag to resize sidebar. Double-click to reset."
        onPointerDown={event => {
          if (event.button !== 0) return;
          event.preventDefault();
          event.currentTarget.focus();
          event.currentTarget.setPointerCapture(event.pointerId);
          drag.current = { pointer: event.pointerId, x: event.clientX, width };
        }}
        onPointerMove={event => {
          if (drag.current?.pointer === event.pointerId) previewWidth(drag.current.width + event.clientX - drag.current.x);
        }}
        onPointerUp={event => {
          if (drag.current?.pointer !== event.pointerId) return;
          const next = drag.current.width + event.clientX - drag.current.x;
          drag.current = null;
          event.currentTarget.releasePointerCapture(event.pointerId);
          saveWidth(next);
        }}
        onLostPointerCapture={() => { if (drag.current) { drag.current = null; previewWidth(preferences.sidebar_width); } }}
        onPointerCancel={() => { drag.current = null; previewWidth(preferences.sidebar_width); }}
        onDoubleClick={() => saveWidth(SIDEBAR_DEFAULT)}
        onKeyDown={event => {
          const next = event.key === "Home" || event.key === "End" ? SIDEBAR_DEFAULT
            : event.key === "ArrowLeft" ? width - (event.shiftKey ? 24 : 8)
            : event.key === "ArrowRight" ? width + (event.shiftKey ? 24 : 8) : null;
          if (next !== null) { event.preventDefault(); saveWidth(next); }
        }} />}
      <div className="sidebar-head">
        <div className="brand-group">
          <button className="brand" onClick={() => go("home")} aria-label="myflames Home">
            <img src={logo} alt="" width="22" height="22" />
            <span className="brand-name">myflames</span>
          </button>
          <UpdateBadge />
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
              title={placeMuted?.[key] ? (narrow && !drawerOpen ? `${label}: ${placeMuted[key]}` : placeMuted[key]) : (narrow && !drawerOpen ? label : undefined)}
              data-muted={placeMuted?.[key] ? "true" : undefined}
              onKeyDown={(e) => onNavKey(e, index)}
              onClick={() => go(key)}
            >
              <Icon name={icon} />
              <span className="sidebar-label">{label}</span>
              {placeMuted?.[key] && <span className="visually-hidden">, {placeMuted[key]}</span>}
              {placeBadges?.[key]}
            </button>
            </Fragment>
          ))}
        </nav>
        <div className="rail-open-plans">
          <Popover
            label={`Open plans (${plans.length})`}
            buttonTitle={`Open plans (${plans.length})`}
            buttonClassName="rail-plans-button"
            trigger={<><Icon name="plan" /><CountBadge count={plans.length} /></>}
          >
            <div className="rail-plans-popover">
              <p className="popover-title">Open plans ({plans.length})</p>
              {!plans.length ? (
                <p className="sidebar-empty">Plans you import or capture appear here.</p>
              ) : (
                <ul className="plan-list">
                  {plans.map((p) => {
                    const current = place === "plans" && p.id === activeId;
                    return (
                      <li className={`plan-item${current ? " selected" : ""}`} key={p.id}>
                        <button
                          type="button"
                          className="plan-select"
                          aria-current={current ? "true" : undefined}
                          onClick={() => {
                            closePopovers();
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
                          type="button"
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
              )}
            </div>
          </Popover>
        </div>
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
                    type="button"
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
          className="icon-button settings-button"
          aria-label="Settings"
          title="Settings"
          aria-current={settingsOpen ? "page" : undefined}
          onClick={() => {
            setDrawerOpen(false);
            onOpenSettings();
          }}
        >
          <Icon name="settings" size={16} />
        </button>
      </div>
    </aside>
  );
}
