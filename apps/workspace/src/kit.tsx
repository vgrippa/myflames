// Shared components for data views (the Server place first): a source list
// for sub-navigation, a view toolbar, a sortable and selectable data table,
// grouped one-line rows, compact metric tiles, an inspector for the selected
// row (a resizable pane, or a bottom sheet on narrow layouts), info popovers,
// a notes button, status dots and pills, count badges, a segmented control,
// a confirmation sheet, a prerequisite gate, and an empty state.
//
// Design rules (.claude/skills/web-design): tokens only, one component per
// job, explanations behind ⓘ or in the inspector, never always-visible prose.
import {
  Children,
  cloneElement,
  isValidElement,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { Icon, isIconName, type IconName } from "./components";
import { createPortal } from "react-dom";
import "./kit.css";

// Only the innermost open surface owns keyboard navigation. A drawer may
// contain Settings, and a popover may be opened from a confirmation sheet.
const focusScopes: HTMLElement[] = [];
const FOCUSABLE = 'button, [href], input, select, textarea, summary, [tabindex]';

/** Shared lifecycle for dialogs, popovers, drawers and context menus. Callers
 * provide a focusable container (tabIndex=-1) for empty or disabled content.
 * Mutable callbacks do not restart the scope or overwrite its opener. */
export function useFocusScope<T extends HTMLElement>(ref: RefObject<T>, options: {
  open: boolean;
  onClose: () => void;
  initialFocus?: () => HTMLElement | null | undefined;
  returnFocus?: () => HTMLElement | null | undefined;
  menu?: boolean;
}) {
  const latest = useRef(options);
  latest.current = options;
  useEffect(() => {
    const root = ref.current;
    if (!options.open || !root) return;
    const opener = latest.current.returnFocus?.() || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const dialog = root instanceof HTMLDialogElement ? root : null;
    if (dialog && !dialog.open) dialog.showModal();
    focusScopes.push(root);
    const visible = (el: HTMLElement) => !el.matches(':disabled, [aria-disabled="true"]') &&
      !el.closest('[hidden], [inert], [aria-hidden="true"]') && !!el.getClientRects().length &&
      getComputedStyle(el).visibility !== 'hidden';
    const items = () => Array.from(root.querySelectorAll<HTMLElement>(latest.current.menu ? '[role="menuitem"]' : FOCUSABLE))
      .filter((el) => (latest.current.menu || el.tabIndex >= 0) && visible(el));
    const focusFirst = () => {
      const preferred = latest.current.initialFocus?.();
      (preferred && visible(preferred) ? preferred : items()[0] || root).focus({ preventScroll: true });
    };
    // Popovers are positioned in a layout effect; retry once if initially hidden.
    focusFirst();
    const frame = requestAnimationFrame(() => {
      if (focusScopes[focusScopes.length - 1] === root && !root.contains(document.activeElement)) focusFirst();
    });
    const key = (event: globalThis.KeyboardEvent) => {
      if (focusScopes[focusScopes.length - 1] !== root) return;
      // A native modal dialog holding focus (About) takes precedence, even when
      // it is rendered inside this scope (About inside the phone drawer); a
      // scope inside that dialog (a popover in a sheet) still handles its keys.
      const native = document.activeElement?.closest('dialog[open]');
      if (native && native !== root && !native.contains(root)) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopImmediatePropagation();
        latest.current.onClose();
        return;
      }
      const nodes = items();
      const at = nodes.indexOf(document.activeElement as HTMLElement);
      if (latest.current.menu && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? nodes.length - 1 :
          event.key === 'ArrowDown' ? (at + 1) % nodes.length : (at <= 0 ? nodes.length - 1 : at - 1);
        (nodes[next] || root).focus();
      } else if (event.key === 'Tab') {
        if (!nodes.length || at < 0 || (event.shiftKey ? at === 0 : at === nodes.length - 1)) {
          event.preventDefault();
          (event.shiftKey ? nodes[nodes.length - 1] || root : nodes[0] || root).focus();
        }
        event.stopPropagation();
      }
    };
    document.addEventListener('keydown', key, true);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener('keydown', key, true);
      const wasTop = focusScopes[focusScopes.length - 1] === root;
      const at = focusScopes.lastIndexOf(root);
      if (at >= 0) focusScopes.splice(at, 1);
      const focused = document.activeElement;
      const restore = wasTop && (root.contains(focused) || focused === document.body || !focused?.isConnected);
      if (dialog?.open) dialog.close();
      // Do not steal focus from an action that deliberately opened a new surface.
      if (restore && opener?.isConnected && visible(opener)) opener.focus({ preventScroll: true });
    };
  }, [ref, options.open]);
}

// ---- Status --------------------------------------------------------------

/** Status roles: never color alone, always a dot plus a short word. */
export type Tone = "ok" | "watch" | "problem" | "info" | "unknown" | "off";
export const TONE_TEXT: Record<Tone, string> = {
  ok: "OK",
  watch: "Watch",
  problem: "Problem",
  info: "No threshold",
  unknown: "Unknown",
  off: "Off",
};
const TONE_RANK: Record<Tone, number> = { problem: 0, watch: 1, off: 2, unknown: 3, info: 4, ok: 5 };
/** Sort key: problems first, then watch, then everything else, OK last. */
export function toneRank(tone: Tone) {
  return TONE_RANK[tone] ?? 6;
}
/** The worst of several statuses (OK when there are none). */
export function worstTone(tones: Tone[]): Tone {
  return tones.reduce<Tone>((worst, t) => (toneRank(t) < toneRank(worst) ? t : worst), "ok");
}

export function StatusDot({ tone, label }: { tone: Tone; label?: string }) {
  return (
    <span className={`status-indicator is-${tone}`}><span className={`status-dot is-${tone}`} aria-hidden="true" />{label || TONE_TEXT[tone]}</span>
  );
}

/** A dot and a short word (OK, Watch, Problem, Off…). In a status column
 * (`quiet`), the normal states step back so only the others draw the eye: OK
 * draws nothing and No threshold a faint dash, while the word stays for
 * screen readers, hover and text search. A custom label is always shown. */
export function StatusPill({ tone, label, title, quiet = false }: { tone: Tone; label?: string; title?: string; quiet?: boolean }) {
  const word = label || TONE_TEXT[tone];
  if (quiet && (tone === "ok" || tone === "info") && word === TONE_TEXT[tone]) {
    return (
      <span className={`status-pill is-${tone} is-quiet`} title={title || word}>
        <span className="visually-hidden">{word}</span>
      </span>
    );
  }
  return (
    <span className={`status-pill is-${tone}`} title={title}>
      <span className="status-dot" aria-hidden="true" />
      {label || TONE_TEXT[tone]}
    </span>
  );
}

export type BadgeTone = "neutral" | "warn" | "danger" | "ok";
export function CountBadge({ count, tone = "neutral", label }: { count: number; tone?: BadgeTone; label?: string }) {
  return (
    <span className={`count-badge is-${tone}`} title={label}>
      {count.toLocaleString()}
    </span>
  );
}

/** The marker beside a navigation row (Server and Workload views, sidebar
 * places): one system everywhere. A status dot carries state (problem red,
 * watch amber, unknown and off a gray ring; OK draws nothing), and a count is
 * always a neutral pill beside it, so color means state and only state. The
 * words reach screen readers through `text` (or the row's own hidden text). */
export function NavMarker({ tone, count, countTone = "neutral", text }: {
  tone?: Tone;
  count?: number;
  countTone?: BadgeTone;
  text?: string;
}) {
  return (
    <>
      <span className="nav-marker" aria-hidden="true">
        {tone && <span className={`status-dot is-${tone}`} />}
        {count !== undefined && <CountBadge count={count} tone={countTone} />}
      </span>
      {text && <span className="visually-hidden">, {text}</span>}
    </>
  );
}
/** The state a count stands for in a navigation marker. */
export function countState(tone: BadgeTone): Tone | undefined {
  return tone === "danger" ? "problem" : tone === "warn" ? "watch" : undefined;
}

/** A small flag in a row (≤ 3 per row); the meaning is on hover and for screen readers. */
export function Pill({ children, tone = "neutral", title }: { children: ReactNode; tone?: BadgeTone | "info"; title?: string }) {
  return (
    <span className={`pill is-${tone}`} title={title}>
      {children}
      {title && <span className="visually-hidden">: {title}</span>}
    </span>
  );
}

// ---- Popovers: ⓘ help and notes --------------------------------------------

/** Only one popover is open at a time across the page. */
const POPOVER_EVENT = "myflames-popover";

/** A button that opens a small, non-modal popover (≤ 320 px, closes with
 * Escape, a click outside, or another popover). */
export function Popover({
  label,
  trigger,
  className = "",
  buttonClassName = "info-button",
  wide,
  buttonTitle,
  disabled = false,
  menu = false,
  children,
}: {
  /** Accessible name of the button and the popover. */
  label: string;
  /** Button content; an ⓘ icon by default. */
  trigger?: ReactNode;
  className?: string;
  buttonClassName?: string;
  /** Up to 400 px wide (notes lists). */
  wide?: boolean;
  /** Tooltip of a button whose content is an icon. */
  buttonTitle?: string;
  /** Disables the trigger and closes an open panel. */
  disabled?: boolean;
  /** Action menus use arrow navigation; forms keep dialog navigation. */
  menu?: boolean;
  children: ReactNode;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [style, setStyle] = useState<CSSProperties>({ visibility: "hidden" });
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  useFocusScope(panel, { open, onClose: () => setOpen(false), returnFocus: () => button.current, menu });
  useEffect(() => {
    if (!open) return;
    window.dispatchEvent(new CustomEvent(POPOVER_EVENT, { detail: id }));
    const other = (e: Event) => {
      if ((e as CustomEvent).detail !== id) setOpen(false);
    };
    const outside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!panel.current?.contains(target) && !button.current?.contains(target)) setOpen(false);
    };
    window.addEventListener(POPOVER_EVENT, other);
    document.addEventListener("mousedown", outside);
    return () => {
      window.removeEventListener(POPOVER_EVENT, other);
      document.removeEventListener("mousedown", outside);
    };
  }, [open, id]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  // Fixed position below (or above) the button, kept inside the viewport.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const b = button.current?.getBoundingClientRect();
      const p = panel.current;
      if (!b || !p) return;
      const width = Math.min(wide ? 400 : 320, window.innerWidth - 16);
      const left = Math.max(8, Math.min(b.left - 12, window.innerWidth - width - 8));
      const height = p.offsetHeight;
      const below = b.bottom + 6;
      const top = below + height > window.innerHeight - 8 && b.top - height - 6 > 8 ? b.top - height - 6 : below;
      const boundedTop = Math.max(8, Math.min(top, window.innerHeight - Math.min(height, window.innerHeight - 16) - 8));
      setStyle({ left, top: boundedTop, width, maxHeight: window.innerHeight - boundedTop - 8 });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, wide]);
  return (
    <span className={`popover-anchor ${className}`}>
      <button
        ref={button}
        type="button"
        className={buttonClassName}
        aria-label={label}
        aria-haspopup={menu ? "menu" : "dialog"}
        disabled={disabled}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        title={trigger ? buttonTitle : label}
        onClick={() => {
          setStyle({ visibility: "hidden" });
          setOpen((o) => !o);
        }}
      >
        {trigger || <Icon name="info" size={15} />}
      </button>
      {open && (
        <div ref={panel} id={id} className={`popover${wide ? " is-wide" : ""}`} tabIndex={-1} role={menu ? "menu" : "dialog"} aria-label={label} style={style}>
          {children}
        </div>
      )}
    </span>
  );
}

/** Close every open popover (after an action inside one moved on). */
export function closePopovers() {
  window.dispatchEvent(new CustomEvent(POPOVER_EVENT, { detail: "" }));
}

/** ⓘ next to a title: what this is and how to read it. */
export function InfoPopover({ label, children }: { label: string; children: ReactNode }) {
  return <Popover label={label}>{children}</Popover>;
}

/** "ⓘ 3 notes": engine differences, caveats and privileges, on demand. */
export function NotesButton({ notes, label = "Notes about this read" }: { notes: ReactNode[]; label?: string }) {
  if (!notes.length) return null;
  return (
    <Popover
      label={label}
      wide
      className="notes-anchor"
      buttonClassName="notes-button"
      trigger={
        <>
          <Icon name="info" size={14} />
          {notes.length} {notes.length === 1 ? "note" : "notes"}
        </>
      }
    >
      <h3 className="popover-title">{label}</h3>
      <ul className="popover-notes">
        {notes.map((n, i) => (
          <li key={i}>{n}</li>
        ))}
      </ul>
    </Popover>
  );
}

