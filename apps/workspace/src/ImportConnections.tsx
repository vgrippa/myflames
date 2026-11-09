import { useEffect, useRef, useState } from "react";
import { api, previewConnections, type Connection, type ConnectionImportTool, type ImportedConnection, type Profile } from "./api";
import { ActionButton, Checkbox, ConfirmSheet, DataTable, InfoPopover, Select, type Column } from "./kit";

// This importer maps endpoint fields only. Endpoint duplicates compare
// the destination and user, including SSH, rather than inventing a TLS match.
function endpoint(c: Partial<Connection>) {
  return JSON.stringify([(c.host || "localhost").trim().toLowerCase(), c.port || 3306, c.user || "",
    (c.ssh_host || "").trim().toLowerCase(), c.ssh_host ? c.ssh_user || "" : "", c.ssh_host ? c.ssh_port || 22 : 0]);
}
export function importDuplicate(c: ImportedConnection, previous: Partial<Connection>[]): string {
  if (previous.some(p => (p.name || "").trim().toLowerCase() === c.name.trim().toLowerCase())) return "Name already exists";
  if (previous.some(p => endpoint(p) === endpoint(c))) return "Endpoint already exists";
  return "";
}
export function importProblem(c: ImportedConnection): string {
  if (c.error) return c.error;
  const host = (c.host || "").trim();
  if (!host) return "A connection has no host field; URL-only connections cannot be imported.";
  if (/\s/.test(host)) return "Remove spaces from the host name.";
  if (host.includes("/")) return "Invalid connection field: host";
  if (host.startsWith("[")) {
    if (!/^\[[0-9A-Fa-f:.]+(%[\w.-]+)?\]$/.test(host))
      return "Enter only the host or IPv6 address in Host, and the port in Port.";
  } else if (host.includes(":")) {
    if ((host.match(/:/g) || []).length === 1 || !/^[0-9A-Fa-f:.]+(%[\w.-]+)?$/.test(host))
      return "Enter only the host in Host, and the port in Port.";
  }
  const bareHost = host.startsWith("[") && host.endsWith("]") ? host.slice(1, -1) : host;
  const name = (c.name || "").trim() || bareHost;
  if (!name || name.length > 200) return "Connection names must be text of at most 200 characters.";
  const port = Number(c.port ?? 3306);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return "An imported connection has an invalid port.";
  const sshHost = (c.ssh_host || "").trim(), sshUser = (c.ssh_user || "").trim(), sshPort = Number(c.ssh_port) || 0;
  if (!sshHost) return sshUser || sshPort ? "Enter the SSH host, or clear SSH user and SSH port." : "";
  if (sshHost.includes("@")) return "Enter the SSH user in SSH user, not in SSH host.";
  if (/[:/]/.test(sshHost)) return "Enter only the SSH host name or alias in SSH host, and the port in SSH port.";
  if (!/^[A-Za-z0-9_][A-Za-z0-9._-]{0,252}$/.test(sshHost))
    return "SSH host: letters, digits, dots, hyphens and underscores, not starting with a hyphen.";
  if (sshUser && !/^[A-Za-z0-9_][A-Za-z0-9._@-]{0,127}$/.test(sshUser))
    return "SSH user: letters, digits, dots, hyphens, underscores or @, not starting with a hyphen or @.";
  if (sshPort && !(Number.isInteger(sshPort) && sshPort >= 1 && sshPort <= 65535))
    return "Enter an SSH port from 1 to 65535, or leave it empty.";
  if (bareHost.includes(":") ? !/^[0-9A-Fa-f:.]+$/.test(bareHost) : !/^[A-Za-z0-9_][A-Za-z0-9._-]{0,252}$/.test(bareHost))
    return "Host cannot be forwarded through SSH.";
  return "";
}
type Row = { key: string; connection: ImportedConnection; reason: string };
export function planImportRows(
  items: ImportedConnection[],
  existing: Partial<Connection>[],
  selectedKeys?: Iterable<string>,
  saved: ReadonlySet<string> = new Set(),
): { rows: Row[]; selected: string[] } {
  const rejectedByKey = new Map<string, string>();
  items.forEach((connection, index) => {
    const key = String(index);
    const problem = importProblem(connection);
    const existingDup = problem ? "" : importDuplicate(connection, existing);
    if (problem || existingDup) rejectedByKey.set(key, problem || existingDup);
  });
  if (selectedKeys === undefined) {
    const reserved: Partial<Connection>[] = [...existing];
    const selected: string[] = [];
    const rows = items.map((connection, index) => {
      const key = String(index);
      const rejected = rejectedByKey.get(key) || "";
      if (rejected) return { key, connection, reason: rejected };
      const dup = importDuplicate(connection, reserved);
      if (dup) return { key, connection, reason: dup };
      reserved.push(connection);
      selected.push(key);
      return { key, connection, reason: "" };
    });
    return { rows, selected };
  }
  const chosen = new Set(selectedKeys);
  const reserved: Partial<Connection>[] = [...existing];
  const activeSelected = new Set<string>();
  const conflictByKey = new Map<string, string>();
  items.forEach((connection, index) => {
    const key = String(index);
    if (rejectedByKey.has(key)) return;
    if (saved.has(key)) {
      reserved.push(connection);
      if (chosen.has(key)) activeSelected.add(key);
      return;
    }
    if (!chosen.has(key)) return;
    const dup = importDuplicate(connection, reserved);
    if (dup) conflictByKey.set(key, dup);
    else {
      reserved.push(connection);
      activeSelected.add(key);
    }
  });
  const rows = items.map((connection, index) => {
    const key = String(index);
    const rejected = rejectedByKey.get(key) || "";
    if (rejected) return { key, connection, reason: rejected };
    if (saved.has(key) || activeSelected.has(key)) return { key, connection, reason: "" };
    return { key, connection, reason: conflictByKey.get(key) || importDuplicate(connection, reserved) };
  });
  return { rows, selected: [...activeSelected] };
}
export function ImportConnections({ profiles, refresh, onClose }: {
  profiles: Profile[]; refresh: () => Promise<Profile[]>; onClose: () => void;
}) {
  const [tool, setTool] = useState<ConnectionImportTool>("workbench");
  const [items, setItems] = useState<ImportedConnection[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [done, setDone] = useState(false);
  const [cursor, setCursor] = useState("");
  const generation = useRef(0);
  const reading = useRef(false);
  const importing = useRef(false);
  const savedKeys = useRef(new Set<string>());
  const profilesRef = useRef(profiles);
  profilesRef.current = profiles;
  const planned = planImportRows(items, profiles, selected, savedKeys.current);
  const rows = planned.rows;
  async function read(source: ConnectionImportTool, choose = false) {
    if (reading.current || importing.current) return;
    reading.current = true;
    const seq = ++generation.current;
    setLoading(true); setError(""); setMessage(""); setItems([]); setSelected([]); savedKeys.current.clear();
    try {
      const result = await previewConnections(source, choose);
      if (seq !== generation.current) return;
      const initial = planImportRows(result.items, profilesRef.current);
      setItems(result.items);
      setSelected(initial.selected);
      const next = initial.rows;
      setMessage(result.cancelled ? "File selection cancelled." : !result.found ? "Default file not found. Choose a connection file." : !next.length ? "No MySQL or MariaDB connections found." : `${next.length} connections found`);
    } catch (e) {
      if (seq === generation.current) setError((e as Error).message);
    } finally {
      reading.current = false;
      if (seq === generation.current) setLoading(false);
    }
  }
  useEffect(() => { void read(tool); return () => { generation.current++; }; }, [tool]);
  async function save() {
    if (importing.current || reading.current) return;
    importing.current = true; setSaving(true); setError("");
    let imported = 0;
    try {
      // Validate every selected row before saving any row so a later invalid
      // entry cannot fail after earlier rows were already persisted.
      const candidates = rows.filter(r => planned.selected.includes(r.key) && !savedKeys.current.has(r.key));
      const invalid = candidates.find(r => importProblem(r.connection));
      if (invalid) {
        setError(importProblem(invalid.connection));
        return;
      }
      // Re-read local profiles immediately before saving, so another window's
      // new connection is not silently duplicated. This never contacts a DB.
      const result = await api<{ items: Profile[] }>("profiles/list", {});
      const previous: Partial<Connection>[] = [...result.items];
      const skipped: string[] = [];
      const toSave: { key: string; value: Connection }[] = [];
      for (const row of candidates) {
        const reason = importDuplicate(row.connection, previous);
        if (reason) { skipped.push(row.connection.name); continue; }
        const c = row.connection;
        // Copy only known fields again; never spread an API object into a
        // persistence request where a future password field could sneak in.
        const value: Connection = { name: c.name, host: c.host, port: c.port, user: c.user, database: c.database,
          ssl_mode: "PREFERRED", type: "production", read_only: false,
          ...(c.ssh_host ? { ssh_host: c.ssh_host, ssh_user: c.ssh_user || "", ssh_port: c.ssh_port || 22 } : {}) };
        previous.push(value);
        toSave.push({ key: row.key, value });
      }
      for (const item of toSave) {
        await api<Profile>("profiles/save", { value: item.value, remember_password: false });
        savedKeys.current.add(item.key); imported++;
        setMessage(`Imported ${savedKeys.current.size} connections…`);
      }
      await refresh();
      setMessage(`Imported ${savedKeys.current.size} connections.${skipped.length ? ` Skipped ${skipped.length} duplicates.` : ""}`);
      setDone(true);
    } catch (e) {
      // Successful rows stay recorded after a partial failure; Retry never
      // creates them again, and all other selections remain intact.
      setError(`${(e as Error).message}${savedKeys.current.size ? ` ${savedKeys.current.size} connections already imported.` : ""}`);
      if (imported) await refresh().catch(() => {});
    } finally { importing.current = false; setSaving(false); }
  }
  const columns: Column<Row>[] = [
    { key: "select", label: "Select", hiddenLabel: true, width: 40, render: row => <Checkbox
      checked={planned.selected.includes(row.key)} disabled={!!row.reason || saving || savedKeys.current.has(row.key)}
      onChange={checked => setSelected(ids => checked ? [...new Set([...ids, row.key])] : ids.filter(id => id !== row.key))}>
      <span className="visually-hidden">Import {row.connection.name}</span></Checkbox> },
    { key: "name", label: "Name", primary: true, width: 130, render: row => <><bdi>{row.connection.name}</bdi>{row.reason && <span className="hint">{row.reason}</span>}</> },
    { key: "endpoint", label: "Endpoint", flex: true, width: 160, minWidth: 140, render: row => <bdi>{row.connection.user}@{row.connection.host}:{row.connection.port}</bdi> },
    { key: "database", label: "Database", width: 110, render: row => <bdi>{row.connection.database || "No default database"}</bdi> },
    { key: "ssh", label: "SSH", width: 140, render: row => <bdi>{row.connection.ssh_host ? `${row.connection.ssh_user || ""}@${row.connection.ssh_host}:${row.connection.ssh_port || 22}` : "Direct"}</bdi> },
  ];
  return <ConfirmSheet title="Import connections" className="connection-import-sheet" confirmTone="primary"
    description="Production connections, without passwords."
    busy={saving} busyLabel="Importing…" confirmLabel={`Import ${planned.selected.filter(k => !savedKeys.current.has(k)).length} connections`}
    confirmDisabled={loading || !planned.selected.some(k => !savedKeys.current.has(k))} doneLabel={done ? "Done" : undefined}
    returnFocus={() => document.querySelector<HTMLButtonElement>('.connections-header-actions [aria-label="Connections menu"]')}
    onCancel={onClose} onConfirm={() => void save()}>
    {!done && <div className="connection-import-controls">
      <label className="field">From<Select value={tool} label="Import from" disabled={loading || saving}
        onChange={value => setTool(value as ConnectionImportTool)} options={[["workbench", "MySQL Workbench"], ["dbeaver", "DBeaver"]]} /></label>
      <ActionButton busy={loading} busyLabel="Reading…" disabled={saving} onClick={() => void read(tool, true)}>Choose file…</ActionButton>
      <InfoPopover label="About imported connections"><p>Reads connections.xml or data-sources.json only. Password stores and SSH keys are never read. Review TLS and SSH settings before connecting.</p></InfoPopover>
    </div>}
    <p role="status" className="hint"><bdi>{message}</bdi></p>
    {error && <p role="alert" className="error"><bdi>{error}</bdi></p>}
    {!done && <div className="connection-import-preview"><DataTable label="Import preview" columns={columns} rows={rows}
      rowKey={row => row.key} selection={{ key: cursor, select: setCursor }}
      empty={<span>{loading ? "Reading connections…" : "No connections to preview"}</span>} /></div>}
  </ConfirmSheet>;
}
