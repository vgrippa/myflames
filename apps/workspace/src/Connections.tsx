// Saved connections (the API calls them profiles) and the active connection
// that the Query editor and Server use. The UI says "connection"; requests
// keep the `profiles/*` endpoints and `profile_id`.
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { api, type Connection, type ConnectionType, type Profile, type SecretStore } from "./api";
import { Icon } from "./components";
import { connectionKey } from "./binding";
import { Checkbox, ConfirmSheet, DataTable, InfoPopover, InspectorContext, Popover, closePopovers,
  SearchField, Segmented, Select, ViewLayout, useFocusScope, type Column } from "./kit";
import { actionLabel, privilegeResult, usePrivileges } from "./privileges";
import { defaultConnection, relativeTime, SSL_MODES, type SessionConnection } from "./session";

/** The connection the Query editor and Server send requests to. The password
 * is typed for this page only and is never part of the saved session. */
export type ActiveConnection = {
  /** Saved connection id, or "" for settings that are not saved. */
  id: string;
  fields: SessionConnection;
  password: string;
};

/** Plain-language location of a remembered password ("saved in macOS Keychain"). */
export function storedIn(backend?: string, label?: string) {
  return backend === "vault"
    ? "encrypted on this computer"
    : `saved in ${label || "the system credential store"}`;
}
/** "Password saved in macOS Keychain" or "No saved password". */
export function passwordStatus(p?: Profile) {
  return p?.has_saved_password
    ? `Password ${storedIn(p.secret_backend, p.secret_backend_label)}`
    : "No saved password";
}
/** The non-secret fields of a connection or saved connection, in session order. */
export function connectionFields(c: Partial<Connection>): SessionConnection {
  return {
    name: c.name || "",
    host: c.host || "",
    port: c.port || 3306,
    user: c.user || "",
    database: c.database || "",
    ssl_mode: c.ssl_mode || "PREFERRED",
    ssl_ca: c.ssl_ca || "",
    read_only: c.read_only === true,
  };
}
/** "analyst@127.0.0.1:3306" */
export function connectionTarget(c: Pick<Connection, "user" | "host" | "port">) {
  if (!c.host) return "No connection";
  return `${c.user ? `${c.user}@` : ""}${c.host}:${c.port}`;
}
// ---- Write safety: Production, Development, Read-only (db-operations) ------

/** The write safety of the connection in use, as the server judges it
 * (dbops.connection_safety): the saved connection's type and read-only flag
 * while the settings in use keep its host, port, user and TLS settings;
 * otherwise Production, read-only when either the saved connection or the
 * settings in use ask for it. An unsaved connection is always Production. The
 * request carries the result, so the server's answer is the same. */
export type Safety = {
  type: ConnectionType;
  read_only: boolean;
  /** A saved connection whose host, port, user and TLS settings are in use. */
  endpointMatches: boolean;
  /** False until the saved connections are loaded (the type is not known yet). */
  known: boolean;
};
/** Host, port, user and TLS settings, compared like workspace_store.ENDPOINT_FIELDS. */
function endpoint(c: Partial<Connection>) {
  return JSON.stringify([c.host || "127.0.0.1", Number(c.port) || 3306, c.user || "", c.ssl_mode || "", c.ssl_ca || ""]);
}
export function sameEndpoint(a: Partial<Connection>, b: Partial<Connection>) {
  return endpoint(a) === endpoint(b);
}
export function connectionSafety(active: ActiveConnection, profiles: Profile[] | null): Safety {
  const asked = active.fields.read_only === true;
  if (!active.id) return { type: "production", read_only: asked, endpointMatches: false, known: true };
  const profile = profiles?.find((p) => p.id === active.id);
  if (!profile) return { type: "production", read_only: asked, endpointMatches: false, known: !!profiles };
  const matches = sameEndpoint(active.fields, profile);
  return {
    type: matches && profile.type === "development" ? "development" : "production",
    // While its endpoint is in use the saved connection decides (so a change
    // made elsewhere shows on the next read of the list); with other
    // settings on screen, their own read-only choice counts too (the server
    // takes either: a request never lowers the protection).
    read_only: profile.read_only === true || (!matches && asked),
    endpointMatches: matches,
    known: true,
  };
}
export const TYPE_LABEL: Record<ConnectionType, string> = { production: "Production", development: "Development" };
/** What each type confirms, for the ⓘ next to Type. */
function TypeInfo() {
  return (
    <InfoPopover label="About connection types">
      <p>Production asks before every change myflames sends. Development asks only before risky ones: DDL, KILL, SET GLOBAL, and UPDATE or DELETE without a key.</p>
      <p>Read-only refuses every change, whatever the type. A connection used without saving is Production.</p>
    </InfoPopover>
  );
}
/** The small dot that names a connection's type (red for Production). */
export function TypeDot({ type, readOnly }: { type: ConnectionType; readOnly?: boolean }) {
  const label = readOnly ? `${TYPE_LABEL[type]}, read-only` : TYPE_LABEL[type];
  return <span className={`type-dot is-${readOnly ? "read-only" : type}`} role="img" aria-label={label} title={label} />;
}
/** Type and Read-only in a connection row. */
function SafetyTags({ type, readOnly }: { type: ConnectionType; readOnly: boolean }) {
  return (
    <span className="safety-tags">
      <span className={`type-tag is-${type}`}>{TYPE_LABEL[type]}</span>
      {readOnly && <span className="type-tag is-read-only">Read-only</span>}
    </span>
  );
}
/**
 * The write policy of the connection in use, for write actions (Kill). While
 * it is not known (saved connections not loaded, or the list failed),
 * `blocked` says why and write actions stay off. `check` reads the saved
 * connections again right before a write is sent and returns the request
 * connection with the type and read-only flag as they are now, so a sheet
 * confirmed under another policy (changed in another tab or the CLI) sends
 * nothing: its connection key no longer matches.
 */