// ---- Segmented control -----------------------------------------------------

export type Segment<T extends string> = {
  value: T;
  label: ReactNode;
  count?: number | null;
  disabled?: boolean;
  title?: string;
};
/** Modes of the same content (Active / All, Unused / Redundant…). */
export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  className = "",
}: {
  label: string;
  value: T;
  options: Segment<T>[];
  onChange: (value: T) => void;
  className?: string;
}) {
  const group = useRef<HTMLDivElement>(null);
  function onKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const enabled = options.map((o, i) => (o.disabled ? -1 : i)).filter((i) => i >= 0);
    const at = enabled.indexOf(index);
    const next = enabled[(at + step + enabled.length) % enabled.length];
    onChange(options[next].value);
    group.current?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
  }
  return (
    <div ref={group} className={`segmented ${className}`} role="group" aria-label={label}>
      {options.map((o, i) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={o.value === value}
          disabled={o.disabled}
          title={o.title}
          data-value={o.value}
          onClick={() => onChange(o.value)}
          onKeyDown={(e) => onKey(e, i)}
        >
          {o.label}
          {o.count !== undefined && o.count !== null && <span className="segment-count">{o.count.toLocaleString()}</span>}
        </button>
      ))}
    </div>
  );
}

// ---- Controls: one height, one look -----------------------------------------

/** A native select with the shared look (custom chevron; keyboard, screen
 * readers and the platform menu unchanged). */
export function Select<V extends string | number>({
  value,
  options,
  onChange,
  label,
  disabled,
  id,
  className = "",
  describedBy,
}: {
  value: V;
  options: [V, string][];
  onChange: (value: string) => void;
  /** Accessible name when no visible label wraps the select. */
  label?: string;
  disabled?: boolean;
  id?: string;
  className?: string;
  describedBy?: string;
}) {
  return (
    <select
      id={id}
      className={`select ${className}`.trim()}
      aria-label={label}
      aria-describedby={describedBy}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    >
      {options.map(([v, text]) => (
        <option key={String(v)} value={v}>
          {text}
        </option>
      ))}
    </select>
  );
}

/** A checkbox and its label: a drawn box over the native input. */
export function Checkbox({
  checked,
  onChange,
  children,
  disabled,
  className = "",
  describedBy,
  title,
  id,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  children: ReactNode;
  disabled?: boolean;
  className?: string;
  describedBy?: string;
  title?: string;
  id?: string;
}) {
  return (
    <label className={`checkbox ${className}`.trim()} title={title}>
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-describedby={describedBy}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{children}</span>
    </label>
  );
}

/** A number field without spin buttons (arrow keys still step), with its
 * unit inside the field ("s", "rows"). *onChange* gets the typed text. */
export function NumberField({
  value,
  onChange,
  label,
  unit,
  min,
  max,
  step,
  disabled,
  id,
  className = "",
  describedBy,
}: {
  value: number | string;
  onChange: (value: string) => void;
  /** Accessible name when no visible label wraps the field. */
  label?: string;
  unit?: string;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
  id?: string;
  className?: string;
  describedBy?: string;
}) {
  return (
    <span className={`number-field${disabled ? " is-disabled" : ""} ${className}`.trim()}>
      <input
        id={id}
        type="number"
        inputMode="numeric"
        aria-label={label}
        aria-describedby={describedBy}
        value={value}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
      {unit && (
        <span className="number-unit" aria-hidden="true">
          {unit}
        </span>
      )}
    </span>
  );
}

/** A toolbar button: one control height, an optional icon before the text. */
export function ToolbarButton({
  icon,
  primary,
  className = "",
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { icon?: IconName; primary?: boolean }) {
  return (
    <button type="button" className={`button${primary ? " primary" : ""} ${className}`.trim()} {...rest}>
      {icon && <Icon name={icon} size={15} />}
      {children}
    </button>
  );
}

/** An icon-only button: its name is the label (also its tooltip). */
export function IconButton({
  icon,
  label,
  size = 16,
  className = "",
  title,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { icon: IconName; label: string; size?: number }) {
  return (
    <button type="button" className={`icon-button ${className}`.trim()} aria-label={label} title={title ?? label} {...rest}>
      <Icon name={icon} size={size} />
    </button>
  );
}

// ---- Compound controls ----------------------------------------------------

/** Two adjacent buttons: the primary action and independently named options.
 * disabled disables both; the arrow exposes aria-expanded while open. Children
 * are ordinary form controls/actions inside Popover, dismissed by Escape or
 * outside click. Use closePopovers() after an option that completes an action. */
export function SplitButton({ label, icon, primary, disabled, onClick, popoverLabel, children }: {
  label: string; icon?: IconName; primary?: boolean; disabled?: boolean;
  onClick: () => void; popoverLabel: string; children: ReactNode;
}) {
  return <div className={`split-button${primary ? " is-primary" : ""}${disabled ? " is-disabled" : ""}`} role="group" aria-label={label}>
    <ToolbarButton icon={icon} primary={primary} disabled={disabled} onClick={onClick}>{label}</ToolbarButton>
    <Popover label={popoverLabel} buttonTitle={popoverLabel} disabled={disabled}
      buttonClassName={`button split-button-options${primary ? " primary" : ""}`}
      trigger={<span className="down-chevron"><Icon name="disclosure" size={12} /></span>}>
      {children}
    </Popover>
  </div>;
}

/** Aligned form rows. Rows stack below 650 px; className is additive. */
export function FormGrid({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`form-grid ${className}`.trim()}>{children}</div>;
}
/** One labelled control/group. htmlFor targets the control; hint and error
 * are linked to direct input/select/textarea children automatically. For a
 * composite control pass its describedBy prop using `${htmlFor}-hint` or
 * `${htmlFor}-error`. An error replaces the hint and is announced. */
export function FormRow({ label, htmlFor, hint, error, children }: {
  label: ReactNode; htmlFor?: string; hint?: ReactNode; error?: ReactNode; children: ReactNode;
}) {
  const generated = useId();
  const id = htmlFor || generated;
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined;
  const content = Children.map(children, (child) => {
    if (!isValidElement<{ "aria-describedby"?: string; "aria-invalid"?: boolean }>(child) ||
        !["input", "select", "textarea"].includes(child.type as string)) return child;
    return cloneElement(child, {
      "aria-describedby": [child.props["aria-describedby"], describedBy].filter(Boolean).join(" ") || undefined,
      "aria-invalid": error ? true : child.props["aria-invalid"],
    });
  });
  return <div className={`form-row${error ? " has-error" : ""}`}>
    <label htmlFor={htmlFor}>{label}</label>
    <div className="form-row-control">{content}
      {(error || hint) && <p id={describedBy} className={error ? "form-error" : "hint"} role={error ? "alert" : undefined}>{error || hint}</p>}
    </div>
  </div>;
}

/** Stays at the bottom of the owning pane. Zero/nonfinite counts render
 * nothing; review/discard callbacks never apply changes themselves. */
export function PendingBar({ count, summary, onReview, onDiscard }: {
  count: number; summary?: string; onReview: () => void; onDiscard: () => void;
}) {
  if (!Number.isFinite(count) || count <= 0) return null;
  const pending = Math.floor(count);
  if (!pending) return null;
  return <div className="pending-bar" role="region" aria-label="Pending changes">
    <span role="status">{pending} {pending === 1 ? "change" : "changes"} pending{summary ? ` · ${summary}` : ""}</span>
    <ToolbarButton primary onClick={onReview}>Review changes</ToolbarButton>
    <ToolbarButton onClick={onDiscard}>Discard</ToolbarButton>
  </div>;
}

/** A persisted first-pane size in CSS pixels. horizontal = side by side;
 * vertical = top/bottom. The container must receive a bounded width/height.
 * min/max are clamped to available space on resize, leaving 48 px for pane 2.
 * Arrows resize 8 px (Shift: 32), Home/End use the limits, Enter collapses or
 * restores pane 1 when collapsible. Drag cancellation restores its start size.
 * Storage errors are harmless; only size/collapse state is stored, no content.
 * Both panes stay mounted; a collapsed first pane is hidden and unfocusable. */
export function SplitPane({ direction, initialSize, minSize, maxSize, storageKey, first, second, collapsible = false }: {
  direction: "horizontal" | "vertical"; initialSize: number; minSize: number; maxSize: number;
  storageKey: string; first: ReactNode; second: ReactNode; collapsible?: boolean;
}) {
  const root = useRef<HTMLDivElement>(null);
  const firstId = useId();
  const floor = Number.isFinite(minSize) ? Math.max(0, minSize) : 0;
  const ceiling = Number.isFinite(maxSize) ? Math.max(floor, maxSize) : Math.max(floor, 1200);
  const fallback = clamp(Number.isFinite(initialSize) ? initialSize : floor, floor, ceiling);
  const key = `myflames.split.${storageKey}`;
  const read = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(key) || "null");
      if (saved && typeof saved.size === "number" && Number.isFinite(saved.size))
        return { size: clamp(saved.size, floor, ceiling), collapsed: collapsible && saved.collapsed === true };
    } catch { /* Disabled storage or an older/corrupt preference. */ }
    return { size: fallback, collapsed: false };
  };
  const [state, setState] = useState(read);
  const [extent, setExtent] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ pointer: number; start: number; size: number; before: typeof state } | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  useEffect(() => { setState(read()); }, [key, floor, ceiling, collapsible]);
  useLayoutEffect(() => {
    const el = root.current;
    if (!el) return;
    const measure = () => {
      const size = direction === "horizontal" ? el.clientWidth : el.clientHeight;
      // Hidden places have no dimensions; retain the last usable bound.
      if (size > 0) setExtent(size);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [direction]);
  const upper = Math.min(ceiling, extent === null ? ceiling : Math.max(0, extent - 56));
  const lower = Math.min(floor, upper);
  const size = state.collapsed ? 0 : clamp(state.size, lower, upper);
  const save = (next: typeof state) => {
    stateRef.current = next;
    setState(next);
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* Best effort. */ }
  };
  const toggle = () => save({ size: state.size, collapsed: !state.collapsed });
  const finish = (e: ReactPointerEvent<HTMLDivElement>, cancelled: boolean) => {
    const d = drag.current;
    if (!d || d.pointer !== e.pointerId) return;
    drag.current = null;
    setDragging(false);
    if (cancelled) setState(d.before);
    else save(stateRef.current);
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
  };
  return <div ref={root} className={`split-pane is-${direction}${dragging ? " is-dragging" : ""}${state.collapsed ? " is-collapsed" : ""}`}
    style={{ "--split-size": `${size}px` } as CSSProperties}>
    <div id={firstId} className="split-pane-first" hidden={state.collapsed}>{first}</div>
    <div className="split-pane-divider">
      <div className="split-pane-handle" role="separator" tabIndex={0} aria-label="Resize panes"
        aria-orientation={direction === "horizontal" ? "vertical" : "horizontal"}
        aria-controls={firstId} aria-valuemin={state.collapsed ? 0 : lower} aria-valuemax={upper}
        aria-valuenow={Math.round(size)} aria-valuetext={state.collapsed ? "Collapsed" : `${Math.round(size)} pixels`}
        onDoubleClick={collapsible ? toggle : undefined}
        onKeyDown={(e) => {
          if (e.key === "Enter" && collapsible) { e.preventDefault(); toggle(); return; }
          const decrease = direction === "horizontal" ? "ArrowLeft" : "ArrowUp";
          const increase = direction === "horizontal" ? "ArrowRight" : "ArrowDown";
          if (![decrease, increase, "Home", "End"].includes(e.key)) return;
          e.preventDefault();
          const step = e.shiftKey ? 32 : 8;
          const next = e.key === "Home" ? lower : e.key === "End" ? upper : size + (e.key === decrease ? -step : step);
          save({ size: clamp(next, lower, upper), collapsed: false });
        }}
        onPointerDown={(e) => {
          if (e.button !== 0) return;
          e.preventDefault();
          e.currentTarget.focus();
          drag.current = { pointer: e.pointerId, start: direction === "horizontal" ? e.clientX : e.clientY, size, before: state };
          e.currentTarget.setPointerCapture(e.pointerId);
          setDragging(true);
        }}
        onPointerMove={(e) => {
          const d = drag.current;
          if (!d || d.pointer !== e.pointerId) return;
          const next = { size: clamp(d.size + (direction === "horizontal" ? e.clientX : e.clientY) - d.start, lower, upper), collapsed: false };
          stateRef.current = next;
          setState(next);
        }}
        onPointerUp={(e) => finish(e, false)} onPointerCancel={(e) => finish(e, true)}
        onLostPointerCapture={(e) => finish(e, true)} />
      {collapsible && <IconButton className="split-pane-toggle" icon="split-pane" size={12}
        label={state.collapsed ? "Expand first pane" : "Collapse first pane"} aria-expanded={!state.collapsed}
        aria-controls={firstId} onClick={toggle} />}
    </div>
    <div className="split-pane-second">{second}</div>
  </div>;
}

