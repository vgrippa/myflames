import { useEffect, useId, useRef, useState } from "react";
import { chooseBackupDirectory } from "./opsApi";

/** Native selection returns a local path. Preflight still validates its use. */
export function FolderField({ label, value, onChange, hint, disabled }: {
  label: string; value: string; onChange: (value: string) => void;
  hint: string; disabled?: boolean;
}) {
  const id = useId();
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true), pending = useRef(false);
  const latest = useRef({ value, disabled, onChange });
  latest.current = { value, disabled, onChange };
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function browse() {
    if (pending.current || disabled) return;
    pending.current = true;
    setPicking(true); setError("");
    const previous = value;
    try {
      const result = await chooseBackupDirectory();
      if (!mounted.current || latest.current.disabled || latest.current.value !== previous) return;
      if (!result.cancelled && typeof result.path === "string" && result.path)
        latest.current.onChange(result.path);
    } catch (e) {
      if (mounted.current) setError((e as Error).message);
    } finally {
      pending.current = false;
      if (mounted.current) setPicking(false);
    }
  }
  return <div className="ops-field ops-wide">
    <label htmlFor={id}>{label}</label>
    <div className="ops-folder-input">
      <input id={id} value={value} disabled={disabled || picking} spellCheck={false} autoComplete="off"
        placeholder="Choose a folder or enter its full path" aria-describedby={`${id}-hint`}
        onChange={e => { setError(""); onChange(e.target.value); }} />
      <button type="button" className="button" disabled={disabled || picking} onClick={() => void browse()}
        aria-label={`Browse for ${label.toLowerCase()}`}>{picking ? "Choosing…" : "Browse…"}</button>
    </div>
    <span id={`${id}-hint`} className="hint">{hint}</span>
    {picking && <span role="status" className="hint">Choose a folder in the system dialog.</span>}
    {error && <span role="alert">{error} You can enter the path above.</span>}
  </div>;
}