export type WritePolicy = { blocked: string; check: () => Promise<Connection> };
export const WritePolicyContext = createContext<WritePolicy | null>(null);
export function useWritePolicy() {
  return useContext(WritePolicyContext);
}
/** The strip across the Query editor and Server: Production asks before
 * every change, Read-only refuses them; nothing for Development. While the
 * policy is not known it assumes Production and says why (`unknown`:
 * "loading" or the error text), with Retry after a failure. */
export function SafetyStrip({
  safety,
  name,
  unknown,
  onRetry,
}: {
  safety: Safety;
  name: string;
  unknown?: string;
  onRetry?: () => void;
}) {
  if (!safety.known)
    return (
      <div className="safety-strip is-production is-unknown" role="note" data-safety="unknown">
        <strong>Production assumed</strong>
        <span>
          {unknown === "loading"
            ? `reading the settings of ${name}; changes stay off until they load`
            : `could not read the settings of ${name}; changes stay off until they load`}
        </span>
        {unknown !== "loading" && onRetry && (
          <button type="button" className="strip-retry" onClick={onRetry}>
            Retry
          </button>
        )}
      </div>
    );
  if (!safety.read_only && safety.type !== "production") return null;
  return safety.read_only ? (
    <div className="safety-strip is-read-only" role="note" data-safety="read-only">
      <strong>Read-only</strong>
      <span>myflames refuses every change on {name}</span>
    </div>
  ) : (
    <div className="safety-strip is-production" role="note" data-safety="production">
      <strong>Production</strong>
      <span>every change on {name} asks first</span>
    </div>
  );
}

/** What a request sends: the active settings, its typed password, and the
 * saved connection id so the server can fill a remembered password, with the
 * type and read-only flag it is judged by (binding.ts connectionKey). */
export function requestConnection(active: ActiveConnection, safety?: Safety): Connection {
  // Empty host alone is not enough: the backend defaults it to localhost.
  // An invalid port keeps a cleared picker from opening a new local session.
  const c: Connection = { ...active.fields, port: active.fields.host ? active.fields.port : 0, password: active.password };
  if (safety) {
    c.type = safety.type;
    c.read_only = safety.read_only;
  }
  return active.id ? { ...c, profile_id: active.id } : c;
}

const blank = (): Connection => ({ ...defaultConnection, password: "", type: "production" });

/** A row's Test result, for the connection identity it tested (binding.ts
 * connectionKey) and the request that produced it. */
type ConnectionRow = { key: string; fields: SessionConnection; profile?: Profile };
// The API has no usage history. Keep only observations from this page lifetime,
// never substitute the profile's modification time for its last use.
const lastUsed = new Map<string, string>();
export function recordConnectionUse(id: string) {
  if (id) lastUsed.set(id, new Date().toISOString());
}

type RowTest = { identity: string; seq: number } & ({ state: "testing" } | { state: "ok" | "failed"; text: string });

/**
 * The Connections section of Home: saved connections as rows, and a form to
 * add or edit one. Save connection asks about a duplicate name and about the
 * typed password; a successful Test connection offers to save the password.
 */