// ---- Window status --------------------------------------------------------

/** Places publish already-known telemetry, never start a read for chrome.
 * Scope must include connectionKey (and editor tab id when applicable). This
 * prevents a hidden place/old connection from labelling the active place.
 * No provided value means unknown, never an implied successful connection. */
export type WindowStatus = {
  engineVersion?: string;
  connectionState?: string;
  transaction?: string;
  refreshedAt?: number | null;
  refreshing?: boolean;
  sessionState?: string;
  lastRunAt?: number | null;
  durationMs?: number | null;
  rows?: number | null;
};
type WindowStatusStore = { values: Record<string, WindowStatus>; publish: (scope: string, value: WindowStatus | null) => void };
const WindowStatusContext = createContext<WindowStatusStore | null>(null);
export function WindowStatusProvider({ children }: { children: ReactNode }) {
  const [values, setValues] = useState<Record<string, WindowStatus>>({});
  const publish = useCallback((scope: string, value: WindowStatus | null) => {
    setValues((old) => {
      const next = { ...old };
      if (value) next[scope] = value;
      else delete next[scope];
      return next;
    });
  }, []);
  const store = useMemo(() => ({ values, publish }), [values, publish]);
  return <WindowStatusContext.Provider value={store}>{children}</WindowStatusContext.Provider>;
}
/** Call in a place with a stable scope and the latest known values. Cleanup
 * drops its contribution on unmount/scope change. Only one owner per scope.
 * Shell scopes: `server:${connectionKey(connection)}` and
 * `query:${connectionKey(connection)}:${tabId}`. Plans use `plans:${planId}`;
 * compare uses `compare:${beforeId}:${afterId}`. Never publish an old report
 * under the current connection's scope. */
export function useWindowStatus(scope: string, value: WindowStatus) {
  const publish = useContext(WindowStatusContext)?.publish;
  const { engineVersion, connectionState, transaction, refreshedAt, refreshing, sessionState, lastRunAt, durationMs, rows } = value;
  useEffect(() => {
    publish?.(scope, { engineVersion, connectionState, transaction, refreshedAt, refreshing, sessionState, lastRunAt, durationMs, rows });
    return () => publish?.(scope, null);
  }, [publish, scope, engineVersion, connectionState, transaction, refreshedAt, refreshing, sessionState, lastRunAt, durationMs, rows]);
}
/** The window footer. *lead* comes first, left-aligned: the shell puts the
 * write safety of the connection in use there (Connections.tsx SafetyStatus)
 * on the places that send to it. */
export function WindowStatusBar({ scope, fallback = {}, lead }: { scope: string; fallback?: WindowStatus; lead?: ReactNode }) {
  const store = useContext(WindowStatusContext);
  const value = { ...fallback, ...store?.values[scope] };
  const stamp = (at: number | null | undefined) => {
    if (typeof at !== "number" || !Number.isFinite(at)) return null;
    const date = new Date(at);
    return Number.isNaN(date.getTime()) ? null :
      <time dateTime={date.toISOString()} title={date.toLocaleString()}>{date.toLocaleTimeString()}</time>;
  };
  const refreshed = stamp(value.refreshedAt);
  const ran = stamp(value.lastRunAt);
  const knownNumber = (n: number | null | undefined): n is number => typeof n === "number" && Number.isFinite(n) && n >= 0;
  return <footer className="window-statusbar" aria-label="Workspace status" tabIndex={0}>
    {lead}
    {value.engineVersion && <span data-status="engine">{value.engineVersion}</span>}
    {value.connectionState && <span data-status="connection">{value.connectionState}</span>}
    {value.sessionState && <span data-status="session">{value.sessionState}</span>}
    {value.transaction && <span data-status="transaction">{value.transaction}</span>}
    {ran && <span data-status="last-run">Last run {ran}</span>}
    {knownNumber(value.durationMs) && <span data-status="duration">{value.durationMs.toLocaleString()} ms</span>}
    {knownNumber(value.rows) && <span data-status="rows">{value.rows.toLocaleString()} {value.rows === 1 ? "row" : "rows"}</span>}
    {value.refreshing && <span data-status="refreshing" role="status">Refreshing…</span>}
    {refreshed && <span className="window-statusbar-refresh" data-status="refreshed">Refreshed {refreshed}</span>}
  </footer>;
}

/** ViewLayout reports its existing loading state to Server, without new reads
 * or DOM observers. The owner binds the callback to the active connection. */
export const ViewActivityContext = createContext<((view: string, loading: boolean) => void) | null>(null);

// ---- Rows: keyboard movement shared by tables, grouped lists and tiles -----

/** Arrow keys move focus between the [data-row-key] items of the nearest
 * [data-rows] container; the caller decides what selection follows. */
export function moveRowFocus(
  event: KeyboardEvent<HTMLElement>,
  items_selector = "[data-row-key]",
  scope_selector = "[data-rows]",
): HTMLElement | null {
  const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
  const horizontal = (event.currentTarget as HTMLElement).dataset.rowAxis === "both";
  if (horizontal) keys.push("ArrowRight", "ArrowLeft");
  if (!keys.includes(event.key)) return null;
  const scope = (event.currentTarget as HTMLElement).closest(scope_selector);
  if (!scope) return null;
  const items = [...scope.querySelectorAll<HTMLElement>(items_selector)].filter((e) =>
    // Rows inside a closed disclosure are laid out but not visible.
    typeof e.checkVisibility === "function" ? e.checkVisibility() : e.offsetParent !== null,
  );
  const at = items.indexOf(event.currentTarget as HTMLElement);
  if (at < 0) return null;
  const next =
    event.key === "Home"
      ? 0
      : event.key === "End"
        ? items.length - 1
        : event.key === "ArrowDown" || event.key === "ArrowRight"
          ? Math.min(items.length - 1, at + 1)
          : Math.max(0, at - 1);
  event.preventDefault();
  const target = items[next];
  target.focus();
  target.scrollIntoView({ block: "nearest" });
  return target;
}

/** What a view gives its rows: the selected key and how to change it.
 * open = true also opens the inspector (click, Enter). */
export type Selection = { key: string; select: (key: string, open?: boolean) => void };

// ---- Data table ------------------------------------------------------------

export type Column<T> = {
  /** Stable id: stored widths and hidden columns refer to it, never to the label. */
  key: string;
  label: ReactNode;
  /** Plain name for the Columns menu and the resize handle, when the label is not a string. */
  name?: string;
  /** Header tooltip: what the column measures. */
  title?: string;
  num?: boolean;
  /** Sortable when given; numbers start largest first. */
  sort?: (row: T) => string | number | null;
  render: (row: T) => ReactNode;
  className?: string;
  /** The header reads only for screen readers (an actions column). */
  hiddenLabel?: boolean;
  /** Takes the width the other columns leave, truncating its text (one per table). */
  flex?: boolean;
  /** The row's name (by default the first text column): when the frame is
   * too narrow it keeps NAME_FLOOR px of text beside its flags, and the other
   * text columns give up room first. */
  primary?: boolean;
  /** Starting width in px; otherwise measured from the header and the rows. */
  width?: number;
  /** Never narrower than this (a status word that must stay whole). On the
   * flexible column it replaces the 260 px SQL-text minimum. */
  minWidth?: number;
};
export type SortState = { key: string; dir: "asc" | "desc" };

export function sortRows<T>(rows: T[], sort: SortState | null, value: ((row: T) => string | number | null) | undefined) {
  if (!sort || !value) return rows;
  const sign = sort.dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = value(a);
    const y = value(b);
    if (x === y) return 0;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return (x < y ? -1 : 1) * sign;
  });
}

/** One table's column layout as the user left it: widths in px by column
 * key, and the keys of hidden columns. Stored with the workspace. */
export type ColumnPrefs = { widths?: Record<string, number>; hidden?: string[] };
/** Where tables keep their column layout (the Server place keeps it in the
 * workspace session); without one, a table remembers it while mounted. */
export type ColumnStore = {
  get: (table: string) => ColumnPrefs | undefined;
  set: (table: string, prefs: ColumnPrefs | null) => void;
};
export const ColumnStoreContext = createContext<ColumnStore | null>(null);
export const COLUMN_MIN = 48;
export const COLUMN_MAX = 1200;
/** The flexible column never gets narrower than this. */
const FLEX_MIN = 260;
/** Measured text columns start at most this wide (wider content truncates). */
const AUTO_MAX = 360;
/** A text column wider than this may give up room when the frame is narrow. */
const SHRINK_FLOOR = 140;
/** The name column keeps this much (plus its flags) when the frame is narrow,
 * for example when the inspector opens. */
const NAME_FLOOR = 220;
/** The narrow column at the right end that holds the Columns menu. */
const MENU_COLUMN = 36;
/** Cell padding (left + right) and room for the sort mark. */
const CELL_PAD = 26;
const SORT_MARK = 12;
/** Above this many rows only the rows in view (plus a margin) are drawn. */
export const WINDOW_ROWS = 300;
const OVERSCAN = 16;

function columnName<T>(c: Column<T>) {
  return c.name || (typeof c.label === "string" ? c.label : c.key);
}
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

export type TableContextAction =
  | {
      label: string;
      icon?: ReactNode;
      shortcut?: string;
      danger?: boolean;
      disabled?: boolean;
      onClick: () => void;
    }
  | { divider: true };

/** Marks *marker* with data-more-left / data-more-right while *scroller* has
 * content scrolled out of view past that edge, for an edge shadow (overlay
 * scroll bars stay hidden until the pointer scrolls, so a column cut at the
 * edge would otherwise look covered). Attributes are set on the element, so
 * scrolling never re-renders. */
export function useScrollEdges(scroller: RefObject<HTMLElement>, marker: RefObject<HTMLElement>) {
  const mark = useCallback(() => {
    const el = scroller.current;
    const box = marker.current;
    if (!el || !box) return;
    const end = el.scrollWidth - el.clientWidth;
    box.toggleAttribute("data-more-left", end > 1 && el.scrollLeft > 1);
    box.toggleAttribute("data-more-right", end > 1 && el.scrollLeft < end - 1);
  }, [scroller, marker]);
  useLayoutEffect(mark);
  // The scroller can mount later (a table that had no rows): attach to
  // whichever element is current after each render, once.
  const attached = useRef<{ el: HTMLElement; detach: () => void } | null>(null);
  useEffect(() => {
    const el = scroller.current;
    if (attached.current?.el === el) return;
    attached.current?.detach();
    attached.current = null;
    if (!el) return;
    el.addEventListener("scroll", mark, { passive: true });
    const observer = new ResizeObserver(mark);
    observer.observe(el);
    if (el.firstElementChild) observer.observe(el.firstElementChild);
    attached.current = {
      el,
      detach: () => {
        el.removeEventListener("scroll", mark);
        observer.disconnect();
      },
    };
  });
  useEffect(() => () => {
    attached.current?.detach();
    attached.current = null;
  }, []);
}

/** A dense, sortable, selectable table (Activity Monitor): sticky header,
 * right-aligned tabular numbers, row focus with the arrow keys, Enter opens
 * the inspector. Every column can be resized (pointer or keyboard) and
 * hidden from the Columns menu; with *id* inside a ColumnStoreContext the
 * layout is kept with the workspace. Long tables draw only the rows in view.
 * Horizontal scroll stays inside the table frame. */
