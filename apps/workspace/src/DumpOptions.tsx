import { Checkbox, Select } from "./kit";
import type { DumpCapabilities, DumpOptions as Values } from "./opsApi";

/** Render the server's allowlisted capabilities for the exact selected tool. */
export function DumpOptions({catalog, value, onChange}: {
  catalog: DumpCapabilities; value: Values; onChange: (next: Values) => void;
}) {
  const set = (key: string, next: string | boolean | number) => onChange({...value, [key]: next});
  const compression = value.compression ?? "default";
  return <>
    {catalog.options.map(option => {
      const current = value[option.key] ?? option.default;
      if (option.key === "compression_level" && !["gzip", "zstd"].includes(String(compression))) return null;
      if (option.key === "bytes_per_chunk" && value.chunking === false) return null;
      return <div className="ops-field" key={option.key}>
        {option.type === "boolean" ? <Checkbox checked={current === true} onChange={v=>set(option.key,v)}>{option.label}</Checkbox>
          : <label><span>{option.label}</span>
            {option.type === "choice" ? <Select label={option.label} value={String(current ?? "")} options={option.choices || []}
              onChange={v=>{const next={...value,[option.key]:v}; if(option.key === "compression") delete next.compression_level; onChange(next);}} />
            : option.type === "integer" ? <input aria-label={option.label} type="number" min={option.min} max={compression === "gzip" ? 9 : option.max}
                value={current == null ? "" : Number(current)} placeholder="Tool default" onChange={e=>{const next={...value};if(e.target.value === "")delete next[option.key];else next[option.key]=Number(e.target.value);onChange(next);}} />
            : <input aria-label={option.label} value={String(current ?? "")} placeholder="Tool default" onChange={e=>set(option.key,e.target.value)} />}
          </label>}
        {option.hint && <span className="hint">{option.hint}</span>}
      </div>;
    })}
    <div className="ops-field ops-wide"><span>Backup file encryption</span><span className="hint">{catalog.encryption.message}</span></div>
  </>;
}
