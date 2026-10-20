// About myflames: the version, license, and credits, opened from the Settings
// menu like a desktop app's About window (see the ui-architecture vocabulary).
// The credit sentence is load-bearing: keep it verbatim, as in README and NOTICE.
import { useEffect, useId, useRef, type KeyboardEvent, type RefObject } from "react";
import { appVersion } from "./api";
import { Fields } from "./kit";

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

export function AboutDialog({
  logo,
  returnFocus,
  onClose,
}: {
  logo: string;
  /** Where focus goes when the window closes (the Settings button). */
  returnFocus: RefObject<HTMLElement>;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  // A press that starts on the backdrop closes; a text selection that ends
  // there does not.
  const pressedBackdrop = useRef(false);
  const titleId = useId();
  const taglineId = useId();
  const creditsId = useId();
  const version = appVersion();

  useEffect(() => {
    const d = dialog.current;
    if (d && !d.open) d.showModal();
    closeButton.current?.focus();
    return () => {
      // Leave the modal state before moving focus back: while a modal dialog
      // is open, the rest of the page is inert.
      if (d?.open) d.close();
      returnFocus.current?.focus();
    };
  }, [returnFocus]);

  /** Tab and Shift+Tab cycle inside the window. */
  function trap(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key !== "Tab") return;
    const items = [...(dialog.current?.querySelectorAll<HTMLElement>(FOCUSABLE) || [])];
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !items.includes(active as HTMLElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !items.includes(active as HTMLElement))) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <dialog
      ref={dialog}
      className="about-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={taglineId}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClose={onClose}
      onKeyDown={trap}
      onMouseDown={(e) => {
        pressedBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        if (pressedBackdrop.current && e.target === e.currentTarget) onClose();
        pressedBackdrop.current = false;
      }}
    >
      <div className="about-body">
        <img className="about-mark" src={logo} alt="" width="48" height="48" />
        <h2 id={titleId}>myflames</h2>
        <p className="about-subtitle">Performance workspace</p>
        <p className="about-tagline" id={taglineId}>
          MySQL and MariaDB performance workspace
        </p>
        <div className="about-facts"><Fields label="Application details" rows={[
          Boolean(version) && ["Version", <span key="version" className="about-version">{version}</span>],
          ["Workspace", "Runs on this computer"],
          ["Databases", "MySQL and MariaDB"],
          ["License", "Apache-2.0"],
        ]} /></div>
        <section className="about-section about-credits" aria-labelledby={creditsId}>
          <h3 className="section-label" id={creditsId}>
            Credits
          </h3>
          <p>
            Inspired by{" "}
            <a
              href="https://github.com/brendangregg/FlameGraph"
              target="_blank"
              rel="noopener noreferrer"
            >
              {"Brendan Gregg's FlameGraph"}
            </a>{" "}
            and{" "}
            <a
              href="https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/"
              target="_blank"
              rel="noopener noreferrer"
            >
              {"Tanel Poder's SQL Plan FlameGraphs"}
            </a>
            .
          </p>
        </section>
        <section className="about-section about-license">
          <p>Releases up to and including 2.3.0 remain available under CDDL 1.0 (see NOTICE).</p>
        </section>
        <div className="about-actions" style={{ display: "flex", gap: "var(--space-2)", marginTop: "var(--space-5)", justifyContent: "center" }}>
          <a
            href="https://github.com/myflames/myflames/issues/new"
            target="_blank"
            rel="noopener noreferrer"
            className="button ghost"
            style={{ textDecoration: "none" }}
          >
            Report a bug
          </a>
          <button ref={closeButton} type="button" className="button" style={{ minWidth: 96 }} onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </dialog>
  );
}