export function DataTable<T>({
  id,
  label,
  columns,
  rows,
  rowKey,
  selection,
  selectionHighlight = "always",
  defaultSort = null,
  sort: controlledSort,
  onSortChange,
  rowAttrs,
  rowAction,
  rowClass,
  empty,
  className = "",
  detailColumns = [],
  compact = false,
  density = "default",
  multiSelection,
  onActivate,
  rowContextMenu,
  nativeRowMenu = true,
}: {
  /** Stable id of the table ("indexes.unused"), for the stored column layout. */
  id?: string;
  label: string;
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  selection?: Selection;
  /** Active-only fill follows the inspector or table focus; the cursor stays put. */
  selectionHighlight?: "always" | "active";
  defaultSort?: SortState | null;
  /** Controlled sorting lets paginated callers sort the complete collection first. */
  sort?: SortState | null;
  onSortChange?: (sort: SortState) => void;
  rowAttrs?: (row: T) => Record<string, string | undefined>;
  /** Optional visible action in the existing trailing column. */
  rowAction?: (row: T) => ReactNode;
  rowClass?: (row: T) => string;
  empty?: ReactNode;
  className?: string;
  /** Detail columns omitted by a compact preset; explicit choices win. */
  detailColumns?: string[];
  compact?: boolean;
  /** Row height only, independent of the compact column preset: 28 or 32 px. */
  density?: "compact" | "default";
  /** Optional collection selection, independent of the keyboard cursor. */
  multiSelection?: { keys: string[]; select: (keys: string[]) => void };
  /** Collections can select with one click and activate with Enter/double click. */
  onActivate?: (row: T) => void;
  /** Custom context menu actions for a row (shown above standard Excel-style copy actions). */
  rowContextMenu?: (
    row: T,
    helpers: {
      col?: Column<T>;
      cellText?: string;
      copy: (text: string, msg?: string) => Promise<void>;
      close: () => void;
    }
  ) => TableContextAction[];
  /** False when the caller draws its own row menu (the Query editor results):
   * the table then opens no "Row actions" menu on right click, Shift+F10 or
   * the ContextMenu key, so only one menu ever opens. */
  nativeRowMenu?: boolean;
}) {
  const rangeAnchor = useRef<string>("");
  const inspector = useInspector();
  const [focused, setFocused] = useState(false);
  const showSelection = selectionHighlight === "always" || !!inspector?.open || focused;
  const store = useContext(ColumnStoreContext);
  const [localPrefs, setLocalPrefs] = useState<ColumnPrefs | null>(null);
  const persisted = !!(id && store);
  const prefs = (persisted ? store!.get(id!) : localPrefs) || null;
  const savePrefs = (next: ColumnPrefs | null) => {
    const clean = next && (Object.keys(next.widths || {}).length || (next.hidden || []).length) ? next : null;
    if (persisted) store!.set(id!, clean);
    else setLocalPrefs(clean);
  };
  const keys = columns.map((c) => c.key);
  const signature = keys.join("\u0000");
  // The stored layout, checked against the columns this table has now:
  // unknown keys are dropped, and at least one column always stays visible.
  const explicitHidden = new Set((prefs?.hidden || []).filter((k) => keys.includes(k)));
  if (explicitHidden.size >= columns.length) explicitHidden.clear();
  const userWidths: Record<string, number> = {};
  for (const [k, v] of Object.entries(prefs?.widths || {}))
    if (keys.includes(k) && typeof v === "number" && Number.isFinite(v)) userWidths[k] = v;
  const autoHidden = compact ? detailColumns : [];
  const hidden = new Set([...explicitHidden, ...autoHidden.filter((k) => keys.includes(k) && userWidths[k] === undefined)]);
  // A compact preset must never remove every column, even after customization.
  if (hidden.size >= columns.length) {
    const first = keys.find((k) => !explicitHidden.has(k));
    if (first) hidden.delete(first);
  }
  const visible = columns.filter((c) => !hidden.has(c.key));

  // Sorting: the accessor is looked up among all columns (hidden ones too),
  // so hiding, showing or resizing never changes the order.
  const [localSort, setLocalSort] = useState<SortState | null>(defaultSort);
  const sort = controlledSort === undefined ? localSort : controlledSort;
  const latestColumns = useRef(columns);
  latestColumns.current = columns;
  const sorted = useMemo(() => {
    const column = latestColumns.current.find((c) => c.key === sort?.key);
    return sortRows(rows, sort, column?.sort);
  }, [rows, sort, signature]);
  const latestRowKey = useRef(rowKey);
  latestRowKey.current = rowKey;
  const rowKeys = useMemo(() => sorted.map((r) => latestRowKey.current(r)), [sorted]);
  function onSort(c: Column<T>) {
    const next: SortState = sort?.key === c.key ? { key: c.key, dir: sort.dir === "asc" ? "desc" : "asc" } : { key: c.key, dir: c.num ? "desc" : "asc" };
    if (onSortChange) onSortChange(next);
    else setLocalSort(next);
  }

  // Widths: measured from the header and the rows drawn (grow only while the
  // table is mounted, so an automatic refresh does not make columns jump),
  // then the user's widths, clamped to each column's minimum.
  const wrap = useRef<HTMLDivElement>(null);
  const table = useRef<HTMLTableElement>(null);
  const [frame, setFrame] = useState(0);
  const [measured, setMeasured] = useState<{
    sig: string;
    natural: Record<string, number>;
    min: Record<string, number>;
    /** Width of a column's flags (pills) in its widest row. */
    flags: Record<string, number>;
  }>({
    sig: "",
    natural: {},
    min: {},
    flags: {},
  });
  const [contextMenu, setContextMenu] = useState<{
    x: number;
    y: number;
    row: T;
    key: string;
    col?: Column<T>;
    cellText?: string;
    rowCells: string[];
  } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<number | null>(null);

  const showToast = (msg: string) => {
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    setToast(msg);
    toastTimer.current = window.setTimeout(() => setToast(null), 1800);
  };

  const copyWithFeedback = async (text: string, msg?: string) => {
    setContextMenu(null);
    const ok = await copyToClipboard(text);
    if (ok) {
      showToast(msg || "Copied to clipboard");
    } else {
      showToast("Could not copy to clipboard");
    }
  };

  useEffect(() => {
    if (!contextMenu) return;
    const handleOutside = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      if (!target?.closest(".context-menu")) {
        setContextMenu(null);
      }
    };
    const handleKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") setContextMenu(null);
    };
    window.addEventListener("mousedown", handleOutside);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handleOutside);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [contextMenu]);
  const natural = measured.sig === signature ? measured.natural : {};
  const mins = measured.sig === signature ? measured.min : {};
  const flagWidths = measured.sig === signature ? measured.flags : {};
  // A flexible column keeps FLEX_MIN (SQL text) unless it names its own
  // minimum (a connection or table name needs less).
  const minOf = (c: Column<T>) =>
    Math.min(COLUMN_MAX, Math.max(mins[c.key] || COLUMN_MIN, c.flex ? c.minWidth ?? FLEX_MIN : COLUMN_MIN, c.minWidth || 0));
  const baseOf = (c: Column<T>) =>
    clamp(userWidths[c.key] ?? (c.width ?? natural[c.key] ?? (c.flex ? FLEX_MIN : c.num ? 96 : 140)), minOf(c), COLUMN_MAX);
  // A drag shows its width live and is stored when the pointer is released.
  const [dragging, setDragging] = useState<{ key: string; width: number } | null>(null);
  let base = visible.map((c) => (dragging?.key === c.key ? clamp(dragging.width, minOf(c), COLUMN_MAX) : baseOf(c)));
  // Too wide for the frame: only long text columns the user has not sized
  // give up room, and never below SHRINK_FLOOR or their measured minimum
  // (label, flags); past that the table scrolls sideways inside its frame
  // instead of crushing short columns. The name column gives up room last,
  // and keeps NAME_FLOOR px of text beside its flags (or its whole width
  // when that is less).
  const needed = base.reduce((a, b) => a + b, 0) + MENU_COLUMN;
  if (frame > 0 && needed > frame) {
    const primary = visible.findIndex((c) => c.primary);
    const flex = visible.findIndex((c) => c.flex);
    const name = primary >= 0 ? primary : flex >= 0 ? flex : visible.findIndex((c) => !c.num);
    const floorOf = (c: Column<T>, i: number) =>
      i === name ? Math.max(minOf(c), Math.min(base[i], NAME_FLOOR + (flagWidths[c.key] || 0))) : Math.max(minOf(c), SHRINK_FLOOR);
    let over = needed - frame;
    // First, columns that start from a fixed width wider than their widest
    // value and label give the spare room back: nothing truncates, and a
    // table that fits no longer scrolls a column under the Columns menu.
    const slack = visible.map((c, i) =>
      c.width !== undefined && !c.flex && userWidths[c.key] === undefined && dragging?.key !== c.key && natural[c.key] !== undefined
        ? Math.max(0, base[i] - Math.max(natural[c.key], minOf(c)))
        : 0,
    );
    const spare = slack.reduce((a, b) => a + b, 0);
    if (spare > 0) {
      const cut = Math.min(over, spare);
      base = base.map((w, i) => w - (cut * slack[i]) / spare);
      over -= cut;
    }
    for (const nameNow of [false, true]) {
      const room = visible.map((c, i) =>
        !c.num && userWidths[c.key] === undefined && dragging?.key !== c.key && (i === name) === nameNow
          ? Math.max(0, base[i] - floorOf(c, i))
          : 0,
      );
      const total = room.reduce((a, b) => a + b, 0);
      const cut = Math.min(over, total);
      if (total > 0 && cut > 0) {
        base = base.map((w, i) => w - (cut * room[i]) / total);
        over -= cut;
      }
    }
  }
  // Whole pixels, so the columns add up to the frame exactly (a fractional
  // total scrolled the table by a pixel).
  base = base.map((w) => Math.floor(w));
  // One column takes the room the others leave: the flexible one, else the
  // last text column, else the last one. The Columns menu has its own narrow
  // column at the right end, which stays in view.
  let fill = visible.findIndex((c) => c.flex);
  if (fill < 0) fill = visible.map((c) => !c.num).lastIndexOf(true);
  if (fill < 0) fill = visible.length - 1;
  const others = base.reduce((sum, w, i) => (i === fill ? sum : sum + w), 0) + MENU_COLUMN;
  const widths = base.map((w, i) => (i === fill && frame > 0 ? Math.max(w, frame - others) : w));
  const tableWidth = widths.reduce((a, b) => a + b, 0) + MENU_COLUMN;

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    // Full-width details keep this list mounted but hidden. Preserve its last
    // usable measurements until it is visible again.
    const update = () => { if (el.clientWidth > 0) setFrame(el.clientWidth); };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [rows.length > 0]);

  // Columns scrolled out of view sideways: the frame marks each edge with
  // more columns beyond it, so the column under the Columns menu reads as
  // scrolled (a shadow on that edge) rather than covered.
  const frameEl = useRef<HTMLDivElement>(null);
  useScrollEdges(wrap, frameEl);

  // Long tables: draw the rows in view only, with spacers above and below.
  const windowed = sorted.length > WINDOW_ROWS;
  const [view, setView] = useState({ top: 0, height: 760 });
  const [rowHeight, setRowHeight] = useState(density === "compact" ? 28 : 32);
  const [headHeight, setHeadHeight] = useState(32);
  const first = windowed ? Math.max(0, Math.floor((view.top - headHeight) / rowHeight) - OVERSCAN) : 0;
  const last = windowed ? Math.min(sorted.length, Math.ceil((view.top + view.height) / rowHeight) + OVERSCAN) : sorted.length;
  const shown = windowed ? sorted.slice(first, last) : sorted;

  // Measure once the rows change: each column's max-content width (table
  // laid out automatically for a moment, before paint) and its header label.
  const hiddenSig = [...hidden].join("\u0000");
  useLayoutEffect(() => {
    const t = table.current;
    const el = wrap.current;
    if (!t || !el || !t.tHead || el.clientWidth === 0) return;
    const left = el.scrollLeft;
    t.classList.add("is-measuring");
    const next: Record<string, number> = measured.sig === signature ? { ...measured.natural } : {};
    const nextMin: Record<string, number> = measured.sig === signature ? { ...measured.min } : {};
    const nextFlags: Record<string, number> = measured.sig === signature ? { ...measured.flags } : {};
    let changed = measured.sig !== signature;
    for (const th of [...t.tHead.rows[0].cells]) {
      const key = th.dataset.col;
      const c = key ? columns.find((x) => x.key === key) : undefined;
      if (!c || !key) continue;
      const head = th.querySelector(".th-label");
      // The table is laid out at max-content here, so the label's box is its full width.
      const labelWidth = head && !c.hiddenLabel ? head.getBoundingClientRect().width : 0;
      let min = Math.max(COLUMN_MIN, Math.ceil(labelWidth) + CELL_PAD + (c.sort ? SORT_MARK : 0));
      const full = Math.ceil(th.getBoundingClientRect().width);
      // Numbers never truncate: their widest value is part of the minimum.
      if (c.num) min = Math.max(min, full);
      // Neither do flags: a cell's pills keep their size (a name beside them truncates).
      const cells = t.tBodies[0].querySelectorAll<HTMLElement>(`:scope > tr[data-row-key] > td[data-col="${CSS.escape(key)}"]`);
      for (let i = 0; i < Math.min(cells.length, 120); i++) {
        const pills = cells[i].querySelectorAll(".pill, .status-pill");
        if (!pills.length) continue;
        let flags = 0;
        pills.forEach((p) => (flags += p.getBoundingClientRect().width + 8));
        // A name beside the flags keeps a few characters.
        const pillWidth = CELL_PAD + (cells[i].querySelector(".cell-name > :not(.pill, .status-pill)") ? 64 : 0) + flags;
        min = Math.max(min, Math.ceil(pillWidth));
        if ((nextFlags[key] || 0) < Math.ceil(flags) - 1) {
          nextFlags[key] = Math.ceil(flags);
          changed = true;
        }
      }
      min = Math.min(min, COLUMN_MAX);
      const width = c.flex ? FLEX_MIN : clamp(full, min, c.num ? COLUMN_MAX : Math.max(min, AUTO_MAX));
      if ((nextMin[key] || 0) < min) {
        nextMin[key] = min;
        changed = true;
      }
      if ((next[key] || 0) < width - 1) {
        next[key] = width;
        changed = true;
      }
    }
    t.classList.remove("is-measuring");
    el.scrollLeft = left;
    if (changed) setMeasured({ sig: signature, natural: next, min: nextMin, flags: nextFlags });
  }, [sorted, signature, hiddenSig]);
  // Row and header heights, for the spacers of a long table.
  useLayoutEffect(() => {
    const t = table.current;
    if (!t || !t.tHead) return;
    const sample = [...t.tBodies[0].rows].find((r) => r.dataset.rowKey !== undefined);
    if (sample && sample.offsetHeight > 0 && Math.abs(sample.offsetHeight - rowHeight) > 0.5) setRowHeight(sample.offsetHeight);
    const head = t.tHead.offsetHeight;
    if (head && Math.abs(head - headHeight) > 0.5) setHeadHeight(head);
  });

  // Keyboard: the next row may not be drawn yet in a long table; scroll it
  // into the window, then focus it once it is.
  const pendingFocus = useRef("");
  useLayoutEffect(() => {
    const key = pendingFocus.current;
    if (!key || !table.current) return;
    const row = table.current.querySelector<HTMLElement>(`tbody tr[data-row-key="${CSS.escape(key)}"]`);
    if (!row) return;
    pendingFocus.current = "";
    row.focus({ preventScroll: true });
    row.scrollIntoView({ block: "nearest" });
  });
  function selectRange(key: string, extend: boolean, toggle: boolean) {
    if (!multiSelection) return;
    if (extend) {
      const start = rowKeys.indexOf(rangeAnchor.current || selection?.key || key);
      const end = rowKeys.indexOf(key);
      const range = rowKeys.slice(Math.min(start < 0 ? end : start, end), Math.max(start, end) + 1);
      multiSelection.select(toggle ? [...new Set([...multiSelection.keys, ...range])] : range);
    } else {
      rangeAnchor.current = key;
      multiSelection.select(toggle ? (multiSelection.keys.includes(key)
        ? multiSelection.keys.filter(k => k !== key) : [...multiSelection.keys, key]) : [key]);
    }
  }
  function goTo(index: number, extend = false, keep = false) {
    const key = rowKeys[index];
    if (key === undefined || !selection) return;
    const el = wrap.current;
    if (windowed && el) {
      const top = headHeight + index * rowHeight;
      if (top < el.scrollTop + headHeight) el.scrollTop = top - headHeight;
      else if (top + rowHeight > el.scrollTop + el.clientHeight) el.scrollTop = top + rowHeight - el.clientHeight;
      setView({ top: el.scrollTop, height: el.clientHeight });
    }
    pendingFocus.current = key;
    const row = table.current?.querySelector<HTMLElement>(`tbody tr[data-row-key="${CSS.escape(key)}"]`);
    if (row) {
      pendingFocus.current = "";
      row.focus({ preventScroll: true });
      row.scrollIntoView({ block: "nearest" });
    }
    selection.select(key, false);
    if (!keep) selectRange(key, extend, false);
  }
  function onRowKey(e: KeyboardEvent<HTMLTableRowElement>, key: string) {
    if (!selection || e.target !== e.currentTarget) return;
    if (nativeRowMenu && (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10"))) {
      e.preventDefault();
      const rect = e.currentTarget.getBoundingClientRect();
      const tr = e.currentTarget;
      const row = sorted[rowKeys.indexOf(key)];
      if (!row) return;
      const rowCells = visible.map((c) => {
        const cellTd = tr.querySelector(`td[data-col="${CSS.escape(c.key)}"]`);
        if (cellTd) return (cellTd.textContent || "").trim();
        return c.sort && typeof c.sort === "function" ? String(c.sort(row) ?? "") : "";
      });
      setContextMenu({
        x: Math.min(rect.left + 40, window.innerWidth - 260),
        y: Math.min(rect.bottom + 4, window.innerHeight - 340),
        row,
        key,
        col: visible[0],
        cellText: rowCells[0],
        rowCells,
      });
      return;
    }
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (e.key === " " && multiSelection) selectRange(key, e.shiftKey, true);
      else if (onActivate) {
        const row = sorted[rowKeys.indexOf(key)];
        if (row) onActivate(row);
      } else selection.select(key, true);
      return;
    }
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      if (wrap.current) wrap.current.scrollLeft += e.key === "ArrowRight" ? 48 : -48;
      return;
    }
    const at = rowKeys.indexOf(key);
    const page = Math.max(1, Math.floor((wrap.current?.clientHeight || 400) / rowHeight) - 1);
    const next =
      e.key === "ArrowDown"
        ? at + 1
        : e.key === "ArrowUp"
          ? at - 1
          : e.key === "Home"
            ? 0
            : e.key === "End"
              ? rowKeys.length - 1
              : e.key === "PageDown"
                ? at + page
                : e.key === "PageUp"
                  ? at - page
                  : null;
    if (next === null || at < 0) return;
    e.preventDefault();
    goTo(clamp(next, 0, rowKeys.length - 1), e.shiftKey, e.metaKey || e.ctrlKey);
  }

  // Resizing: pointer drag on a header's right edge, or the keyboard on it.
  const drag = useRef<{ key: string; x: number; width: number } | null>(null);
  // Saving keeps what is stored for columns this table does not show right
  // now (Waits has columns per time range); they are ignored until they return.
  const elsewhere = {
    widths: Object.fromEntries(Object.entries(prefs?.widths || {}).filter(([k]) => !keys.includes(k))),
    hidden: (prefs?.hidden || []).filter((k) => !keys.includes(k)),
  };
  function commitWidth(c: Column<T>, width: number) {
    const value = Math.round(clamp(width, minOf(c), COLUMN_MAX));
    savePrefs({ widths: { ...elsewhere.widths, ...userWidths, [c.key]: value }, hidden: [...elsewhere.hidden, ...explicitHidden] });
  }
  function autoWidth(c: Column<T>) {
    const rest = { ...elsewhere.widths, ...userWidths };
    // Fitting an explicitly shown detail column must not hide it again.
    if (detailColumns.includes(c.key)) rest[c.key] = Math.round(clamp(natural[c.key] ?? c.width ?? 140, minOf(c), COLUMN_MAX));
    else delete rest[c.key];
    savePrefs({ widths: rest, hidden: [...elsewhere.hidden, ...explicitHidden] });
  }
  function toggleColumn(key: string) {
    const next = new Set(explicitHidden);
    const sized = { ...elsewhere.widths, ...userWidths };
    if (hidden.has(key)) {
      next.delete(key);
      // Showing a compacted column pins its current width, using the existing
      // persisted width contract so the choice survives inspector toggles and restore.
      const c = columns.find((column) => column.key === key)!;
      if (detailColumns.includes(key)) sized[key] = Math.round(baseOf(c));
    }
    else if (visible.length > 1) next.add(key);
    else return; // The last visible column stays.
    savePrefs({ widths: sized, hidden: [...elsewhere.hidden, ...keys.filter((k) => next.has(k))] });
  }
  const customized = Object.keys(userWidths).length > 0 || explicitHidden.size > 0;

  // A truncated cell shows its full text on hover.
  function onHover(e: ReactPointerEvent<HTMLTableSectionElement>) {
    const td = (e.target as HTMLElement).closest?.("td");
    if (!td || (td.hasAttribute("title") && !td.dataset.autoTitle)) return;
    const clipped =
      td.scrollWidth > td.clientWidth + 1 ||
      [...td.querySelectorAll<HTMLElement>(".cell-truncate, .cell-name > *")].some((x) => x.scrollWidth > x.clientWidth + 1);
    if (clipped) {
      td.title = (td.textContent || "").trim();
      td.dataset.autoTitle = "true";
    } else if (td.dataset.autoTitle) {
      td.removeAttribute("title");
      delete td.dataset.autoTitle;
    }
  }

  if (!rows.length && empty) return <div className="table-empty">{empty}</div>;
  const focusKey = selection && rowKeys.includes(selection.key) ? selection.key : rowKeys[0];
  const focusShown = rowKeys.slice(first, first + shown.length).includes(focusKey);
  const cellClass = (c: Column<T>) => [c.num ? "num" : "", c.flex ? "cell-flex" : "", c.className || ""].join(" ").trim() || undefined;
  return (
    <div ref={frameEl} className={`data-table-frame density-${density} ${className}`.trim()} data-density={density}>
      <div
        ref={wrap}
        className="data-table-wrap"
        role="region"
        aria-label={label}
        tabIndex={selection ? undefined : 0}
        data-rows
        onFocusCapture={() => setFocused(true)}
        onBlurCapture={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocused(false);
        }}
        onScroll={windowed ? (e) => {
          if (e.currentTarget.clientHeight > 0)
            setView({ top: e.currentTarget.scrollTop, height: e.currentTarget.clientHeight });
        } : undefined}
      >
        <table
          ref={table}
          className="data-table is-sized"
          aria-label={label}
          role={multiSelection ? "grid" : undefined}
          aria-multiselectable={multiSelection ? true : undefined}
          aria-rowcount={windowed ? sorted.length + 1 : undefined}
          style={{ width: tableWidth }}
        >
          <colgroup>
            {visible.map((c, i) => (
              <col key={c.key} data-col={c.key} style={{ width: widths[i] }} />
            ))}
            <col className="col-menu" style={{ width: MENU_COLUMN }} />
          </colgroup>
          <thead>
            <tr aria-rowindex={windowed ? 1 : undefined}>
              {visible.map((c, i) => {
                const active = sort?.key === c.key;
                const name = columnName(c);
                return (
                  <th
                    key={c.key}
                    scope="col"
                    data-col={c.key}
                    className={cellClass(c)}
                    title={c.title}
                    aria-sort={active ? (sort!.dir === "asc" ? "ascending" : "descending") : c.sort ? "none" : undefined}
                  >
                    {c.hiddenLabel ? (
                      <span className="visually-hidden th-label">{c.label}</span>
                    ) : c.sort ? (
                      <button type="button" className="sort-button" onClick={() => onSort(c)}>
                        <span className="th-label">{c.label}</span>
                        <span className="sort-mark" aria-hidden="true">
                          {active && <span className={sort!.dir === "asc" ? "sort-up" : "sort-down"}><Icon name="disclosure" size={12} /></span>}
                        </span>
                      </button>
                    ) : (
                      <span className="th-label">{c.label}</span>
                    )}
                    <span
                      className="col-resize"
                      role="separator"
                      aria-orientation="vertical"
                      aria-label={`Width of ${name}`}
                      aria-valuemin={minOf(c)}
                      aria-valuemax={COLUMN_MAX}
                      aria-valuenow={Math.round(widths[i])}
                      title={`Drag to resize ${name}; double-click to fit`}
                      tabIndex={0}
                      onPointerDown={(e) => {
                        e.preventDefault();
                        drag.current = { key: c.key, x: e.clientX, width: widths[i] };
                        (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
                        setDragging({ key: c.key, width: widths[i] });
                      }}
                      onPointerMove={(e) => {
                        const d = drag.current;
                        if (d && d.key === c.key) setDragging({ key: c.key, width: d.width + e.clientX - d.x });
                      }}
                      onPointerUp={(e) => {
                        const d = drag.current;
                        drag.current = null;
                        setDragging(null);
                        if (d && d.key === c.key && e.clientX !== d.x) commitWidth(c, d.width + e.clientX - d.x);
                      }}
                      onPointerCancel={() => {
                        drag.current = null;
                        setDragging(null);
                      }}
                      onDoubleClick={() => autoWidth(c)}
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => {
                        const step = e.shiftKey ? 64 : 16;
                        const now = widths[i];
                        if (e.key === "ArrowRight") commitWidth(c, now + step);
                        else if (e.key === "ArrowLeft") commitWidth(c, now - step);
                        else if (e.key === "Home") commitWidth(c, minOf(c));
                        else if (e.key === "End") commitWidth(c, COLUMN_MAX);
                        else return;
                        e.preventDefault();
                        e.stopPropagation();
                      }}
                    />
                  </th>
                );
              })}
              <th className="col-menu" scope="col">
                <Popover
                  label={`Columns of ${label}`}
                  className="table-columns"
                  buttonClassName="icon-button table-columns-button"
                  buttonTitle="Columns"
                  trigger={
                    <>
                      <Icon name="columns" size={15} />
                      <span className="visually-hidden">Columns</span>
                    </>
                  }
                >
                  <h3 className="popover-title">Columns</h3>
                  <ul className="columns-list">
                    {columns.map((c) => {
                      const shownNow = !hidden.has(c.key);
                      const lastOne = shownNow && visible.length === 1;
                      return (
                        <li key={c.key}>
                          <label className="checkbox" title={lastOne ? "At least one column stays visible" : undefined}>
                            <input
                              type="checkbox"
                              data-col={c.key}
                              checked={shownNow}
                              disabled={lastOne}
                              onChange={() => toggleColumn(c.key)}
                            />
                            <span>{columnName(c)}</span>
                          </label>
                        </li>
                      );
                    })}
                  </ul>
                  <p className="hint columns-hint">Drag a header edge to resize; double-click it to fit.</p>
                  <button type="button" className="button small columns-reset" disabled={!customized} onClick={() => savePrefs(null)}>
                    Reset columns
                  </button>
                </Popover>
              </th>
            </tr>
          </thead>
          <tbody onPointerOver={onHover}>
            {windowed && first > 0 && (
              <tr className="row-spacer" aria-hidden="true">
                <td colSpan={visible.length + 1} style={{ height: first * rowHeight }} />
              </tr>
            )}
            {shown.map((row, i) => {
              const key = rowKeys[first + i];
              const selected = multiSelection ? multiSelection.keys.includes(key) : !!selection && selection.key === key;
              const focusable = key === focusKey || (!focusShown && i === 0);
              return (
                <tr
                  key={key}
                  data-row-key={key}
                  aria-rowindex={windowed ? first + i + 2 : undefined}
                  className={[selected && showSelection ? "is-selected" : "", rowClass?.(row) || ""].join(" ").trim() || undefined}
                  aria-current={!multiSelection && selection && selected ? "true" : undefined}
                  aria-selected={multiSelection ? selected : undefined}
                  tabIndex={selection ? (focusable ? 0 : -1) : undefined}
                  onClick={selection ? (e) => {
                    if ((multiSelection || onActivate) && (e.target as HTMLElement).closest("button, label, input, select, a, [role=menu]")) return;
                    selectRange(key, e.shiftKey, e.metaKey || e.ctrlKey);
                    selection.select(key, !onActivate);
                    e.currentTarget.focus({ preventScroll: true });
                  } : undefined}
                  onDoubleClick={onActivate ? (e) => {
                    if (!(e.target as HTMLElement).closest("button, label, input, select, a, [role=menu]")) onActivate(row);
                  } : undefined}
                  onKeyDown={selection ? (e) => onRowKey(e, key) : undefined}
                  onContextMenu={nativeRowMenu ? (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    const td = (e.target as HTMLElement).closest("td[data-col]");
                    const colKey = td?.getAttribute("data-col") || undefined;
                    const col = visible.find((c) => c.key === colKey);
                    const cellText = td ? (td.textContent || "").trim() : undefined;

                    const tr = e.currentTarget;
                    const rowCells = visible.map((c) => {
                      const cellTd = tr.querySelector(`td[data-col="${CSS.escape(c.key)}"]`);
                      if (cellTd) return (cellTd.textContent || "").trim();
                      return c.sort && typeof c.sort === "function" ? String(c.sort(row) ?? "") : "";
                    });

                    if (selection) {
                      selectRange(key, e.shiftKey, e.metaKey || e.ctrlKey);
                      selection.select(key, false);
                    }

                    const menuW = 260;
                    const menuH = 340;
                    const x = Math.max(8, Math.min(e.clientX, window.innerWidth - menuW - 16));
                    const y = Math.max(8, Math.min(e.clientY, window.innerHeight - menuH - 16));

                    setContextMenu({
                      x,
                      y,
                      row,
                      key,
                      col,
                      cellText,
                      rowCells,
                    });
                  } : undefined}
                  {...(rowAttrs ? rowAttrs(row) : {})}
                >
                  {visible.map((c) => (
                    <td key={c.key} data-col={c.key} className={cellClass(c)}>
                      {c.render(row)}
                    </td>
                  ))}
                  <td className="col-menu" aria-hidden={rowAction ? undefined : true}>{rowAction?.(row)}</td>
                </tr>
              );
            })}
            {windowed && last < sorted.length && (
              <tr className="row-spacer" aria-hidden="true">
                <td colSpan={visible.length + 1} style={{ height: (sorted.length - last) * rowHeight }} />
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {contextMenu && (
        <div
          className="context-menu"
          role="menu"
          aria-label="Row actions"
          style={{ left: contextMenu.x, top: contextMenu.y }}
          tabIndex={-1}
        >
          {(() => {
            const customActions = rowContextMenu
              ? rowContextMenu(contextMenu.row, {
                  col: contextMenu.col,
                  cellText: contextMenu.cellText,
                  copy: copyWithFeedback,
                  close: () => setContextMenu(null),
                })
              : [];

            if (customActions.length === 0) {
              return (
                contextMenu.cellText ? (
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => void copyWithFeedback(contextMenu.cellText!, "Cell value copied")}
                  >
                    <span className="context-menu-icon"><Icon name="copy" /></span>
                    <span>Copy "{contextMenu.cellText.length > 24 ? contextMenu.cellText.slice(0, 24) + "…" : contextMenu.cellText}"</span>
                  </button>
                ) : null
              );
            }

            return (
              <>
                {customActions.map((action, idx) => {
                  if ("divider" in action && action.divider) {
                    return <div key={`div-${idx}`} className="context-menu-divider" />;
                  }
                  const a = action as {
                    label: string;
                    icon?: ReactNode;
                    shortcut?: string;
                    danger?: boolean;
                    disabled?: boolean;
                    onClick: () => void;
                  };
                  return (
                    <button
                      key={a.label}
                      type="button"
                      role="menuitem"
                      className={a.danger ? "is-danger" : ""}
                      disabled={a.disabled}
                      onClick={() => {
                        setContextMenu(null);
                        a.onClick();
                      }}
                    >
                      {a.icon && <span className="context-menu-icon">{typeof a.icon === "string" && isIconName(a.icon) ? <Icon name={a.icon} /> : a.icon}</span>}
                      <span>{a.label}</span>
                      {a.shortcut && <span className="context-menu-shortcut">{a.shortcut}</span>}
                    </button>
                  );
                })}
              </>
            );
          })()}
        </div>
      )}
      {toast && <div className="table-copy-toast" role="status">{toast}</div>}
    </div>
  );
}

