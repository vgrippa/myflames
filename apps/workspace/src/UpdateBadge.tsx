// The update badge beside the logo: a newer version is available, its
// download is running, or it is ready to install at restart. It starts the
// automatic check and hosts the one Software update dialog, which the
// Settings Updates and news section and About myflames open too
// (updateStatus.ts).
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./components";
import { Checkbox } from "./kit";
import { lazyComponent } from "./lazy";
import { useMinuteClock } from "./UpdateCheck";
import {
  closeUpdateDialog, openUpdateDialog, setAutoCheck, stagingPercent, startAutomaticChecks,
  updateDialogReturnFocus, updatePhase, updatePhaseLabel, useUpdateStatus,
} from "./updateStatus";

// The dialog (with the release-notes renderer) is off the first screen: its
// chunk loads when the badge or the Settings entry is pointed at or focused,
// or at the latest when one of them is clicked (the button shows busy until
// the chunk is ready, so a click always reacts).
const UpdateDialog = lazyComponent(() => import("./UpdateDialog").then((m) => m.UpdateDialog));
const warmUpdateDialog = () => { void UpdateDialog.preload().catch(() => {}); };

/** Loads the dialog chunk, then opens it; the control shows busy meanwhile. */
function useOpenUpdateDialog(): [boolean, (from: HTMLElement | null, before?: () => void) => void] {
  const [preparing, setPreparing] = useState(false);
  const open = (from: HTMLElement | null, before?: () => void) => {
    if (preparing) return;
    before?.();
    setPreparing(true);
    UpdateDialog.preload()
      .catch(() => {})
      .then(() => void openUpdateDialog(from))
      .finally(() => setPreparing(false));
  };
  return [preparing, open];
}

function ProgressRing({ percent }: { percent: number }) {
  const radius = 6;
  const length = 2 * Math.PI * radius;
  return (
    <svg className="update-ring" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
      <circle className="update-ring-track" cx="8" cy="8" r={radius} />
      <circle className="update-ring-value" cx="8" cy="8" r={radius} strokeDasharray={length}
        strokeDashoffset={length * (1 - percent / 100)} transform="rotate(-90 8 8)" />
    </svg>
  );
}

export function UpdateBadge() {
  const s = useUpdateStatus();
  const now = useMinuteClock();
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => startAutomaticChecks(), []);
  const phase = updatePhase(s, now);
  const label = phase ? updatePhaseLabel(s, phase) : "";
  const percent = stagingPercent(s.staging);
  const [preparing, openDialog] = useOpenUpdateDialog();
  const [announcement, setAnnouncement] = useState("");
  const announced = useRef(new Set<string>());
  const milestone = phase === "available" || phase === "ready" ? label : "";
  useEffect(() => {
    if (!milestone || announced.current.has(milestone)) return;
    announced.current.add(milestone);
    setAnnouncement(milestone);
  }, [milestone]);
  return (
    <>
      {/* Always mounted outside the sidebar, which can be display:none.
          Announce each version's milestones once, never download percent. */}
      {createPortal(
        <span className="update-announcement visually-hidden" role="status" aria-live="polite" aria-atomic="true">
          {announcement}
        </span>,
        document.body,
      )}
      {phase && (
        <button ref={button} type="button" className="update-badge" data-phase={phase} aria-label={label} title={label}
          aria-haspopup="dialog" aria-busy={preparing || undefined} aria-disabled={preparing || undefined}
          onPointerEnter={warmUpdateDialog} onFocus={warmUpdateDialog} onClick={() => openDialog(button.current)}>
          {preparing || phase === "preparing" ? <span className="spinner" aria-hidden="true" />
            : phase === "downloading" && percent !== null ? <ProgressRing percent={percent} />
              : phase === "available" || phase === "ready" ? <Icon name={phase === "ready" ? "refresh" : "import"} size={14} />
                : <span className="spinner" aria-hidden="true" />}
        </button>
      )}
      {/* Rendered at the page root so About's styles and layout never reach it. */}
      {s.dialog && createPortal(
        <UpdateDialog updateInfo={s.dialog.info} onClose={closeUpdateDialog} returnFocus={updateDialogReturnFocus} />,
        document.body,
      )}
    </>
  );
}

/** The Settings page's entry while there is something to do (the badge alone is small on the icon rail). */
export function UpdateMenuItem({ onChoose }: { onChoose: () => void }) {
  const s = useUpdateStatus();
  const now = useMinuteClock();
  const [preparing, openDialog] = useOpenUpdateDialog();
  const phase = updatePhase(s, now);
  if (!phase) return null;
  const text = phase === "ready" ? "Restart to update…" : phase === "available" ? "Update available…" : "Update progress…";
  return (
    <button type="button" className="settings-item update-menu-item" data-phase={phase} aria-haspopup="dialog"
      aria-busy={preparing || undefined} aria-disabled={preparing || undefined}
      onPointerEnter={warmUpdateDialog} onFocus={warmUpdateDialog}
      onClick={() => openDialog(document.querySelector<HTMLElement>(".settings-button"), onChoose)}>
      {preparing ? <span className="spinner" aria-hidden="true" /> : <Icon name={phase === "ready" ? "refresh" : "import"} size={16} />}
      {text}
    </button>
  );
}

/** The Settings page switch for the automatic check. */
export function AutoUpdateSetting() {
  const s = useUpdateStatus();
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  return (
    <>
      <div className="settings-option-row update-option-row">
        <Checkbox className="check-field session-toggle update-auto-toggle" checked={s.autoCheck === true}
          disabled={s.autoCheck === null} busy={saving} describedBy="update-auto-hint"
          onChange={(on) => {
            setError("");
            setSaving(true);
            setAutoCheck(on)
              .catch(() => { if (mounted.current) setError("Could not save this setting. Try again."); })
              .finally(() => { if (mounted.current) setSaving(false); });
          }}>
          Check for updates automatically
        </Checkbox>
      </div>
      <p className={error ? "hint error" : "hint"} id="update-auto-hint" role={error ? "alert" : undefined}>
        {error || "Asks GitHub for the latest release when myflames opens and once a day."}
      </p>
    </>
  );
}