export function Connections({
  profiles,
  secretStore,
  refresh,
  active,
  busy,
  onUse,
  onActiveDeleted,
  loading = false,
  loadError = "",
}: {
  profiles: Profile[];
  secretStore: SecretStore | null;
  refresh: () => Promise<Profile[]>;
  active: ActiveConnection;
  /** A capture is running: saving and testing wait. */
  busy: boolean;
  /** Make these settings the active connection. */
  onUse: (id: string, fields: SessionConnection, password: string) => void;
  loading?: boolean;
  loadError?: string;
  /** Clear both settings and typed credentials after removing the active row. */
  onActiveDeleted: () => void;
}) {
  // null: the form is closed. "": a new connection. Otherwise the id being edited.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Connection>(blank);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  // One save at a time (the ref answers before React re-renders).
  const savingRef = useRef(false);
  // formGen changes when the form opens or closes; draftGen also when a field
  // changes. A save applies its answer only to the same form, a test only to
  // the same settings.
  const formGen = useRef(0);
  const draftGen = useRef(0);
  const testSeq = useRef(0);
  const statusFromTest = useRef(false);
  const activeRef = useRef(active);
  activeRef.current = active;
  // Changes on every Use here; with the active connection's settings it tells
  // whether a newer choice was made while a save was on its way.
  const useGen = useRef(0);
  const choice = () => JSON.stringify([useGen.current, activeRef.current.id, activeRef.current.fields]);
  const rowSeq = useRef(0);
  // Save connection asks about a duplicate name, then about the password.
  // `target` undefined = the connection being edited (or a new one); "" = new.
  const [savePrompt, setSavePrompt] = useState<null | {
    duplicate?: Profile;
    askPassword: boolean;
    target?: string;
  }>(null);
  const savePromptRef = useRef<HTMLDivElement>(null);
  useFocusScope(savePromptRef, { open: !!savePrompt, onClose: () => setSavePrompt(null) });
  // After a successful connection test with a typed password, offer to save it.
  const [offerSave, setOfferSave] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<ConnectionRow[] | null>(null);
  const [deleting, setDeleting] = useState(false);
  const deletingRef = useRef(false);
  const [deletedIds, setDeletedIds] = useState<string[]>([]);
  const [deleteErrors, setDeleteErrors] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<string[]>([]);
  const [cursor, setCursor] = useState("");
  const [search, setSearch] = useState("");
  const [sheet, setSheet] = useState(() => typeof window !== "undefined" && window.innerWidth < 1040);
  const [inspectorWidth, setInspectorWidth] = useState(340);
  const tableRef = useRef<HTMLDivElement>(null);
  const addButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 1039px)");
    const change = () => setSheet(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  const [rowTests, setRowTests] = useState<Record<string, RowTest>>({});
  // Development holds only for one host, port, user and TLS settings: the
  // ones a saved Development connection was saved with, or the ones on screen
  // when Development was chosen. Any change to them makes the form show and
  // save Production until Development is chosen again (as the server does).
  const [devFor, setDevFor] = useState<{ key: string; label: string } | null>(null);
  const nameField = useRef<HTMLInputElement>(null);
  const formRef = useRef<HTMLDivElement>(null);

  const profile = editingId ? profiles.find((p) => p.id === editingId) : undefined;
  // A saved connection with a remembered password connects without typing it:
  // the server fills the password for that one request from the secret store.
  const savedPassword = !!profile?.has_saved_password;
  const usingSaved = savedPassword && !draft.password;
  const formConnection: Connection = editingId
    ? { ...draft, profile_id: editingId }
    : draft;
  const canRemember = !!secretStore?.available;
  const movedFromDevelopment = !!devFor && devFor.key !== endpoint(draft);
  const shownType: ConnectionType = devFor && !movedFromDevelopment ? "development" : "production";
  /** Development for the endpoint of *c* (null: Production). */
  const developmentFor = (c: Partial<Connection>) => ({
    key: endpoint(c),
    label: `${connectionTarget({ user: c.user || "", host: c.host || "", port: c.port || 3306 })}${c.ssl_mode ? ` (TLS ${c.ssl_mode})` : ""}`,
  });

  function openForm(p?: Profile, fields?: SessionConnection) {
    formGen.current++;
    draftGen.current++;
    const source = p || fields;
    const key = p?.id || (fields === active.fields ? "active" : "");
    setCursor(key);
    setSelected(key ? [key] : []);
    setEditingId(p ? p.id : "");
    setDevFor(p?.type === "development" ? developmentFor(p) : null);
    setDraft(
      source
        ? { ...connectionFields(source), password: fields === active.fields ? active.password : "", type: p?.type === "development" ? "development" : "production" }
        : blank(),
    );
    setSavePrompt(null);
    setOfferSave(false);
    setSchemas([]);
    setStatus("");
    setError("");
    if (!sheet) window.setTimeout(() => nameField.current?.focus());
  }
  function closeForm() {
    formGen.current++;
    draftGen.current++;
    setEditingId(null);
    setSavePrompt(null);
    setOfferSave(false);
  }
  function chooseType(type: ConnectionType) {
    setDevFor(type === "development" ? developmentFor(draft) : null);
  }
  function edit<K extends keyof Connection>(key: K, value: Connection[K]) {
    setDraft((c) => ({ ...c, [key]: value }));
    // Type and Read-only do not change what a test connects to.
    if (key === "type" || key === "read_only") return;
    draftGen.current++;
    if (editingId) setRowTests(tests => {
      const next = { ...tests };
      delete next[editingId];
      return next;
    });
    // A test result describes the settings that were tested, not these.
    if (statusFromTest.current) {
      statusFromTest.current = false;
      setStatus("");
      setOfferSave(false);
    }
  }

  /** Save the connection. `withPassword` stores the typed password in the
   * secret store. Otherwise a password already saved for that connection is
   * kept, and a connection without one stays without one. */
  async function saveProfile(withPassword: boolean, targetId = editingId || "") {
    // Single flight: a second click while a save runs does nothing, so one
    // Save never creates two connections.
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError("");
    setSavePrompt(null);
    setOfferSave(false);
    // What is saved, the form it came from, and the connection in use, as
    // they are now; each is checked again after every await.
    const gen = formGen.current;
    const chosen = choice();
    const startedOn = activeRef.current.id;
    const { password, ...rest } = draft;
    // The type is always sent: the one on screen (a moved Development
    // connection shows and saves Production until Development is chosen again).
    const settings = { ...rest, type: shownType, read_only: draft.read_only === true };
    try {
      const target = profiles.find((p) => p.id === targetId);
      const storeTyped = withPassword && !!password;
      const keepExisting = !storeTyped && !!target?.has_saved_password;
      const p = await api<Profile>("profiles/save", {
        id: targetId || undefined,
        value: settings,
        password: storeTyped ? password : undefined,
        remember_password: storeTyped || keepExisting,
      });
      await refresh();
      // The edited connection stays in use, and a first saved connection
      // becomes the one in use, unless the user chose another connection
      // (Use, Use without saving, the picker) or moved to another form meanwhile.
      const sameForm = formGen.current === gen;
      if (sameForm) {
        setEditingId(p.id);
        // The form now shows what was saved (a moved Development connection
        // saved as Production stays Production on screen).
        setDevFor(settings.type === "development" ? developmentFor(settings) : null);
      }
      if (choice() === chosen && ((sameForm && !startedOn) || startedOn === p.id || (!!targetId && startedOn === targetId)))
        onUse(p.id, connectionFields(settings), password || "");
      statusFromTest.current = false;
      setStatus(
        p.has_saved_password
          ? `Connection "${p.name}" saved. Password ${storedIn(p.secret_backend, p.secret_backend_label)}.`
          : `Connection "${p.name}" saved without a password. Enter it when you connect; you can save it later.`,
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }
  /** Save connection: confirm a duplicate name, then ask whether to keep the
   * typed password, so the choice is never a checkbox that is easy to miss. */
  function startSave() {
    setError("");
    setOfferSave(false);
    const name = (draft.name || "").trim().toLowerCase();
    const duplicate =
      !editingId && name
        ? profiles.find((p) => (p.name || "").trim().toLowerCase() === name)
        : undefined;
    const askPassword = !!draft.password && canRemember;
    if (duplicate || askPassword) setSavePrompt({ duplicate, askPassword });
    else void saveProfile(false);
  }
  /** Duplicate name resolved: replace (its id) or save as new (""). */
  function chooseTarget(target: string) {
    if (savePrompt?.askPassword) setSavePrompt({ ...savePrompt, target });
    else void saveProfile(false, target);
  }
  async function testForm() {
    // The answer belongs to these settings: it is dropped once the fields
    // change or another form opens, and a newer test replaces it.
    const gen = draftGen.current;
    const seq = ++testSeq.current;
    const tested = { editing: editingId, password: draft.password };
    setTesting(true);
    setError("");
    try {
      const r = await api<{ version: string; schemas: string[] }>(
        "connection/test",
        { connection: formConnection },
      );
      if (draftGen.current !== gen || testSeq.current !== seq) return;
      setSchemas(r.schemas);
      statusFromTest.current = true;
      setStatus(`Connected · ${r.version}`);
      if (tested.editing && !savedPassword && tested.password && canRemember)
        setOfferSave(true);
    } catch (e) {
      if (draftGen.current === gen && testSeq.current === seq)
        setError((e as Error).message);
    } finally {
      if (testSeq.current === seq) setTesting(false);
    }
  }
  async function forget(id: string) {
    setError("");
    try {
      await api("profiles/forget-password", { id });
      await refresh();
      setStatus("Saved password forgotten. Enter the password to connect.");
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function askDelete(rows: ConnectionRow[]) {
    if (!rows.length || deletingRef.current || savingRef.current) return;
    setError("");
    setConfirmDelete(rows);
  }
  async function remove() {
    if (!confirmDelete || deletingRef.current) return;
    // DataTable owns sorting. Preserve its displayed order, rather than the
    // profile storage order, so deleting a row advances to its neighbour.
    const displayed = Array.from(tableRef.current?.querySelectorAll<HTMLElement>("tbody [data-row-key]") || [])
      .map(row => row.dataset.rowKey!);
    const targets = new Set(confirmDelete.map(row => row.key));
    const anchor = displayed.indexOf(targets.has(cursor) ? cursor : displayed.find(key => targets.has(key)) || "");
    const focusOrder = anchor < 0 ? displayed : [...displayed.slice(anchor), ...displayed.slice(0, anchor).reverse()];
    deletingRef.current = true;
    setDeleting(true);
    setDeleteErrors({});
    const failures: Record<string, string> = {};
    const removed: string[] = [];
    // Per-id endpoints are deliberately non-atomic. A failed row stays selected
    // with its own error; successful deletions never reappear after a list error.
    for (const row of confirmDelete) {
      try {
        if (row.profile) await api("profiles/delete", { id: row.key });
        removed.push(row.key);
        lastUsed.delete(row.key);
        setDeletedIds(ids => [...ids, row.key]);
        if (row.profile ? activeRef.current.id === row.key :
          !activeRef.current.id && sameEndpoint(activeRef.current.fields, row.fields)) onActiveDeleted();
        if (editingId === row.key || (!row.profile && editingId === "")) closeForm();
      } catch (e) { failures[row.key] = (e as Error).message; }
    }
    setSelected(Object.keys(failures));
    setDeleteErrors(failures);
    setStatus(removed.length ? `${removed.length} ${removed.length === 1 ? "connection removed" : "connections removed"}.` : "");
    try { await refresh(); }
    catch (e) { setError(`Could not refresh connections: ${(e as Error).message}`); }
    finally {
      deletingRef.current = false;
      setDeleting(false);
      setConfirmDelete(null);
      // Wait for both the confirmation and any deleted row's form to close.
      // Failed deletions remain in the table and can receive focus again.
      window.setTimeout(() => {
        const remaining = Array.from(tableRef.current?.querySelectorAll<HTMLElement>("tbody [data-row-key]") || []);
        const next = focusOrder.map(key => remaining.find(row => row.dataset.rowKey === key)).find(Boolean) || remaining[0];
        setCursor(next?.dataset.rowKey || "");
        if (next) {
          next.focus();
          next.scrollIntoView({ block: "nearest" });
        } else addButtonRef.current?.focus();
      });
    }
  }
  /** Test a row. The answer applies only to the latest test of that row, and
   * shows only while the row still has the settings that were tested. */
  async function testRow(key: string, connection: Connection) {
    const identity = connectionKey(connection);
    const seq = ++rowSeq.current;
    const put = (next: RowTest) => setRowTests((t) => (t[key]?.seq === seq ? { ...t, [key]: next } : t));
    setRowTests((t) => ({ ...t, [key]: { state: "testing", identity, seq } }));
    try {
      const r = await api<{ version: string }>("connection/test", { connection });
      put({ state: "ok", text: `Connected · ${r.version}`, identity, seq });
    } catch (e) {
      put({ state: "failed", text: (e as Error).message, identity, seq });
    }
  }
  function use(id: string, fields: SessionConnection, password: string) {
    useGen.current++;
    onUse(id, fields, password);
    setStatus(`Using "${fields.name || connectionTarget(fields)}" in the Query editor and Server.`);
    setError("");
  }

  const storeSentence =
    secretStore && !canRemember
      ? "Passwords stay in memory for this session."
      : `Save connection asks whether to keep the password; a saved password is ${storedIn(secretStore?.backend, secretStore?.label)}, never in the workspace database, captures, or exports.`;

  const rows: ConnectionRow[] = [
    ...(!active.id && active.fields.host ? [{ key: "active", fields: active.fields }] : []),
    ...profiles.filter(p => !deletedIds.includes(p.id)).map(p => ({ key: p.id, fields: connectionFields(p), profile: p })),
  ];
  const visibleRows = rows.filter(row => [row.fields.name, connectionTarget(row.fields), row.fields.database]
    .some(value => value.toLowerCase().includes(search.toLowerCase())));
  const selectedRows = rows.filter(row => selected.includes(row.key));
  function activate(row: ConnectionRow) {
    if (deleting || saving) return;
    use(row.profile?.id || "", row.fields, (row.key === active.id || row.key === "active") && sameEndpoint(row.fields, active.fields) ? active.password : "");
  }
  function duplicate(row: ConnectionRow) {
    openForm(undefined, { ...row.fields, name: `${row.fields.name || connectionTarget(row.fields)} copy` });
    // Copy settings, never a remembered or typed password. Preserve the policy
    // only for the same endpoint, with the same guard as editing a connection.
    setDevFor(row.profile?.type === "development" ? developmentFor(row.fields) : null);
  }
  const columns: Column<ConnectionRow>[] = [
    { key: "select", label: "Select", hiddenLabel: true, width: 48, render: row => (
      <Checkbox checked={selected.includes(row.key)} disabled={deleting} onChange={checked => {
        setCursor(row.key);
        setSelected(ids => checked ? [...new Set([...ids, row.key])] : ids.filter(id => id !== row.key));
      }}><span className="visually-hidden">Select {row.fields.name || connectionTarget(row.fields)}</span></Checkbox>
    ) },
    { key: "name", label: "Name", primary: true, flex: true, width: 180, sort: row => row.fields.name,
      render: row => <span className="cell-name">
        <TypeDot type={row.profile?.type || "production"} readOnly={row.fields.read_only} />
        <strong className="cell-truncate" title={row.fields.name || connectionTarget(row.fields)}>{row.fields.name || connectionTarget(row.fields)}</strong>
        <span className="connection-type-short" aria-hidden="true">
          {row.fields.read_only ? "Read-only" : row.profile?.type === "development" ? "Dev" : "Prod"}
        </span>
      </span> },
    { key: "type", label: "Type", width: 132, sort: row => row.profile?.type || "production", render: row => (
      <span className={`type-tag is-${row.fields.read_only ? "read-only" : row.profile?.type || "production"}`}
        title={`${TYPE_LABEL[row.profile?.type || "production"]}${row.fields.read_only ? " · Read-only" : ""}`}>
        {row.profile?.type === "development" ? "Development" : "Production"}{row.fields.read_only ? " · Read-only" : ""}
      </span>
    ) },
    { key: "endpoint", label: "user@host:port", width: 200, sort: row => connectionTarget(row.fields),
      render: row => <span className="cell-truncate" title={connectionTarget(row.fields)}>{connectionTarget(row.fields)}</span> },
    { key: "schema", label: "Default schema", width: 140, sort: row => row.fields.database,
      render: row => <span className="cell-truncate" title={row.fields.database || "None"}>{row.fields.database || "None"}</span> },
    { key: "password", label: "Password", width: 96, sort: row => row.profile?.has_saved_password ? 1 : 0,
      render: row => <span className="cell-truncate" title={passwordStatus(row.profile)}>{row.profile?.has_saved_password ? "Saved" : "None"}</span> },
    { key: "last-used", label: "Last used", width: 140, title: "Usage observed during this window; earlier use is not recorded.",
      sort: row => lastUsed.get(row.key) || "", render: row => row.profile?.id === active.id || row.key === "active" ? <span title="In use">In use</span>
        : lastUsed.has(row.key) ? <time className="cell-truncate" title={new Date(lastUsed.get(row.key)!).toLocaleString()} dateTime={lastUsed.get(row.key)}>{relativeTime(lastUsed.get(row.key)!)}</time>
          : <span className="cell-truncate" title="Usage before this window is not recorded.">Not recorded</span> },
  ];
  function rowMenu(row: ConnectionRow) {
    const name = row.fields.name || connectionTarget(row.fields);
    const act = (action: () => void) => { closePopovers(); action(); };
    return <Popover menu label={`Actions for ${name}`} buttonTitle={`Actions for ${name}`} buttonClassName="icon-button connection-actions"
      disabled={deleting || saving} trigger={<Icon name="more" />}>
      <div className="connection-menu">
        {row.profile ? <>
          <button role="menuitem" onClick={() => act(() => activate(row))} aria-label={`Use ${name}`}>Use</button>
          <button role="menuitem" onClick={() => act(() => openForm(row.profile))} aria-label={`Edit ${name}`}>Edit</button>
          <button role="menuitem" onClick={() => act(() => duplicate(row))} aria-label={`Duplicate ${name}`}>Duplicate</button>
          <button role="menuitem" disabled={busy} onClick={() => act(() => {
            openForm(row.profile);
            void testRow(row.key, { ...row.fields, type: row.profile?.type, profile_id: row.key, password: row.key === active.id && sameEndpoint(row.fields, active.fields) ? active.password : "" });
          })} aria-label={`Test ${name}`}>Test</button>
          <button role="menuitem" disabled={busy || !row.profile.has_saved_password}
            onClick={() => act(() => { openForm(row.profile); void forget(row.key); })}
            aria-label={`Forget the saved password of ${name}`}>Forget password</button>
          <button role="menuitem" onClick={() => act(() => askDelete([row]))} aria-label={`Delete ${name}…`}>Delete…</button>
        </> : <>
          <button role="menuitem" onClick={() => act(() => openForm(undefined, row.fields))} aria-label={`Save ${name}`}>Save</button>
          <button role="menuitem" onClick={() => act(() => askDelete([row]))} aria-label={`Remove ${name}`}>Remove</button>
        </>}
      </div>
    </Popover>;
  }
  const form = (
        <div
          className="connection-panel"
          ref={formRef}
          role="group"
          aria-labelledby="connection-form-title"
        >
          <h3 id="connection-form-title" className="visually-hidden">
            {editingId ? `Edit “${profile?.name || draft.name}”` : "Add connection"}
          </h3>
          <div className="connection-fields">
            <label className="field">
              Connection name
              <input
                ref={nameField}
                value={draft.name}
                onChange={(e) => edit("name", e.target.value)}
              />
            </label>
            <label className="field">
              Host
              <input
                value={draft.host}
                onChange={(e) => edit("host", e.target.value)}
              />
            </label>
            <div className="field-pair">
              <label className="field">
                Port
                <input
                  type="number"
                  min="1"
                  max="65535"
                  value={draft.port}
                  onChange={(e) => edit("port", +e.target.value)}
                />
              </label>
              <label className="field">
                User
                <input
                  autoComplete="username"
                  value={draft.user}
                  onChange={(e) => edit("user", e.target.value)}
                />
              </label>
            </div>
            <div className="password-field">
              <label className="field">
                Password
                <input
                  type="password"
                  autoComplete="current-password"
                  value={draft.password || ""}
                  placeholder={usingSaved ? "Saved password" : ""}
                  aria-describedby={
                    savedPassword
                      ? "saved-password-hint"
                      : editingId
                        ? "no-saved-password-hint"
                        : undefined
                  }
                  onChange={(e) => edit("password", e.target.value)}
                />
              </label>
              {editingId && !savedPassword && (
                <p className="hint" id="no-saved-password-hint">
                  No password is saved for this connection. Enter it to connect
                  {canRemember ? "; Save connection asks whether to keep it." : "."}
                </p>
              )}
              {savedPassword && (
                <div className="saved-password">
                  <p className="hint" id="saved-password-hint">
                    {usingSaved
                      ? `Using the password ${storedIn(profile?.secret_backend, profile?.secret_backend_label)}. Type a password to use a different one for this session.`
                      : "Using the typed password for this session. Clear the field to use the saved one."}
                  </p>
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => editingId && forget(editingId)}
                  >
                    Forget saved password
                  </button>
                </div>
              )}
            </div>
            <label className="field">
              Schema
              <input
                list="schema-list"
                value={draft.database}
                onChange={(e) => edit("database", e.target.value)}
              />
              <datalist id="schema-list">
                {schemas.map((s) => (
                  <option key={s} value={s} />
                ))}
              </datalist>
            </label>
          </div>
          <div className="connection-safety" role="group" aria-labelledby="connection-type-label">
            <div className="safety-field">
              <span className="field-label" id="connection-type-label">
                Type
                <TypeInfo />
              </span>
              <Segmented<ConnectionType>
                label="Connection type"
                className="type-control"
                value={shownType}
                onChange={chooseType}
                options={[
                  { value: "production", label: "Production", title: "Asks before every change" },
                  { value: "development", label: "Development", title: "Asks only before risky changes" },
                ]}
              />
            </div>
            <Checkbox className="read-only-field" checked={draft.read_only === true} onChange={(v) => edit("read_only", v)}>
              Read-only
            </Checkbox>
          </div>
          {movedFromDevelopment ? (
            <p className="hint safety-hint is-warning" role="note">
              Development covers {devFor!.label} only. Other host, port, user or TLS settings are Production until you
              choose Development again.
            </p>
          ) : (
            !editingId &&
            shownType === "development" && (
              <p className="hint safety-hint" role="note">
                Development applies once saved. Used without saving, a connection is Production.
              </p>
            )
          )}
          <details>
            <summary>TLS settings</summary>
            <div className="connection-fields">
              <label className="field">
                TLS mode
                <Select
                  value={draft.ssl_mode}
                  onChange={(v) => edit("ssl_mode", v)}
                  options={SSL_MODES.map((m): [string, string] => [m, m])}
                />
              </label>
              <label className="field">
                CA file path
                <input
                  value={draft.ssl_ca || ""}
                  onChange={(e) => edit("ssl_ca", e.target.value)}
                />
              </label>
            </div>
          </details>
          <div className="button-row">
            <button
              className="button"
              disabled={testing || busy || deleting || saving}
              onClick={testForm}
            >
              {testing ? "Connecting…" : "Test connection"}
            </button>
            <button
              className="button primary"
              disabled={busy || !!savePrompt || saving || deleting}
              onClick={startSave}
            >
              {saving ? "Saving…" : "Save connection"}
            </button>
            <button
              className="button"
              onClick={() =>
                use(editingId || "", connectionFields(draft), draft.password || "")
              }
            >
              {editingId ? "Use" : "Use without saving"}
            </button>
          </div>
          {savePrompt && (
            <div
              className="save-prompt"
              role="group"
              aria-labelledby="save-prompt-title"
              ref={savePromptRef}
              tabIndex={-1}
            >
              {savePrompt.duplicate && savePrompt.target === undefined ? (
                <>
                  <p id="save-prompt-title">
                    <strong>
                      A connection named “{savePrompt.duplicate.name}” already
                      exists.
                    </strong>{" "}
                    Replace it with these settings, or save a new one?
                  </p>
                  <div className="button-row">
                    <button
                      className="button primary"
                      autoFocus
                      disabled={saving}
                      onClick={() => chooseTarget(savePrompt.duplicate!.id)}
                    >
                      Replace
                    </button>
                    <button className="button" disabled={saving} onClick={() => chooseTarget("")}>
                      Save as new
                    </button>
                    <button
                      className="text-button"
                      onClick={() => setSavePrompt(null)}
                    >
                      Cancel
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <p id="save-prompt-title">
                    <strong>Also save the password?</strong> It will be{" "}
                    {storedIn(secretStore?.backend, secretStore?.label)}, never
                    in the workspace database, captures, or exports.
                  </p>
                  <div className="button-row">
                    <button
                      className="button primary"
                      autoFocus
                      disabled={saving}
                      onClick={() =>
                        saveProfile(true, savePrompt.target ?? editingId ?? "")
                      }
                    >
                      Save with password
                    </button>
                    <button
                      className="button"
                      disabled={saving}
                      onClick={() =>
                        saveProfile(false, savePrompt.target ?? editingId ?? "")
                      }
                    >
                      Settings only
                    </button>
                    <button
                      className="text-button"
                      onClick={() => setSavePrompt(null)}
                    >
                      Cancel
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
          {offerSave && profile && !savedPassword && draft.password && (
            <div
              className="save-prompt"
              role="group"
              aria-labelledby="offer-save-title"
            >
              <p id="offer-save-title">
                <strong>Save this password for “{profile.name}”?</strong> It
                will be {storedIn(secretStore?.backend, secretStore?.label)}.
              </p>
              <div className="button-row">
                <button
                  className="button primary"
                  disabled={saving}
                  onClick={() => saveProfile(true, editingId || "")}
                >
                  Save password
                </button>
                <button
                  className="text-button"
                  onClick={() => setOfferSave(false)}
                >
                  Not now
                </button>
              </div>
            </div>
          )}
          {editingId && profile && (
            <button
              className="text-button delete-connection"
              disabled={saving || deleting}
              onClick={() => askDelete([{ key: profile.id, fields: connectionFields(profile), profile }])}
            >
              Delete connection
            </button>
          )}
          {status && <p role="status" className="notice connections-status">{status}</p>}
          {error && <p role="alert" className="error">{error}</p>}
          {editingId && rowTests[editingId] && rowTests[editingId].identity === connectionKey(formConnection) && (
            <p className="row-test" role={rowTests[editingId].state === "failed" ? "alert" : "status"}>
              {rowTests[editingId].state === "testing" ? "Connecting…" : (rowTests[editingId] as { text: string }).text}
            </p>
          )}
          <p className="hint">
            Connections store server settings. {storeSentence} Requires a local
            mysql or mariadb client.
          </p>
        </div>
  );
  return (
    <InspectorContext.Provider value={{ open: editingId !== null, setOpen: value => {
      if (!value) closeForm();
      else openForm(rows.find(row => row.key === cursor)?.profile, rows.find(row => row.key === cursor)?.fields);
    }, sheet, width: inspectorWidth, setWidth: setInspectorWidth }}>
      <ViewLayout id="connections" title="Connections" className="home-pane connections"
        info={<p>Double click or press Enter to use a connection. Select rows to delete them together. Last used covers this window only.</p>}
        controls={<button ref={addButtonRef} className="button primary pane-action" disabled={deleting || saving} onClick={() => openForm()}>
          <Icon name="plus" size={14} /> Add connection
        </button>}
        inspector={editingId === null ? null : { key: editingId || "new", title: editingId ? `Edit “${profile?.name || draft.name}”` : "Add connection", body: form }}>
        <div className="connections-table-toolbar">
          {(rows.length >= 6 || !!search) && <SearchField label="Search connections" value={search} onChange={setSearch} />}
          {editingId === null && !!selectedRows.length && <button className="button" disabled={deleting || saving} onClick={() => askDelete(selectedRows)}>Delete ({selectedRows.length})</button>}
          <span className="connections-count">{rows.length} {rows.length === 1 ? "connection" : "connections"}</span>
        </div>
        {loading && <p role="status">Loading connections…</p>}
        {loadError && <p role="alert" className="error">{loadError} <button className="text-button" onClick={() => void refresh().catch(e => setError((e as Error).message))}>Retry connections</button></p>}
        <div ref={tableRef} id="connections-table" tabIndex={-1} aria-label="Connections table" onKeyDown={e => {
          if ((e.target as HTMLElement).closest('input:not([type="checkbox"]), select, textarea, button, [role=menu], [role=dialog]')) return;
          if (e.key === "Delete" || e.key === "Backspace") {
            e.preventDefault();
            askDelete(selectedRows.length ? selectedRows : rows.filter(row => row.key === cursor));
          }
          if (e.target === e.currentTarget && ["ArrowDown", "ArrowUp"].includes(e.key)) {
            e.preventDefault();
            tableRef.current?.querySelector<HTMLElement>("tbody [data-row-key]")?.focus();
          }
        }}>
          <DataTable label="Connections" defaultSort={{ key: "name", dir: "asc" }} columns={columns} rows={visibleRows} rowKey={row => row.key}
            selection={{ key: cursor, select: key => setCursor(key) }} multiSelection={{ keys: selected, select: setSelected }} onActivate={activate}
            rowClass={row => `connection-row${(row.profile?.id === active.id || row.key === "active") ? " is-active" : ""}`}
            rowAttrs={row => ({ "aria-label": `${row.fields.name || connectionTarget(row.fields)}${row.profile?.id === active.id || row.key === "active" ? ", in use" : ""}` })}
            rowAction={rowMenu} compact={sheet || editingId !== null} detailColumns={["type", "endpoint", "schema", "password", "last-used"]}
            empty={loading ? undefined : <p className="empty-small">{search ? "No connections match your search." : "Add a connection to start working with a server."}</p>} />
        </div>
        {Object.entries(deleteErrors).map(([id, message]) => <p key={id} role="alert" className="error connection-delete-error">
          {profiles.find(p => p.id === id)?.name || id}: {message}
        </p>)}
        {editingId === null && status && <p role="status" className="notice connections-status">{status}</p>}
        {editingId === null && error && <p role="alert" className="error connections-status">{error}</p>}
      </ViewLayout>
      {confirmDelete && <ConfirmSheet title={confirmDelete.every(row => !row.profile) ? "Remove connection?"
        : confirmDelete.length === 1 ? "Delete connection?" : `Delete ${confirmDelete.length} connections?`}
        description="Remove these settings from this computer. Existing editor sessions are not closed."
        busy={deleting} confirmLabel={confirmDelete.every(row => !row.profile) ? "Remove connection" : confirmDelete.length === 1 ? "Delete connection" : "Delete connections"}
        onCancel={() => setConfirmDelete(null)} onConfirm={() => void remove()}>
        <ul className="connection-delete-list">{confirmDelete.map(row => <li key={row.key}>
          <strong>{row.fields.name || connectionTarget(row.fields)}</strong>
          <span>{row.profile ? row.profile.has_saved_password ? "Saved password will also be removed." : "No saved password to remove." : "Unsaved settings only; nothing on disk to delete."}</span>
          {(row.profile?.id === active.id || row.key === "active") && <span>This connection is in use. The picker will switch to No connection and its typed password will be cleared.</span>}
        </li>)}</ul>
      </ConfirmSheet>}
    </InspectorContext.Provider>
  );
}

/**
 * The compact active-connection picker in the Query editor and Server
 * headers: which connection is in use, a switch, the password when none is
 * saved, and a way to Home's Connections.
 */
export function ConnectionPicker({
  profiles,
  active,
  safety,
  onSwitch,
  onPassword,
  onManage,
  note,
}: {
  profiles: Profile[];
  active: ActiveConnection;
  /** The type and read-only flag the connection in use is judged by. */
  safety: Safety;
  onSwitch: (id: string) => void;
  onPassword: (password: string) => void;
  onManage: () => void;
  /** A one-off remark, such as a connection that no longer exists. */
  note?: string;
}) {
  const privileges = usePrivileges();
  // read_privileges returns credential-stripped grant lines at the top level.
  const grantLines = (privileges?.report as { grants?: string[] } | null)?.grants;
  const profile = profiles.find((p) => p.id === active.id);
  const saved = !!profile?.has_saved_password;
  const [override, setOverride] = useState(false);
  const passwordField = useRef<HTMLInputElement>(null);
  useEffect(() => { if (override) passwordField.current?.focus(); }, [override]);
  useEffect(() => setOverride(false), [active.id]);
  const hasConnection = !!active.fields.host;
  const showPassword = hasConnection && (!saved || override || !!active.password);
  const name = active.fields.name || connectionTarget(active.fields);
  const state = active.password
    ? "Typed password"
    : saved
      ? passwordStatus(profile)
      : "No password entered";
  return (
    <div className="connection-picker" role="group" aria-label="Connection in use">
      <Popover label={hasConnection ? `Connection in use: ${name} · ${connectionTarget(active.fields)}` : "No connection"}
        className="connection-popover" buttonClassName="connection-pill" wide
        trigger={<>
          {hasConnection && (safety.known ? <TypeDot type={safety.type} readOnly={safety.read_only} /> : <span className="status-dot is-unknown" aria-label="Policy unknown" role="img" />)}
          <span className="connection-pill-name">{name}</span>
          <span className="picker-target">{active.fields.name ? ` · ${connectionTarget(active.fields)}` : ""}</span>
          {hasConnection && safety.known && <SafetyTags type={safety.type} readOnly={safety.read_only} />}
          <span className="down-chevron"><Icon name="disclosure" size={12} /></span>
        </>}>
        <div className="picker-panel">
          <h3 className="popover-title">Connection in use</h3>
          <label className="field">Connection
            <Select label="Connection" value={active.id} onChange={onSwitch} options={[
              ...(profile ? [] : [[active.id, hasConnection ? `${name} (not saved)` : "No connection"] as [string, string]]),
              ...profiles.map((p): [string, string] => [p.id, p.name]),
            ]} />
          </label>
          {hasConnection && <>
          <p className="picker-endpoint">{connectionTarget(active.fields)}{active.fields.database ? ` · ${active.fields.database}` : ""}</p>
          <p className="hint">TLS {active.fields.ssl_mode}</p>
          {hasConnection && safety.known && <SafetyTags type={safety.type} readOnly={safety.read_only} />}
          <p className={`picker-state${saved && !active.password ? " saved" : ""}`}>{state}</p>
          {showPassword ? <label className="field">Session password
            <input ref={passwordField} type="password" className="picker-password" aria-label={`Password for ${name}`}
              placeholder={saved ? "Saved password" : "Password"} autoComplete="current-password"
              value={active.password} onChange={(e) => onPassword(e.target.value)} />
          </label> : <button className="button" onClick={() => setOverride(true)}>Use a different password</button>}
          {saved && showPassword && <button className="button" onClick={() => { onPassword(""); setOverride(false); }}>Use saved password</button>}
          </>}
          {!hasConnection && <p className="hint">Choose a connection to work with a server.</p>}
          {note && <p className="hint picker-note" role="status">{note}</p>}
          <button className="button picker-manage" onClick={() => { closePopovers(); onManage(); }}>Manage connections…</button>
        </div>
      </Popover>
      {privileges && <Popover label="Privileges" buttonClassName="button small" trigger="Privileges">
        <h3 className="popover-title">Privileges</h3>
        {privileges.report ? <>
          <p>Connected as {privileges.report.account}</p>
          <p className="hint">New sessions. An editor tab can have different active roles.</p>
          <p>Active roles: {(privileges.report.current_roles || []).map(r => r.host ? `${r.name}@${r.host}` : r.name).join(", ") || "None"}</p>
          {(grantLines || privileges.report.model) && <details><summary>Grants</summary>
            <pre className="ops-pre">{grantLines ? grantLines.join("\n") : JSON.stringify(privileges.report.model, null, 2)}</pre>
          </details>}
          <details><summary>Denied and unverified actions</summary><ul>
            {Object.entries(privileges.report.actions).filter(([, answer]) => answer.state !== "allowed").map(([action, answer]) =>
              <li key={action}><strong>{actionLabel(action)}</strong>: {privilegeResult(answer, privileges.report?.account).reason}</li>)}
          </ul></details>
        </> : <p className="hint">Unverified. Privileges have not been read for this connection.</p>}
        {privileges.error && <p role="alert" className="error">{privileges.error}</p>}
        <button type="button" className="button" disabled={privileges.loading} onClick={() => void privileges.refresh(true)}>
          {privileges.loading ? "Reading privileges…" : "Refresh privileges"}
        </button>
      </Popover>}
    </div>
  );
}