// ---- Grouped list (System Settings) -----------------------------------------

export function GroupedList({
  id,
  title,
  count,
  info,
  aside,
  footer,
  children,
  className = "",
}: {
  id: string;
  title: ReactNode;
  count?: number;
  /** ⓘ content for the group title. */
  info?: ReactNode;
  /** Right side of the group header (a one-line summary or a control). */
  aside?: ReactNode;
  /** One secondary line under the group, only when needed. */
  footer?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`grouped ${className}`} aria-labelledby={id}>
      <header className="grouped-head">
        <h3 id={id}>
          {title}
          {count !== undefined && <span className="count">{count.toLocaleString()}</span>}
        </h3>
        {info && <InfoPopover label={`About ${typeof title === "string" ? title : "this group"}`}>{info}</InfoPopover>}
        {aside && <span className="grouped-aside">{aside}</span>}
      </header>
      <ul className="grouped-rows">
        {children}
      </ul>
      {footer && <p className="grouped-footer">{footer}</p>}
    </section>
  );
}

/** One line: status, label, secondary text, value on the right, chevron. */
export function GroupedRow({
  rowKey,
  selection,
  tone,
  toneLabel,
  label,
  detail,
  value,
  secondary,
  attrs,
  depth = 0,
  wrap = false,
  className = "",
}: {
  rowKey: string;
  selection?: Selection;
  tone?: Tone;
  toneLabel?: string;
  label: ReactNode;
  /** Secondary text after the label (one line, muted). */
  detail?: ReactNode;
  value?: ReactNode;
  /** Muted text under the value (a default, a unit). */
  secondary?: ReactNode;
  attrs?: Record<string, string | undefined>;
  depth?: number;
  /** In a narrow view, the value moves under the label instead of cutting it. */
  wrap?: boolean;
  className?: string;
}) {
  const selected = !!selection && selection.key === rowKey;
  const body = (
    <>
      {tone && <StatusPill tone={tone} label={toneLabel} quiet />}
      <span className="row-label">
        <span className="row-title">{label}</span>
        {detail && <span className="row-detail">{detail}</span>}
      </span>
      {(value !== undefined || secondary) && (
        <span className="row-value">
          {value}
          {secondary && <span className="row-secondary">{secondary}</span>}
        </span>
      )}
      {selection && <Icon name="disclosure" size={14} />}
    </>
  );
  return (
    <li className={`grouped-item${depth ? ` depth-${Math.min(depth, 3)}` : ""} ${className}`} {...(attrs || {})}>
      {selection ? (
        <button
          type="button"
          className={`grouped-row${selected ? " is-selected" : ""}${wrap ? " is-wrapping" : ""}`}
          data-row-key={rowKey}
          aria-current={selected ? "true" : undefined}
          onClick={() => selection.select(rowKey, true)}
          onKeyDown={(e) => {
            // Up and Down move through every grouped row of the view, across groups.
            const next = moveRowFocus(e, "button.grouped-row[data-row-key]", ".view-main, [data-rows]");
            if (next?.dataset.rowKey) selection.select(next.dataset.rowKey, false);
          }}
        >
          {body}
        </button>
      ) : (
        <div className={`grouped-row is-static${wrap ? " is-wrapping" : ""}`}>{body}</div>
      )}
    </li>
  );
}

