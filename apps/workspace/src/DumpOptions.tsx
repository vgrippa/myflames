// The dump tool's options as a two-column form: one section per group of the
// server's capability catalog (dump_options.capability_catalog), labels left
// of their controls, related checkboxes in one row, help under the field.
import { useId, type ReactNode } from "react";
import { Checkbox, Disclosure, FormGrid, FormRow, NumberField, Select } from "./kit";
import type { DumpCapabilities, DumpOption, DumpOptions as Values } from "./opsApi";

/** Older backends send no groups: the same sections, by option key. */
const DEFAULT_GROUPS: [string, string][] = [["performance", "Performance"], ["contents", "Contents"], ["output", "SQL output"], ["compression", "Compression"]];
const KEY_GROUP: Record<string, string> = {
  chunking: "performance", bytes_per_chunk: "performance", max_rate: "performance",
  triggers: "contents", routines: "contents", events: "contents",
  hex_blob: "output", add_drop_table: "output",
  compression: "compression", compression_level: "compression", network_compression: "compression",
};
const groupOf = (option: DumpOption) => option.group || KEY_GROUP[option.key] || "contents";

/** Whether an option applies with the values chosen so far (the level needs
 * Gzip or Zstandard; the chunk size needs chunking). */
export function optionShown(option: DumpOption, value: Values) {
  if (option.key === "compression_level") return ["gzip", "zstd"].includes(String(value.compression ?? "default"));
  if (option.key === "bytes_per_chunk") return value.chunking !== false;
  return true;
}

/** "4 threads · Gzip, level 6 · 2 more changes": the threads, the backup
 * compression, and how many other options differ from the tool's defaults. */
export function optionsSummary(catalog: DumpCapabilities, value: Values, threads?: number) {
  const parts: string[] = [];
  if (threads !== undefined) parts.push(`${threads} ${threads === 1 ? "thread" : "threads"}`);
  let others = 0;
  for (const option of catalog.options) {
    if (!optionShown(option, value) || !(option.key in value) || option.key === "compression_level") continue;
    const current = value[option.key];
    if (current === option.default || current === "") continue;
    if (option.key === "compression") {
      const method = option.choices?.find(([v]) => v === current)?.[1] || String(current);
      parts.push(value.compression_level !== undefined ? `${method}, level ${value.compression_level}` : method);
    } else others++;
  }
  if (others) parts.push(`${others} more ${others === 1 ? "change" : "changes"}`);
  return parts.join(" · ");
}

function CheckRow({ label, options, value, set, disabled }: {
  label: string; options: DumpOption[]; value: Values; set: (key: string, next: boolean) => void; disabled?: boolean;
}) {
  const id = useId();
  return <div className="form-row dump-check-row" role="group" aria-labelledby={id}>
    <span id={id} className="form-row-label">{label}</span>
    <div className="form-row-control dump-checks">
      {options.map(option => {
        const hint = option.hint ? `${id}-${option.key}-hint` : undefined;
        return <div key={option.key} className="dump-check">
          <Checkbox checked={(value[option.key] ?? option.default) === true} onChange={v => set(option.key, v)} disabled={disabled} describedBy={hint}>{option.label}</Checkbox>
          {option.hint && <p id={hint} className="hint">{option.hint}</p>}
        </div>;
      })}
    </div>
  </div>;
}

/** Render the server's allowlisted capabilities for the exact selected tool.
 * *threads* (MySQL Shell only) opens the Performance section. */
export function DumpOptions({ catalog, value, onChange, threads, onThreads, disabled }: {
  catalog: DumpCapabilities; value: Values; onChange: (next: Values) => void;
  threads?: number; onThreads?: (threads: number) => void; disabled?: boolean;
}) {
  const id = useId();
  const set = (key: string, next: string | boolean | number) => onChange({ ...value, [key]: next });
  const compression = String(value.compression ?? "default");
  const field = (option: DumpOption): ReactNode => {
    const current = value[option.key] ?? option.default;
    const control = `${id}-${option.key}`;
    const hint = option.hint ? `${control}-hint` : undefined;
    if (option.type === "choice")
      return <FormRow key={option.key} label={option.label} htmlFor={control} hint={option.hint}>
        <Select id={control} value={String(current ?? "")} options={option.choices || []} disabled={disabled} describedBy={hint}
          onChange={v => { const next = { ...value, [option.key]: v }; if (option.key === "compression") delete next.compression_level; onChange(next); }} />
      </FormRow>;
    if (option.type === "integer")
      return <FormRow key={option.key} label={option.label} htmlFor={control} hint={option.hint}>
        <NumberField id={control} min={option.min} max={option.key === "compression_level" && compression === "gzip" ? 9 : option.max}
          value={current == null ? "" : Number(current)} disabled={disabled} describedBy={hint}
          onChange={v => { const next = { ...value }; if (v === "") delete next[option.key]; else next[option.key] = Number(v); onChange(next); }} />
      </FormRow>;
    return <FormRow key={option.key} label={option.label} htmlFor={control} hint={option.hint}>
      <span className="dump-text">
        <input id={control} value={String(current ?? "")} placeholder={option.placeholder || "Tool default"} disabled={disabled}
          spellCheck={false} autoComplete="off" aria-describedby={hint} onChange={e => set(option.key, e.target.value)} />
        {option.unit && <span className="dump-unit">{option.unit}</span>}
      </span>
    </FormRow>;
  };
  const shown = catalog.options.filter(option => optionShown(option, value));
  const sections = (catalog.groups?.length ? catalog.groups : DEFAULT_GROUPS).map(([key, title]) => {
    const options = shown.filter(option => groupOf(option) === key);
    const rows: ReactNode[] = [];
    if (key === "performance" && threads !== undefined && onThreads)
      rows.push(<FormRow key="threads" label="Threads" htmlFor={`${id}-threads`} hint="Parallel connections that read the server.">
        <NumberField id={`${id}-threads`} min={1} max={64} unit="threads" value={threads} disabled={disabled}
          describedBy={`${id}-threads-hint`} onChange={v => onThreads(+v)} />
      </FormRow>);
    for (let i = 0; i < options.length; i++) {
      const option = options[i];
      if (option.type !== "boolean") { rows.push(field(option)); continue; }
      const row = option.row || option.label;
      const together = [option];
      while (i + 1 < options.length && options[i + 1].type === "boolean" && (options[i + 1].row || options[i + 1].label) === row) together.push(options[++i]);
      rows.push(<CheckRow key={option.key} label={row} options={together} value={value} set={set} disabled={disabled} />);
    }
    return rows.length ? <fieldset key={key} className="dump-group" data-group={key}>
      <legend>{title}</legend>
      <FormGrid>{rows}</FormGrid>
    </fieldset> : null;
  });
  return <div className="dump-options">
    {sections}
    <div className="dump-encryption">
      <span>{catalog.encryption.summary || "Backup files are not encrypted."}</span>
      <Disclosure title="Why" className="dump-encryption-why"><p className="hint">{catalog.encryption.message}</p></Disclosure>
    </div>
  </div>;
}