// ---- Metric tiles ------------------------------------------------------------

export function MetricTile({
  rowKey,
  selection,
  label,
  value,
  unit,
  tone,
  toneLabel,
  trend,
  attrs,
}: {
  rowKey: string;
  selection?: Selection;
  label: string;
  value: string;
  unit?: string;
  tone?: Tone;
  toneLabel?: string;
  trend?: ReactNode;
  attrs?: Record<string, string | undefined>;
}) {
  const selected = !!selection && selection.key === rowKey;
  const content = (
    <>
      <span className="tile-label">{label}</span>
      <span className="tile-value">
        {value}
        {unit && <span className="tile-unit">{unit === "%" || unit.startsWith("/") ? unit : ` ${unit}`}</span>}
      </span>
      <span className="tile-foot">
        {tone && <StatusPill tone={tone} label={toneLabel} quiet />}
        {trend}
      </span>
    </>
  );
  return (
    <li className={`tile-item${tone ? ` is-${tone}` : ""}`} {...(attrs || {})}>
      {selection ? (
        <button
          type="button"
          className={`metric-tile${selected ? " is-selected" : ""}`}
          data-row-key={rowKey}
          data-row-axis="both"
          aria-current={selected ? "true" : undefined}
          onClick={() => selection.select(rowKey, true)}
          onKeyDown={(e) => {
            const next = moveRowFocus(e, "button.metric-tile[data-row-key]", ".view-main, [data-rows]");
            if (next?.dataset.rowKey) selection.select(next.dataset.rowKey, false);
          }}
        >
          {content}
        </button>
      ) : (
        <div className="metric-tile is-static">{content}</div>
      )}
    </li>
  );
}

/** Equal-width tiles: every tile in a grid has the same width. The frame is a
 * size container, so kit.css picks the column count from the tile count and
 * the room the grid has: up to six in one row, seven or eight as rows of
 * four, and balanced rows (three, then two) when a row does not fit. */
export function TileGrid({ label, children }: { label: string; children: ReactNode }) {
  const tiles = Children.toArray(children).length;
  const columns = tiles <= 6 ? Math.max(1, tiles) : tiles <= 8 ? 4 : 6;
  return (
    <div className="tile-grid-frame">
      <ul className="tile-grid" aria-label={label} data-tiles={tiles > 8 ? "many" : tiles}
        style={{ "--tiles": columns } as CSSProperties}>
        {children}
      </ul>
    </div>
  );
}

// ---- Sparkline ----------------------------------------------------------------

export type TrendPoint = { at: string; value: number };
/** A small single-series trend: muted line, the latest point in the accent,
 * each sample's value on hover, and a text summary for screen readers. */
export function Sparkline({
  points,
  label,
  format,
  width = 96,
  height = 22,
  className = "",
}: {
  points: TrendPoint[];
  label: string;
  format: (value: number) => string;
  width?: number;
  height?: number;
  className?: string;
}) {
  if (points.length < 2) return null;
  const pad = 3;
  const values = points.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = (width - 2 * pad) / (points.length - 1);
  const x = (i: number) => pad + i * step;
  const y = (v: number) => height - pad - ((v - min) / span) * (height - 2 * pad);
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(p.value).toFixed(1)}`).join(" ");
  const last = points[points.length - 1];
  const summary = `${label}, last ${points.length} samples: low ${format(min)}, high ${format(max)}, latest ${format(last.value)}`;
  return (
    <svg className={`sparkline ${className}`} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={summary}>
      <title>{summary}</title>
      <path d={path} className="sparkline-line" vectorEffect="non-scaling-stroke" />
      {points.map((p, i) => (
        <rect key={i} className="sparkline-hit" x={x(i) - step / 2} y={0} width={step} height={height}>
          <title>{`${new Date(p.at).toLocaleTimeString()}: ${format(p.value)}`}</title>
        </rect>
      ))}
      <circle cx={x(points.length - 1)} cy={y(last.value)} r={2.5} className="sparkline-last" />
    </svg>
  );
}

// ---- Gates, empty states, summary ------------------------------------------------

/** A view that cannot run: one sentence and one action (Workbench). */
export function PrerequisiteGate({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="gate" role="note">
      <Icon name="warning" size={18} />
      <p>{children}</p>
      {action && <span className="gate-action">{action}</span>}
    </div>
  );
}

/** Nothing loaded yet: a title, one line, and the one action. */
export function EmptyState({
  icon,
  title,
  children,
  action,
}: {
  icon: IconName;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="view-empty">
      <Icon name={icon} size={22} />
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

/** One line per view (≤ 120 characters), with the notes button at the end.
 * On desktop it stays on one line: a longer text truncates and shows in full
 * on hover. */
export function SummaryLine({ tone, toneLabel, children, notes, extra }: {
  tone?: Tone;
  toneLabel?: string;
  children: ReactNode;
  notes?: ReactNode[];
  extra?: ReactNode;
}) {
  const text = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const el = text.current;
    if (!el) return;
    if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1) el.title = (el.textContent || "").trim();
    else el.removeAttribute("title");
  });
  return (
    <div className="summary-line">
      <p>
        {tone && <StatusPill tone={tone} label={toneLabel} />}
        <span className="summary-text" ref={text}>
          {children}
        </span>
      </p>
      {extra}
      {notes && <NotesButton notes={notes} />}
    </div>
  );
}

/** Facts on one line: "Server MySQL 8.4 · Uptime 2 d". */
export function FactStrip({ label, facts }: { label: string; facts: [string, ReactNode][] }) {
  return (
    <dl className="fact-strip" aria-label={label}>
      {facts.map(([dt, dd]) => (
        <div key={dt}>
          <dt>{dt}</dt>
          <dd>{dd}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Advanced or rarely used detail, collapsed by default. */
export function Disclosure({
  title,
  count,
  open,
  onToggle,
  children,
  className = "",
  id,
}: {
  title: ReactNode;
  count?: number;
  open?: boolean;
  onToggle?: (open: boolean) => void;
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <details
      id={id}
      className={`disclosure ${className}`}
      open={open}
      onToggle={onToggle ? (e) => onToggle((e.currentTarget as HTMLDetailsElement).open) : undefined}
    >
      <summary>
        <Icon name="disclosure" size={14} />
        {title}
        {count !== undefined && <span className="count">{count.toLocaleString()}</span>}
      </summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}

// ---- Confirmation sheet ---------------------------------------------------------

/** A modal confirmation for a consequential action: what happens, Cancel
 * focused first, Escape cancels. */
export function ConfirmSheet({
  eyebrow,
  title,
  description,
  busy,
  confirmLabel,
  confirmTone = "danger",
  confirmDisabled = false,
  onCancel,
  onConfirm,
  secondary,
  children,
  className = "",
}: {
  eyebrow?: string;
  title: ReactNode;
  description?: ReactNode;
  busy?: boolean;
  confirmLabel: ReactNode;
  confirmTone?: "danger" | "primary";
  /** Keeps the confirm button off until the request is ready (a form inside the sheet). */
  confirmDisabled?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  /** A second way forward, between Cancel and the confirm button (Roll back and close). */
  secondary?: { label: ReactNode; onClick: () => void };
  children?: ReactNode;
  className?: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descId = useId();
  useFocusScope(dialog, { open: true, onClose: () => { if (!busy) onCancel(); }, initialFocus: () => cancel.current });
  // Retained list views can be hidden while their selected detail is open.
  // Keep consequential reviews outside those layout ancestors so the native
  // dialog remains visible and Cancel can receive focus.
  return createPortal(
    <dialog
      ref={dialog}
      className={`confirm-sheet ${className}`}
      role={confirmTone === "danger" ? "alertdialog" : "dialog"}
      tabIndex={-1}
      aria-labelledby={titleId}
      aria-describedby={description ? descId : undefined}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <div className="dialog-heading">
        <div>
          {eyebrow && <div className="eyebrow">{eyebrow}</div>}
          <h2 id={titleId}>{title}</h2>
        </div>
      </div>
      {description && (
        <p id={descId} className="sheet-lead">
          {description}
        </p>
      )}
      {children}
      <div className="dialog-actions sheet-actions">
        <button ref={cancel} type="button" className="button" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
        {secondary && (
          <button type="button" className="button" disabled={busy} onClick={secondary.onClick}>
            {secondary.label}
          </button>
        )}
        <button
          type="button"
          className={`button ${confirmTone === "danger" ? "danger-solid" : "primary"}`}
          disabled={busy || confirmDisabled}
          onClick={onConfirm}
        >
          {confirmLabel}
        </button>
      </div>
    </dialog>, document.body
  );
}

// ---- Inspector -------------------------------------------------------------------

export const INSPECTOR_MIN = 280;
export const INSPECTOR_MAX = 560;
export const INSPECTOR_DEFAULT = 340;
const WIDTH_KEY = "myflames.inspector.width";

/** The inspector's width, remembered in this browser (best effort). */
export function storedInspectorWidth() {
  try {
    const value = Number(window.localStorage.getItem(WIDTH_KEY));
    if (Number.isFinite(value) && value >= INSPECTOR_MIN && value <= INSPECTOR_MAX) return value;
  } catch {
    // Storage can be off (private window, blocked site data).
  }
  return INSPECTOR_DEFAULT;
}
export function storeInspectorWidth(width: number) {
  try {
    window.localStorage.setItem(WIDTH_KEY, String(Math.round(width)));
  } catch {
    // Not remembered; the default applies next time.
  }
}

/** The place's inspector: open or closed (restored with the workspace), its
 * width, and whether the layout shows it as a bottom sheet. */
export type InspectorShared = {
  open: boolean;
  setOpen: (open: boolean) => void;
  width: number;
  setWidth: (width: number) => void;
  sheet: boolean;
};
export const InspectorContext = createContext<InspectorShared | null>(null);
export function useInspector() {
  return useContext(InspectorContext);
}

/** What a view shows for its selection. */
export type InspectorContent = {
  /** Changes when the selection changes (scrolls the body to the top). */
  key: string;
  title: ReactNode;
  /** A status pill, a kind, or a short fact under the title. */
  meta?: ReactNode;
  /** Primary actions for the selection (Explain, Kill…). */
  actions?: ReactNode;
  body: ReactNode;
};

export function InspectorSection({ title, children, info }: { title: string; children: ReactNode; info?: ReactNode }) {
  const id = useId();
  return (
    <section className="inspector-block" aria-labelledby={id}>
      <h4 id={id}>
        {title}
        {info && <InfoPopover label={`About ${title}`}>{info}</InfoPopover>}
      </h4>
      {children}
    </section>
  );
}

/** A label that is an identifier (innodb_buffer_pool_size, Innodb_rows_read,
 * schema.table): one word with an underscore or a dot. */
const IDENTIFIER = /^[A-Za-z_$][\w$]*(?:\.[\w$*]+)*$/;
export function isIdentifier(text: unknown): text is string {
  return typeof text === "string" && /[_.]/.test(text) && IDENTIFIER.test(text);
}
/** Label / value pairs in the inspector. A label that is an identifier is
 * shown as code. Only code labels longer than 20 characters stack over the
 * value; short keys keep the same two-column alignment as plain labels. */
function nodeText(node: ReactNode): string {
  return Children.toArray(node).map((child) => isValidElement<{ children?: ReactNode }>(child)
    ? nodeText(child.props.children) : typeof child === "string" || typeof child === "number" ? String(child) : "").join("");
}
export function Fields({ label, rows }: { label?: string; rows: ([ReactNode, ReactNode] | null | false | undefined | "" | 0)[] }) {
  const shown = rows.filter(Boolean) as [ReactNode, ReactNode][];
  if (!shown.length) return null;
  return (
    <dl className="inspector-fields" aria-label={label}>
      {shown.map(([dt, dd], i) => (
        <div key={i}>
          <dt className={(isIdentifier(dt) ? dt.length > 20 : isValidElement<{ children?: ReactNode }>(dt) && dt.type === "code" && nodeText(dt.props.children).length > 20) ? "is-long-label" : undefined}>{isIdentifier(dt) ? <code>{dt}</code> : dt}</dt>
          <dd>{dd}</dd>
        </div>
      ))}
    </dl>
  );
}

function Inspector({
  content,
  empty,
  onClose,
  detailBackLabel,
}: {
  content: InspectorContent | null;
  empty: ReactNode;
  onClose: () => void;
  detailBackLabel?: string;
}) {
  const shared = useInspector();
  const titleId = useId();
  const body = useRef<HTMLDivElement>(null);
  const root = useRef<HTMLElement>(null);
  const sheet = !detailBackLabel && !!shared?.sheet;
  const width = shared?.width ?? INSPECTOR_DEFAULT;
  useEffect(() => {
    body.current?.scrollTo?.({ top: 0 });
    if(detailBackLabel)root.current?.querySelector<HTMLButtonElement>(".detail-back")?.focus();
  }, [content?.key]);
  // Shared scopes let nested row menus and confirmation dialogs own focus
  // without the sheet trapping their keys or closing underneath them.
  useFocusScope(root, { open: sheet, onClose });
  const drag = useRef<{ x: number; width: number } | null>(null);
  // Key repeats can arrive before the next render: resize from the latest width.
  const latest = useRef(width);
  latest.current = width;
  function resize(next: number) {
    const max = Math.min(INSPECTOR_MAX, Math.max(INSPECTOR_MIN, window.innerWidth - 640));
    const value = Math.max(INSPECTOR_MIN, Math.min(max, Math.round(next)));
    latest.current = value;
    shared?.setWidth(value);
  }
  return (
    <>
      {sheet && <div className="sheet-scrim" aria-hidden="true" onClick={onClose} />}
      <aside
        ref={root}
        className={`view-inspector${detailBackLabel ? " is-detail" : sheet ? " is-sheet" : ""}`}
        aria-labelledby={titleId}
        role={detailBackLabel ? "region" : sheet ? "dialog" : "complementary"}
        aria-modal={sheet ? true : undefined}
        style={sheet || detailBackLabel ? undefined : { width }}
        tabIndex={sheet ? -1 : undefined}
      >
        {!sheet && !detailBackLabel && (
          <div
            className="inspector-resize"
            role="separator"
            aria-orientation="vertical"
            aria-label="Inspector width"
            aria-valuemin={INSPECTOR_MIN}
            aria-valuemax={INSPECTOR_MAX}
            aria-valuenow={width}
            tabIndex={0}
            onPointerDown={(e) => {
              drag.current = { x: e.clientX, width };
              (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
            }}
            onPointerMove={(e) => {
              if (drag.current) resize(drag.current.width + drag.current.x - e.clientX);
            }}
            onPointerUp={(e) => {
              if (drag.current) resize(drag.current.width + drag.current.x - e.clientX);
              drag.current = null;
            }}
            onKeyDown={(e) => {
              const step = e.shiftKey ? 64 : 16;
              if (e.key === "ArrowLeft") resize(latest.current + step);
              else if (e.key === "ArrowRight") resize(latest.current - step);
              else if (e.key === "Home") resize(INSPECTOR_MIN);
              else if (e.key === "End") resize(INSPECTOR_MAX);
              else return;
              e.preventDefault();
            }}
          />
        )}
        <header className="inspector-head">
          {detailBackLabel&&<button type="button" className="button ghost detail-back inspector-close" onClick={onClose}><Icon name="back" size={14}/>{detailBackLabel}</button>}
          <div className="inspector-title">
            <h3 id={titleId}>{content ? content.title : "Inspector"}</h3>
            {content?.meta && <div className="inspector-meta">{content.meta}</div>}
          </div>
          {!detailBackLabel&&<IconButton icon="close" size={15} className="inspector-close" label="Close inspector" title="Close inspector (Escape)" onClick={onClose} />}
        </header>
        {content?.actions && <div className="inspector-actions-bar">{content.actions}</div>}
        <div className="inspector-scroll" ref={body}>
          {content ? content.body : <div className="inspector-placeholder">{empty}</div>}
        </div>
      </aside>
    </>
  );
}

// ---- View layout: toolbar, content, inspector -------------------------------------

/** The anatomy every data view shares: a toolbar (title, ⓘ, controls,
 * Refresh), then the content, and the inspector for the selection. Escape
 * closes the inspector and returns focus to the selected row. */
export function ViewLayout({
  id,
  title,
  hideTitle = false,
  info,
  controls,
  onRefresh,
  refreshDisabled,
  loading,
  onBack,
  inspector,
  inspectorEmpty,
  detailBackLabel = `Back to ${title.toLocaleLowerCase()}`,
  status,
  children,
  className = "",
  otherConnection,
}: {
  id: string;
  title: string;
  /** The app top bar already names this screen; keep its focus/ARIA target. */
  hideTitle?: boolean;
  info?: ReactNode;
  controls?: ReactNode;
  onRefresh?: () => void;
  refreshDisabled?: boolean;
  loading?: boolean;
  /** Narrow layouts: back to the list of views. */
  onBack?: () => void;
  inspector?: InspectorContent | null;
  inspectorEmpty?: ReactNode;
  /** Show selected content in the full view, retaining the list underneath. */
  detailBackLabel?: string;
  /** Loading line and errors, under the toolbar. */
  status?: ReactNode;
  children: ReactNode;
  className?: string;
  /** The report on screen was read from another connection (its label):
   * it is greyed, and the view turns its actions off. */
  otherConnection?: string | null;
}) {
  const reportActivity = useContext(ViewActivityContext);
  useEffect(() => {
    reportActivity?.(id, !!loading);
    return () => reportActivity?.(id, false);
  }, [reportActivity, id, loading]);
  const shared = useInspector();
  const main = useRef<HTMLDivElement>(null);
  const open = !!shared?.open && ((!shared.sheet && !detailBackLabel) || !!inspector);
  const detailOpen = !!detailBackLabel && open;
  function close() {
    shared?.setOpen(false);
    // Return focus to the selected row once the inspector is gone, unless it
    // opened again or focus already went somewhere else in the meantime.
    window.setTimeout(() => {
      const split = main.current?.parentElement;
      if (!split || split.querySelector(".view-inspector")) return;
      const active = document.activeElement as HTMLElement | null;
      if (active && active !== document.body && split.contains(active)) return;
      const row = main.current?.querySelector<HTMLElement>('.is-selected[data-row-key], [aria-current="true"][data-row-key]');
      row?.focus({ preventScroll: false });
    });
  }
  return (
    <section
      className={`server-view ${className}${otherConnection ? " is-other-connection" : ""}`}
      aria-labelledby={`${id}-title`}
      data-view={id}
      data-other-connection={otherConnection ? "true" : undefined}
      onKeyDown={(e) => {
        // A modal dialog (the Kill sheet) handles its own Escape.
        if ((e.target as HTMLElement).closest?.("dialog")) return;
        if (e.key === "Escape" && open && !e.defaultPrevented) {
          e.preventDefault();
          close();
        }
      }}
    >
      <header className="view-toolbar">
        {onBack && (
          <button type="button" className="server-back" onClick={onBack}>
            <Icon name="back" size={14} />
            Server
          </button>
        )}
        <div className="view-title">
          <h2 id={`${id}-title`} tabIndex={-1} className={hideTitle ? "visually-hidden" : undefined}>
            {title}
          </h2>
          {info && <InfoPopover label={`About ${title}`}>{info}</InfoPopover>}
        </div>
        <div className="toolbar-controls" hidden={detailOpen}>{controls}</div>
        <div className="toolbar-end">
          {onRefresh && (
            <ToolbarButton
              className="toolbar-refresh"
              aria-label="Refresh"
              data-action="refresh"
              disabled={refreshDisabled || loading}
              onClick={onRefresh}
            >
              {loading ? <span className="spinner" aria-hidden="true" /> : <Icon name="refresh" size={15} />}
              <span className="toolbar-refresh-label">Refresh</span>
            </ToolbarButton>
          )}
          {shared && !shared.sheet && !detailBackLabel && (
            <IconButton
              icon="inspector"
              className="inspector-toggle"
              aria-pressed={shared.open}
              label={shared.open ? "Hide inspector" : "Show inspector"}
              title={`${shared.open ? "Hide" : "Show"} inspector (⌥⌘I)`}
              onClick={() => (shared.open ? close() : shared.setOpen(true))}
            />
          )}
        </div>
      </header>
      {status}
      {detailOpen&&otherConnection&&<div className="gate other-connection" role="note"><Icon name="connection" size={18}/><p>From {otherConnection}, refresh to load this server.</p></div>}
      <div
        className={`view-split${open && !shared?.sheet && !detailOpen ? " has-inspector" : ""}`}
        style={open && !shared?.sheet && !detailOpen ? ({ "--inspector-w": `${shared?.width ?? INSPECTOR_DEFAULT}px` } as CSSProperties) : undefined}
      >
        <div className="view-main" ref={main} hidden={detailOpen}>
          {otherConnection && (
            <div className="gate other-connection" role="note">
              <Icon name="connection" size={18} />
              <p>From {otherConnection}, refresh to load this server.</p>
              {onRefresh && (
                <span className="gate-action">
                  <button type="button" className="button" disabled={refreshDisabled || loading} onClick={onRefresh}>
                    Refresh
                  </button>
                </span>
              )}
            </div>
          )}
          {children}
        </div>
        {open && (
          <Inspector
            content={inspector || null}
            empty={inspectorEmpty || <p>Select a row to see its details here.</p>}
            onClose={close}
            detailBackLabel={detailBackLabel}
          />
        )}
      </div>
    </section>
  );
}

/** Loading line and error for a view (under the toolbar). */
export function ViewStatus({ loading, error, children }: { loading?: string; error?: string; children?: ReactNode }) {
  if (!loading && !error) return null;
  return (
    <div className="view-status">
      {loading && (
        <p role="status" className="capture-status">
          <span className="spinner" /> {loading}
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
          {children}
        </p>
      )}
    </div>
  );
}

// ---- Automatic refresh control -----------------------------------------------------

/** "Auto-refresh: Off / 10 s / 30 s / 60 s": one control, off by default. */
export function AutoRefresh({
  value,
  options,
  onChange,
  disabled,
  describedBy,
}: {
  value: number;
  options: number[];
  onChange: (seconds: number) => void;
  disabled?: boolean;
  describedBy?: string;
}) {
  return (
    <label className="toolbar-field auto-refresh">
      <span>Auto-refresh</span>
      <Select
        label="Automatic refresh"
        describedBy={describedBy}
        value={value}
        disabled={disabled}
        onChange={(v) => onChange(+v)}
        options={[[0, "Off"], ...options.map((v): [number, string] => [v, `Every ${v} s`])]}
      />
    </label>
  );
}

/** A labeled select in a toolbar. */
export function ToolbarSelect({
  label,
  value,
  options,
  onChange,
  disabled,
  className = "",
}: {
  label: string;
  value: string | number;
  options: [string | number, string][];
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <label className={`toolbar-field ${className}`}>
      <span>{label}</span>
      <Select value={value} disabled={disabled} onChange={onChange} options={options} />
    </label>
  );
}

/** A search field in a toolbar (filters the rows on screen; never a server read). */
export function SearchField({
  label,
  value,
  onChange,
  placeholder,
  id,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  id?: string;
}) {
  return (
    <label className="toolbar-search">
      <Icon name="search" size={14} />
      <input
        id={id}
        type="search"
        aria-label={label}
        value={value}
        placeholder={placeholder || label}
        spellCheck={false}
        autoCapitalize="off"
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

// ---- Source list (grouped sub-navigation) ---------------------------------------------

export type SourceItem = {
  id: string;
  label: string;
  icon: IconName;
  /** A count badge or a status dot from data already loaded. */
  badge?: ReactNode;
  /** What the badge says, for screen readers ("2 blocked"). */
  badgeText?: string;
};
/** A System Settings-style list of views, grouped by job, with a status dot or
 * count badge per item. Arrow keys move between items; in the pushed list of
 * narrow layouts they only move focus and Enter opens the view. */
export function SourceList({
  label,
  groups,
  current,
  onSelect,
  idPrefix,
  pushed,
}: {
  label: string;
  groups: { label: string; items: SourceItem[] }[];
  current: string;
  onSelect: (id: string) => void;
  idPrefix: string;
  /** Narrow layouts: the list is a screen of its own (selection pushes the view). */
  pushed?: boolean;
}) {
  const ids = groups.flatMap((g) => g.items.map((i) => i.id));
  function onKey(event: KeyboardEvent<HTMLButtonElement>, id: string) {
    const at = ids.indexOf(id);
    const next =
      event.key === "ArrowDown"
        ? Math.min(ids.length - 1, at + 1)
        : event.key === "ArrowUp"
          ? Math.max(0, at - 1)
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? ids.length - 1
              : -1;
    if (next < 0) return;
    event.preventDefault();
    const target = document.getElementById(`${idPrefix}${ids[next]}`);
    target?.focus();
    if (!pushed) onSelect(ids[next]);
  }
  return (
    <nav className={`source-list${pushed ? " is-pushed" : ""}`} aria-label={label}>
      {groups.map((g) => (
        <div key={g.label} className="source-group">
          <h3 id={`${idPrefix}group-${g.label.toLowerCase()}`}>{g.label}</h3>
          <ul aria-labelledby={`${idPrefix}group-${g.label.toLowerCase()}`}>
            {g.items.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  id={`${idPrefix}${item.id}`}
                  className="source-item"
                  data-view={item.id}
                  aria-current={item.id === current ? "page" : undefined}
                  tabIndex={item.id === current ? 0 : -1}
                  onClick={() => onSelect(item.id)}
                  onKeyDown={(e) => onKey(e, item.id)}
                >
                  <Icon name={item.icon} />
                  <span className="source-label">{item.label}</span>
                  {item.badge}
                  {item.badgeText && <span className="visually-hidden">, {item.badgeText}</span>}
                  <span className="source-chevron" aria-hidden="true">
                    <Icon name="disclosure" size={14} />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}
