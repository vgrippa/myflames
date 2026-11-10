// Saved connections (the API calls them profiles) and the active connection
// that the Query editor and Server use. The UI says "connection"; requests
// keep the `profiles/*` endpoints and `profile_id`.
import { lazy, Suspense, createContext, useContext, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, clientMissingCode, type Connection, type ConnectionType, type Profile, type SecretStore } from "./api";
import { ClientMissingNotice } from "./ClientMissing";
import { Icon } from "./components";
import { ReadinessFacts, checkReadiness, copyReadiness, readinessHint, readinessLine } from "./readiness";
import { connectionKey, connectionLabel, hostHasSpace, sshKey, trimPasted, trimPastedEnd } from "./binding";
import { ActionButton, ActionStatus, Checkbox, ConfirmSheet, DataTable, InfoPopover, InspectorContext, Popover, closePopovers,
  SearchField, Segmented, Select, ViewLayout, useFocusScope, type Column } from "./kit";
import { actionLabel, privilegeResult, usePrivileges } from "./privileges";
import { defaultConnection, relativeTime, SSL_MODES, type SessionConnection } from "./session";

const ImportConnections = lazy(() => import("./ImportConnections").then(module => ({ default: module.ImportConnections })));

/** connection/test: the version, the product named in @@version_comment, and the bounded database list. */
type ConnectionTestResult = {
  version: string;
  engine?: string;
  flavor?: string;
  product?: string;
  schemas?: string[];
  databases?: string[];
  databases_truncated?: boolean;
  databases_error?: string;
  /** "Through SSH host bastion" for a test that went through an SSH tunnel. */
  through_ssh?: string;
};
const SYSTEM_DATABASES = new Set(["mysql", "sys", "performance_schema", "information_schema"]);
const TYPE_DATABASE_OPTION = "__type_database__";
const SHOW_SYSTEM_DATABASES_OPTION = "__show_system_databases__";
const HIDE_SYSTEM_DATABASES_OPTION = "__hide_system_databases__";
function isSystemDatabase(name: string) {
  return SYSTEM_DATABASES.has(name.trim().toLowerCase());
}
/** "8.4.11", "11.8.9-MariaDB-log" (they name themselves), "Percona Server 8.4.11-11". */
function testedServer(r: ConnectionTestResult) {
  return r.flavor === "percona" && r.product ? `${r.product} ${r.version}` : r.version;
}
/** "Connected · 8.4.11", with " · Through SSH host bastion" through SSH. */
function testedText(r: ConnectionTestResult) {
  return `Connected · ${testedServer(r)}${r.through_ssh ? ` · ${r.through_ssh}` : ""}`;
}

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
/** The non-secret fields of a connection or saved connection, in session
 * order. Spaces pasted around the host or CA path, or after the database,
 * are dropped as the server drops them (live.connection_options). */
export function connectionFields(c: Partial<Connection>): SessionConnection {
  return {
    name: c.name || "",
    host: trimPasted(c.host || ""),
    port: c.port || 3306,
    user: c.user || "",
    database: trimPastedEnd(c.database || ""),
    ssl_mode: c.ssl_mode || "PREFERRED",
    ssl_ca: trimPasted(c.ssl_ca || ""),
    read_only: c.read_only === true,
    ...sshFields(c),
  };
}
/** The form's settings as they are sent: the same trimming as connectionFields.
 * SSH user and port without an SSH host are sent too, so the server says what is missing. */
function tidy(c: Connection): Connection {
  const { ssh_host: _host, ssh_user: _user, ssh_port: _port, ...rest } = c;
  const user = trimPasted(c.ssh_user || ""), port = Number(c.ssh_port) || 0;
  return { ...rest, host: trimPasted(c.host || ""), database: trimPastedEnd(c.database || ""), ssl_ca: trimPasted(c.ssl_ca || ""),
    ...sshFields(c), ...(user ? { ssh_user: user } : {}), ...(port ? { ssh_port: port } : {}) };
}
/**
 * What the user meant by the text in the Host field: a host name or address
 * without the spaces a paste brings along, or the parts of a pasted
 * "host:port", "[IPv6]:port" or mysql:// / mariadb:// URL. Null when there is
 * nothing to change.
 */
export function splitHost(text: string): { host: string; port?: number; user?: string; password?: string; database?: string } | null {
  const value = trimPasted(text);
  const url = /^(mysql|mariadb|mysqlx):\/\//i.test(value) ? (() => { try { return new URL(value); } catch { return null; } })() : null;
  if (url) {
    const decode = (s: string) => { try { return decodeURIComponent(s); } catch { return s; } };
    const host = url.hostname.replace(/^\[(.*)\]$/, "$1");
    if (!host) return null;
    const database = decode(url.pathname.replace(/^\//, ""));
    return {
      host,
      ...(url.port ? { port: Number(url.port) } : {}),
      ...(url.username ? { user: decode(url.username) } : {}),
      ...(url.password ? { password: decode(url.password) } : {}),
      ...(database ? { database } : {}),
    };
  }
  // One colon only: an IPv6 address has several and keeps them.
  const pair = value.match(/^\[([0-9A-Fa-f:.]+(?:%[\w.-]+)?)\]:(\d{1,5})$/) || value.match(/^([^:\s[\]]+):(\d{1,5})$/);
  if (pair && Number(pair[2]) >= 1 && Number(pair[2]) <= 65535) return { host: pair[1], port: Number(pair[2]) };
  const bracketed = value.match(/^\[([0-9A-Fa-f:.]+(?:%[\w.-]+)?)\]$/);
  if (bracketed) return { host: bracketed[1] };
  return value === text ? null : { host: value };
}
/** "analyst@127.0.0.1:3306", or "analyst@127.0.0.1:3306 via bastion" through SSH. */
export function connectionTarget(c: Pick<Connection, "user" | "host" | "port"> & Partial<Pick<Connection, "ssh_host">>) {
  if (!c.host) return "No connection";
  const via = trimPasted(c.ssh_host || "");
  return `${c.user ? `${c.user}@` : ""}${c.host}:${c.port}${via ? ` via ${via}` : ""}`;
}
/** Default name for a new connection: user@host (plus :port when port is not 3306). */
export function defaultConnectionName(user: string, host: string, port: number): string {
  const split = splitHost(host || "");
  const h = split ? split.host : trimPasted(host || "");
  const u = (split?.user ?? user ?? "").trim();
  const p = split?.port ?? Number(port);
  if (!h) return u;
  const portSuffix = Number.isInteger(p) && p >= 1 && p <= 65535 && p !== 3306 ? `:${p}` : "";
  return `${u ? `${u}@` : ""}${h}${portSuffix}`;
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
/** Host, port, user, TLS and SSH settings, compared like workspace_store.ENDPOINT_FIELDS. */
function endpoint(c: Partial<Connection>) {
  const base = [trimPasted(c.host || "") || "127.0.0.1", Number(c.port) || 3306, c.user || "", c.ssl_mode || "", trimPasted(c.ssl_ca || "")];
  const ssh = sshKey(c);
  return JSON.stringify(ssh ? [...base, ssh] : base);
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
/** The small dot that names a connection's type (purple for Production, teal for Development). */
export function TypeDot({ type, readOnly }: { type: ConnectionType; readOnly?: boolean }) {
  const label = readOnly ? `${TYPE_LABEL[type]}, read-only` : TYPE_LABEL[type];
  return <span className={`type-dot is-${readOnly ? "read-only" : type}`} role="img" aria-label={label} title={label} />;
}
/** Type and Read-only as two compact pills in a connection row or the Audit
 * log. They never merge into one truncating string: a read-only Development
 * connection keeps its teal pill and the Read-only outline beside it. */
export function SafetyTags({ type, readOnly }: { type: ConnectionType; readOnly: boolean }) {
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
/** The write safety of the connection in use, first in the window footer of
 * the places that send to it (Query editor, Server, Workload, Replication,
 * Tuning): Production asks before every change, Read-only refuses them,
 * Development asks only before risky ones. While the policy is not known it
 * assumes Production and says why (`unknown`: "loading" or the error text),
 * with Retry after a failure. A dot plus words, never color alone; the full
 * sentence is its accessible name and tooltip. */
export function SafetyStatus({
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
  const loading = unknown === "loading";
  const [state, word, detail, sentence] = !safety.known
    ? [
        "unknown",
        "Production assumed",
        loading ? "reading settings" : "settings not loaded",
        loading
          ? `Production assumed: reading the settings of ${name}; changes stay off until they load`
          : `Production assumed: could not read the settings of ${name}${unknown ? ` (${unknown})` : ""}; changes stay off until they load`,
      ]
    : safety.read_only
      ? ["read-only", "Read-only", "changes refused", `Read-only: myflames refuses every change on ${name}`]
      : safety.type === "production"
        ? ["production", "Production", "changes ask first", `Production: every change on ${name} asks first`]
        : ["development", "Development", "risky changes ask first", `Development: changes on ${name} ask first only when risky`];
  return (
    <span className={`safety-status is-${state}`} data-status="safety" data-safety={state} title={sentence}>
      <span className="safety-status-dot" aria-hidden="true" />
      <span className="safety-status-text" role="note" aria-label={sentence}>
        <strong>{word}</strong>{" "}
        <span className="safety-status-detail" aria-hidden="true">· {detail}</span>
      </span>
      {!safety.known && !loading && onRetry && (
        <button type="button" className="safety-retry" onClick={onRetry} aria-label={`Retry reading the settings of ${name}`}>
          Retry
        </button>
      )}
    </span>
  );
}

/** A thin decorative strip along the top edge of the window on places that send
 * to the connection in use. Hidden from assistive technology; the type word
 * stays in the window footer. */
export function ConnectionTypeStrip({ safety }: { safety: Safety }) {
  const state = !safety.known ? "production" : safety.read_only ? "read-only" : safety.type;
  return <div className={`connection-type-strip is-${state}`} data-connection-strip={state} aria-hidden="true" />;
}

/** The band at the top of every write review sheet: names the connection's
 * type in words and shows the reviewed connection target, colored with the
 * same token as the top-edge strip. The target is connectionLabel's, so a
 * connection through SSH reads "Prod (app@127.0.0.1:3306 via bastion)": the
 * database host alone would name another server. */
export function ConnectionTypeBand({
  connection,
  label,
  type,
  readOnly,
}: {
  connection?: {
    name?: string | null;
    host?: string | null;
    port?: number | null;
    user?: string | null;
    ssl_mode?: string | null;
    ssh_host?: string | null;
    type?: ConnectionType | string | null;
    read_only?: boolean | null;
  } | null;
  label?: string;
  type?: string;
  readOnly?: boolean;
}) {
  const rawType = type || connection?.type;
  const resolvedType: ConnectionType = rawType === "development" ? "development" : "production";
  const isReadOnly = readOnly ?? connection?.read_only === true;
  const state = isReadOnly ? "read-only" : resolvedType;
  const typeWord = isReadOnly ? `Read-only (${TYPE_LABEL[resolvedType]})` : TYPE_LABEL[resolvedType];
  const full = connection?.host
    ? {
        name: connection.name || undefined,
        host: connection.host,
        port: connection.port || 3306,
        user: connection.user || "",
        ssl_mode: connection.ssl_mode || "",
        ssh_host: connection.ssh_host || "",
      }
    : null;
  const targetText = full
    ? full.name
      ? connectionLabel(full)
      : label || connectionLabel(full)
    : label || "No connection";
  return (
    <div className={`connection-type-band is-${state}`} data-connection-type={state}>
      <span className="connection-type-band-dot" aria-hidden="true" />
      <strong>{typeWord}</strong>
      <span className="connection-type-band-sep" aria-hidden="true">·</span>
      <span className="connection-type-band-target" title={targetText}>{targetText}</span>
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

type RowTest = { identity: string; seq: number } & ({ state: "testing" } | { state: "ok" | "failed"; text: string; /** The refusal code when the test was refused for a client problem ("client-missing" or "client-auth-plugin"). */ missing?: string });

/**
 * The Connections section of Home: saved connections as rows, and a form to
 * add or edit one. A duplicate name is refused inline; Save connection asks
 * about the typed password; a successful Test connection offers to save it.
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
  addRequest = 0,
  onAddHandled,
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
  /** Above zero: New connection… was chosen elsewhere (picker, palette); open
   * the Add connection form, then call onAddHandled so it opens only once. */
  addRequest?: number;
  onAddHandled?: () => void;
}) {
  // null: the form is closed. "": a new connection. Otherwise the id being edited.
  const [importingConnections, setImportingConnections] = useState(false);
  const [readinessNote, setReadinessNote] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Connection>(blank);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [schemasError, setSchemasError] = useState("");
  const [showSystemSchemas, setShowSystemSchemas] = useState(false);
  const [typingSchema, setTypingSchema] = useState(false);
  const schemaInputRef = useRef<HTMLInputElement>(null);
  const nameEditedRef = useRef(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  /** The shown error was a refused read for a missing database client. */
  const [errorMissing, setErrorMissing] = useState("");
  const [testing, setTesting] = useState(false);
  const [testSince, setTestSince] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [forgetting, setForgetting] = useState(false);
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
  // Save connection asks whether to keep the typed password. `target`
  // undefined = the connection being edited (or a new one); "" = new.
  const [savePrompt, setSavePrompt] = useState<null | {
    askPassword: boolean;
    target?: string;
  }>(null);
  const savePromptRef = useRef<HTMLDivElement>(null);
  useFocusScope(savePromptRef, { open: !!savePrompt, onClose: () => setSavePrompt(null) });
  // After a successful connection test with a typed password, offer to save it.
  const [offerSave, setOfferSave] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<ConnectionRow[] | null>(null);
  const [deleting, setDeleting] = useState(false);
  // The Delete sheet was closed while the deletion had no reply yet.
  const [deleteLeft, setDeleteLeft] = useState(false);
  const deletingRef = useRef(false);
  const [deletedIds, setDeletedIds] = useState<string[]>([]);
  const [deleteErrors, setDeleteErrors] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<string[]>([]);
  const [cursor, setCursor] = useState("");
  const [search, setSearch] = useState("");
  const [sheet, setSheet] = useState(() => typeof window !== "undefined" && window.innerWidth < 1040);
  const [phone, setPhone] = useState(() => typeof window !== "undefined" && window.innerWidth <= 650);
  const [inspectorWidth, setInspectorWidth] = useState(340);
  const tableRef = useRef<HTMLDivElement>(null);
  const addButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 1039px)");
    const change = () => setSheet(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 650px)");
    const change = () => setPhone(media.matches);
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
    ? { ...tidy(draft), profile_id: editingId }
    : tidy(draft);
  const hostProblem = hostHasSpace(draft.host || "") ? "A host name or IP address cannot contain spaces." : "";
  const portProblem = Number.isInteger(draft.port) && draft.port >= 1 && draft.port <= 65535 ? "" : "Enter a port from 1 to 65535.";
  const trimmedName = (draft.name || "").trim();
  const duplicateProfile = trimmedName
    ? profiles.find(
        (p) =>
          p.id !== editingId &&
          !deletedIds.includes(p.id) &&
          (p.name || "").trim().toLowerCase() === trimmedName.toLowerCase(),
      )
    : undefined;
  const nameProblem = duplicateProfile
    ? `A saved connection named ${(duplicateProfile.name || "").trim()} already exists.`
    : "";
  const userSchemas = schemas.filter((s) => !isSystemDatabase(s));
  const systemSchemas = schemas.filter((s) => isSystemDatabase(s));
  const visibleSchemas = showSystemSchemas ? [...userSchemas, ...systemSchemas] : userSchemas;
  const showSchemaSelect = schemas.length > 0 && !typingSchema && !schemasError;
  const canRemember = !!secretStore?.available;
  const movedFromDevelopment = !!devFor && devFor.key !== endpoint(draft);
  const shownType: ConnectionType = devFor && !movedFromDevelopment ? "development" : "production";
  /** Development for the endpoint of *c* (null: Production). */
  const developmentFor = (c: Partial<Connection>) => ({
    key: endpoint(c),
    label: `${connectionTarget({ user: c.user || "", host: c.host || "", port: c.port || 3306, ssh_host: c.ssh_host })}${c.ssl_mode ? ` (TLS ${c.ssl_mode})` : ""}`,
  });

  function openForm(p?: Profile, fields?: SessionConnection) {
    statusFromTest.current = false;
    formGen.current++;
    draftGen.current++;
    const source = p || fields;
    nameEditedRef.current = !!source;
    const key = p?.id || (fields === active.fields ? "active" : "");
    setCursor(key);
    setSelected(key ? [key] : []);
    setEditingId(p ? p.id : "");
    setDevFor(p?.type === "development" ? developmentFor(p) : null);
    setDraft(
      source
        ? { ...connectionFields(source), password: fields === active.fields ? active.password : "", type: p?.type === "development" ? "development" : "production" }
        : (() => { const b = blank(); return { ...b, name: defaultConnectionName(b.user || "", b.host || "", b.port || 3306) }; })(),
    );
    setSavePrompt(null);
    setOfferSave(false);
    setSchemas([]);
    setReadinessNote("");
    setSchemasError("");
    setShowSystemSchemas(false);
    setTypingSchema(false);
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
  // New connection… from the picker or the palette.
  useEffect(() => {
    if (!addRequest) return;
    openForm();
    onAddHandled?.();
  }, [addRequest]);
  function chooseType(type: ConnectionType) {
    setDevFor(type === "development" ? developmentFor(draft) : null);
  }
  /** Leaving Host: drop pasted spaces and move a pasted port, user, password
   * or database (host:port, [IPv6]:port, a mysql:// URL) to their fields. */
  function tidyHost() {
    const parts = splitHost(draft.host || "");
    if (!parts) return;
    const moved: string[] = [];
    edit("host", parts.host);
    if (parts.port !== undefined && parts.port !== draft.port) { edit("port", parts.port); moved.push("port"); }
    if (parts.user !== undefined) { edit("user", parts.user); moved.push("user"); }
    if (parts.password !== undefined) { edit("password", parts.password); moved.push("password"); }
    if (parts.database !== undefined) { edit("database", parts.database); moved.push("schema"); }
    if (moved.length) {
      statusFromTest.current = false;
      setStatus(`Moved the ${moved.join(", ").replace(/, ([^,]*)$/, " and $1")} from Host to ${moved.length === 1 ? "its field" : "their fields"}.`);
    }
  }
  function edit<K extends keyof Connection>(key: K, value: Connection[K]) {
    setReadinessNote("");
    if (key === "name") nameEditedRef.current = true;
    setDraft((c) => {
      const next = { ...c, [key]: value };
      if (!editingId && !nameEditedRef.current && (key === "host" || key === "user" || key === "port")) {
        next.name = defaultConnectionName(next.user || "", next.host || "", next.port || 3306);
      }
      return next;
    });
    // Type and Read-only do not change what a test connects to.
    if (key === "type" || key === "read_only") return;
    if (key !== "name" && key !== "database") {
      setSchemas([]);
      setSchemasError("");
      setTypingSchema(false);
    }
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
    const { password, ...rest } = tidy(draft);
    const resolvedName = (rest.name || "").trim() || defaultConnectionName(rest.user || "", rest.host || "", rest.port || 3306);
    // The type is always sent: the one on screen (a moved Development
    // connection shows and saves Production until Development is chosen again).
    const settings = { ...rest, name: resolvedName, type: shownType, read_only: draft.read_only === true };
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
      copyReadiness(formConnection, { ...settings, profile_id: p.id });
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
      setErrorMissing("");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }
  /** Save connection: refuse a duplicate name, then ask whether to keep the
   * typed password, so the choice is never a checkbox that is easy to miss. */
  function startSave() {
    setError("");
    setOfferSave(false);
    if (nameProblem || hostProblem || portProblem) {
      if (nameProblem) nameField.current?.focus();
      return;
    }
    const askPassword = !!draft.password && canRemember;
    if (askPassword) setSavePrompt({ askPassword });
    else void saveProfile(false);
  }
  async function testForm() {
    // The answer belongs to these settings: it is dropped once the fields
    // change or another form opens, and a newer test replaces it.
    const gen = draftGen.current;
    const seq = ++testSeq.current;
    const tested = { editing: editingId, password: draft.password };
    setTesting(true);
    setTestSince(Date.now());
    setError("");
    // The result line belongs to this test from now on.
    if (statusFromTest.current) { statusFromTest.current = false; setStatus(""); }
    try {
      const [test, readiness] = await Promise.allSettled([
        api<ConnectionTestResult>("connection/test", { connection: formConnection }),
        checkReadiness(formConnection),
      ]);
      if (test.status === "rejected") throw test.reason;
      const r = test.value;
      if (draftGen.current !== gen || testSeq.current !== seq) return;
      const dbs = Array.isArray(r.databases) ? r.databases : Array.isArray(r.schemas) ? r.schemas : [];
      setSchemas(dbs);
      setSchemasError(r.databases_error || "");
      setTypingSchema(false);
      setShowSystemSchemas(!!draft.database && isSystemDatabase(draft.database));
      statusFromTest.current = true;
      setStatus(readiness.status === "fulfilled" ? `Connected · ${readinessLine(readiness.value)}` : testedText(r));
      setReadinessNote(readiness.status === "fulfilled" ? readinessHint(readiness.value) : "Readiness could not be checked; try Check now.");
      if (tested.editing && !savedPassword && tested.password && canRemember)
        setOfferSave(true);
    } catch (e) {
      if (draftGen.current === gen && testSeq.current === seq) {
        setError((e as Error).message);
        setErrorMissing(clientMissingCode(e));
      }
    } finally {
      if (testSeq.current === seq) setTesting(false);
    }
  }
  /** Stop waiting for the test: its answer is dropped (the server ends the
   * attempt at its own connect timeout). */
  function stopTest() {
    testSeq.current++;
    setTesting(false);
    setStatus("Stopped waiting for the test. Nothing was saved or changed.");
    statusFromTest.current = true;
  }
  async function forget(id: string) {
    if (forgetting) return;
    setError("");
    setForgetting(true);
    try {
      await api("profiles/forget-password", { id });
      await refresh();
      setStatus("Saved password forgotten. Enter the password to connect.");
    } catch (e) {
      setError(`Could not forget the saved password: ${(e as Error).message}`);
      setErrorMissing("");
    } finally {
      setForgetting(false);
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
    catch (e) { setError(`Could not refresh connections: ${(e as Error).message}`); setErrorMissing(""); }
    finally {
      deletingRef.current = false;
      setDeleting(false);
      setConfirmDelete(null);
      setDeleteLeft(false);
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
      const [test] = await Promise.allSettled([
        api<ConnectionTestResult>("connection/test", { connection }), checkReadiness(connection),
      ]);
      if (test.status === "rejected") throw test.reason;
      const r = test.value;
      put({ state: "ok", text: testedText(r), identity, seq });
    } catch (e) {
      put({ state: "failed", text: (e as Error).message, identity, seq, missing: clientMissingCode(e) || undefined });
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
    { key: "name", label: "Name", primary: true, flex: true, width: 108, minWidth: 96, sort: row => row.fields.name,
      render: row => <span className="cell-name">
        <TypeDot type={row.profile?.type || "production"} readOnly={row.fields.read_only} />
        <strong className="cell-truncate" title={row.fields.name || connectionTarget(row.fields)}>{row.fields.name || connectionTarget(row.fields)}</strong>
        <span className={`connection-type-short${row.fields.read_only ? " is-read-only" : ""}`} aria-hidden="true">
          {row.profile?.type === "development" ? "Dev" : "Prod"}
        </span>
      </span> },
    // No declared width: the badge (Production, Development, Read-only) sets it.
    { key: "type", label: "Type", sort: row => row.profile?.type || "production", render: row => (
      <SafetyTags type={row.profile?.type || "production"} readOnly={row.fields.read_only} />
    ) },
    { key: "endpoint", label: "user@host:port", width: 112, minWidth: 92, sort: row => connectionTarget(row.fields),
      render: row => <span className="cell-truncate" title={connectionTarget(row.fields)}>{connectionTarget(row.fields)}</span> },
    { key: "schema", label: "Database", title: "Default database", width: 94, sort: row => row.fields.database,
      render: row => row.fields.database
        ? <span className="cell-truncate" title={row.fields.database}>{row.fields.database}</span>
        : <span className="cell-truncate cell-empty" title="No default database">—<span className="visually-hidden">No default database</span></span> },
    { key: "server", label: "Server", width: 112, minWidth: 112,
      render: row => <ReadinessFacts connection={{ ...row.fields, profile_id: row.profile?.id, type: row.profile?.type }} /> },
    { key: "password", label: "Password", width: 80, sort: row => row.profile?.has_saved_password ? 1 : 0,
      render: row => <span className="cell-truncate" title={passwordStatus(row.profile)}>{row.profile?.has_saved_password ? "Saved" : "Not saved"}</span> },
    { key: "last-used", label: "Last used", width: 80, title: "Usage observed during this window; earlier use is not recorded.",
      sort: row => lastUsed.get(row.key) || "", render: row => row.profile?.id === active.id || row.key === "active" ? <span title="In use">In use</span>
        : lastUsed.has(row.key) ? <time className="cell-truncate" title={new Date(lastUsed.get(row.key)!).toLocaleString()} dateTime={lastUsed.get(row.key)}>{relativeTime(lastUsed.get(row.key)!)}</time>
          : <span className="cell-empty" title="Not used in this window. Earlier use is not recorded.">—<span className="visually-hidden">Not used in this window</span></span> },
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
            void checkReadiness({ ...row.fields, type: row.profile?.type, profile_id: row.key,
              password: row.key === active.id && sameEndpoint(row.fields, active.fields) ? active.password : "" }).catch(() => {});
          })} aria-label={`Check now ${name}`}>Check now</button>
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
          {editingId && !statusFromTest.current && <ReadinessFacts connection={formConnection} details />}
          <div className="connection-fields">
            <label className="field">
              Connection name
              <input
                ref={nameField}
                value={draft.name}
                aria-invalid={nameProblem ? true : undefined}
                aria-describedby={nameProblem ? "connection-name-problem" : undefined}
                onChange={(e) => edit("name", e.target.value)}
              />
              {nameProblem && <span className="form-error" id="connection-name-problem" role="alert">{nameProblem}</span>}
            </label>
            <label className="field">
              Host
              <input
                value={draft.host}
                spellCheck={false}
                autoCapitalize="off"
                autoCorrect="off"
                aria-invalid={hostProblem ? true : undefined}
                aria-describedby={hostProblem ? "connection-host-problem" : undefined}
                onChange={(e) => edit("host", e.target.value)}
                onBlur={tidyHost}
              />
              {hostProblem && <span className="form-error" id="connection-host-problem" role="alert">{hostProblem}</span>}
            </label>
            <div className="field-pair">
              <label className="field">
                Port
                <input
                  type="number"
                  min="1"
                  max="65535"
                  value={draft.port || ""}
                  aria-invalid={portProblem ? true : undefined}
                  aria-describedby={portProblem ? "connection-port-problem" : undefined}
                  onChange={(e) => edit("port", e.target.value === "" ? 0 : +e.target.value)}
                />
                {portProblem && <span className="form-error" id="connection-port-problem" role="alert">{portProblem}</span>}
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
                    aria-busy={forgetting || undefined}
                    aria-disabled={forgetting || undefined}
                    onClick={() => { if (editingId && !forgetting) void forget(editingId); }}
                  >
                    {forgetting ? "Forgetting…" : "Forget saved password"}
                  </button>
                </div>
              )}
            </div>
            <label className="field">
              Default database
              {showSchemaSelect ? (
                <Select
                  label="Default database"
                  value={draft.database}
                  options={[
                    ["", "None"],
                    ...(draft.database && !visibleSchemas.includes(draft.database)
                      ? [[draft.database, draft.database] as [string, string]]
                      : []),
                    ...visibleSchemas.map((s): [string, string] => [s, s]),
                    ...(systemSchemas.length > 0
                      ? [
                          (showSystemSchemas
                            ? [HIDE_SYSTEM_DATABASES_OPTION, "Hide system databases"]
                            : [SHOW_SYSTEM_DATABASES_OPTION, "Show system databases"]) as [string, string],
                        ]
                      : []),
                    [TYPE_DATABASE_OPTION, "Type a name…"],
                  ]}
                  onChange={(v) => {
                    if (v === SHOW_SYSTEM_DATABASES_OPTION) {
                      setShowSystemSchemas(true);
                      return;
                    }
                    if (v === HIDE_SYSTEM_DATABASES_OPTION) {
                      setShowSystemSchemas(false);
                      return;
                    }
                    if (v === TYPE_DATABASE_OPTION) {
                      setTypingSchema(true);
                      window.setTimeout(() => schemaInputRef.current?.focus());
                      return;
                    }
                    edit("database", v);
                  }}
                />
              ) : (
                <input
                  ref={schemaInputRef}
                  aria-label="Default database"
                  list="schema-list"
                  value={draft.database}
                  aria-describedby={schemasError ? "connection-schema-hint" : undefined}
                  onChange={(e) => edit("database", e.target.value)}
                />
              )}
              <datalist id="schema-list">
                {visibleSchemas.map((s) => (
                  <option key={s} value={s} />
                ))}
              </datalist>
              {schemasError && (
                <span className="hint" id="connection-schema-hint">
                  Could not read the database list ({schemasError}). Type a database name instead.
                </span>
              )}
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
          <SshSettings draft={draft} edit={edit} formKey={formGen.current} />
          <div className="button-row">
            {/* Busy buttons keep focus (aria-disabled): a spinner, a
                present-tense label, and a second click sends nothing. */}
            <ActionButton
              busy={testing}
              busyLabel="Testing…"
              disabled={!testing && (busy || deleting || saving)}
              onClick={() => void testForm()}
            >
              Test connection
            </ActionButton>
            <ActionButton
              primary
              busy={saving}
              busyLabel="Saving…"
              disabled={!saving && (busy || !!savePrompt || deleting || !!nameProblem)}
              onClick={startSave}
            >
              Save connection
            </ActionButton>
            <button
              className="button"
              disabled={saving}
              onClick={closeForm}
            >
              Cancel
            </button>
            <button
              className="button"
              onClick={() => {
                // The server refuses these too (Test, Save); Use must not
                // quietly connect to another port or host instead.
                const ssh = sshProblem(draft);
                if (hostProblem || portProblem || ssh) { setError(hostProblem || portProblem || ssh); setErrorMissing(""); return; }
                const resolvedName = (draft.name || "").trim() || defaultConnectionName(draft.user || "", draft.host || "", draft.port || 3306);
                use(editingId || "", connectionFields({ ...draft, name: resolvedName }), draft.password || "");
              }}
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
              className="text-button danger delete-connection"
              disabled={saving || deleting}
              onClick={() => askDelete([{ key: profile.id, fields: connectionFields(profile), profile }])}
            >
              Delete connection
            </button>
          )}
          {/* A test running from the form: the seconds, and Stop waiting when slow. */}
          <ActionStatus
            className="connections-progress"
            state={{ phase: testing ? "pending" : "idle", message: `Testing the connection to ${connectionTarget({ user: draft.user || "", host: draft.host || "", port: draft.port || 3306 })}…`, startedAt: testSince }}
            onStop={stopTest}
            stopLabel="Stop waiting"
          />
          {/* Always mounted: what Test, Save and Forget password did is announced. */}
          <div className="connections-live" role="status" aria-live="polite">
            {status && <p className="notice connections-status"><bdi>{status}</bdi></p>}
            {readinessNote && <p className="hint"><bdi>{readinessNote}</bdi></p>}
            {editingId && rowTests[editingId] && rowTests[editingId].identity === connectionKey(formConnection) && rowTests[editingId].state !== "failed" && (
              <p className="row-test">
                {rowTests[editingId].state === "testing"
                  ? <><span className="spinner" aria-hidden="true" /> Testing the connection…</>
                  : (rowTests[editingId] as { text: string }).text}
              </p>
            )}
          </div>
          {error && (errorMissing ? (
            <ClientMissingNotice text={errorMissing === "client-auth-plugin" ? error : ""} onRetry={() => void testForm()} />
          ) : (
            <p role="alert" className="error">{error}</p>
          ))}
          {editingId && rowTests[editingId] && rowTests[editingId].identity === connectionKey(formConnection) && rowTests[editingId].state === "failed" && (
            (rowTests[editingId] as { missing?: string }).missing ? (
              <ClientMissingNotice text={(rowTests[editingId] as { missing?: string }).missing === "client-auth-plugin" ? (rowTests[editingId] as { text: string }).text : ""} onRetry={() => {
                if (profile) void testRow(editingId, { ...connectionFields(profile), type: profile.type, profile_id: profile.id,
                  password: editingId === active.id && sameEndpoint(connectionFields(profile), active.fields) ? active.password : "" });
              }} />
            ) : (
              <p className="row-test" role="alert">{(rowTests[editingId] as { text: string }).text}</p>
            )
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
        controls={<span className="connections-header-actions"><button ref={addButtonRef} className="button primary pane-action" disabled={deleting || saving} onClick={() => openForm()}>
          <Icon name="plus" size={14} /> Add connection
        </button><Popover menu label="Connections menu" buttonClassName="icon-button" trigger={<Icon name="more" />}>
          <div className="connection-menu"><button role="menuitem" onClick={() => { closePopovers(); setImportingConnections(true); }}>
            Import from MySQL Workbench or DBeaver…
          </button></div>
        </Popover></span>}
        inspector={editingId === null ? null : { key: editingId || "new", title: editingId ? `Edit “${profile?.name || draft.name}”` : "Add connection", body: form }}>
        <div className="connections-table-toolbar">
          {(rows.length >= 6 || !!search) && <SearchField label="Search connections" value={search} onChange={setSearch} />}
          {editingId === null && !!selectedRows.length && <button className="button" disabled={deleting || saving} onClick={() => askDelete(selectedRows)}>Delete ({selectedRows.length})</button>}
          <span className="connections-count">{rows.length} {rows.length === 1 ? "connection" : "connections"}</span>
        </div>
        {loading && <p role="status">Loading connections…</p>}
        {loadError && <p role="alert" className="error">{loadError} <button className="text-button" onClick={() => void refresh().catch(e => { setError((e as Error).message); setErrorMissing(""); })}>Retry connections</button></p>}
        <div ref={tableRef} id="connections-table" tabIndex={-1} aria-label="Connections table" data-compact={phone || editingId !== null || undefined} onKeyDown={e => {
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
            rowAction={rowMenu} compact={sheet || editingId !== null} detailColumns={phone || editingId !== null ? ["type", "endpoint", "schema", "server", "password", "last-used"] : ["schema", "server", "password", "last-used"]}
            empty={loading ? undefined : <p className="empty-small">{search ? "No connections match your search." : "Add a connection to start working with a server."}</p>} />
        </div>
        {Object.entries(deleteErrors).map(([id, message]) => <p key={id} role="alert" className="error connection-delete-error">
          {profiles.find(p => p.id === id)?.name || id}: {message}
        </p>)}
        {/* Always mounted: what a deletion (or Use) did is announced. */}
        <ActionStatus className="connections-progress" state={{ phase: deleting && deleteLeft ? "pending" : "idle",
          message: "Deleting the chosen connections…", startedAt: null }} />
        <div className="connections-live" role="status" aria-live="polite">
          {editingId === null && status && <p className="notice connections-status">{status}</p>}
        </div>
        {editingId === null && error && <p role="alert" className="error connections-status">{error}</p>}
      </ViewLayout>
      {importingConnections && <Suspense fallback={<p role="status">Loading import…</p>}><ImportConnections profiles={profiles} refresh={refresh} onClose={() => setImportingConnections(false)} /></Suspense>}
      {confirmDelete && !deleteLeft && <ConfirmSheet title={confirmDelete.every(row => !row.profile) ? "Remove connection?"
        : confirmDelete.length === 1 ? "Delete connection?" : `Delete ${confirmDelete.length} connections?`}
        description="Remove these settings from this computer. Existing editor sessions are not closed."
        busy={deleting} busyLabel={confirmDelete.every(row => !row.profile) ? "Removing…" : "Deleting…"}
        progress="Removing from this computer"
        onLeave={() => setDeleteLeft(true)} leaveText="Still deleting. Close does not stop it; the list updates when it ends."
        confirmLabel={confirmDelete.every(row => !row.profile) ? "Remove connection" : confirmDelete.length === 1 ? "Delete connection" : "Delete connections"}
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
  onPasswordCommit,
  onManage,
  onAdd,
  onAccount,
  note,
}: {
  profiles: Profile[];
  active: ActiveConnection;
  /** The type and read-only flag the connection in use is judged by. */
  safety: Safety;
  onSwitch: (id: string) => void;
  onPassword: (password: string) => void;
  onPasswordCommit?: () => void;
  onManage: () => void;
  /** New connection…: Home's Add connection form. */
  onAdd?: () => void;
  /** Opens Account and privileges for the connection in use. */
  onAccount?: () => void;
  /** A one-off remark, such as a connection that no longer exists. */
  note?: string;
}) {
  const privileges = usePrivileges();
  const profile = profiles.find((p) => p.id === active.id);
  const saved = !!profile?.has_saved_password;
  const [override, setOverride] = useState(false);
  const passwordField = useRef<HTMLInputElement>(null);
  const committedPassword = useRef(active.password);
  useEffect(() => { committedPassword.current = active.password; }, [active.id]);
  function commitPassword(password = active.password) {
    if (password === committedPassword.current) return;
    committedPassword.current = password;
    onPasswordCommit?.();
  }
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
      <Popover label={hasConnection ? `Connection in use: ${name} · ${connectionTarget(active.fields)} · ${safety.known ? `${TYPE_LABEL[safety.type]}${safety.read_only ? " · Read-only" : ""}` : "Production assumed · settings not loaded"}` : "No connection"}
        className="connection-popover" buttonClassName="connection-pill" wide
        trigger={<>
          {hasConnection && (safety.known ? <TypeDot type={safety.type} readOnly={safety.read_only} /> : <span className="status-dot is-unknown" aria-label="Policy unknown" role="img" />)}
          <span className="connection-pill-name">{name}</span>
          <span className="picker-target">{active.fields.name ? ` · ${connectionTarget(active.fields)}` : ""}</span>
          {hasConnection && safety.known && <SafetyTags type={safety.type} readOnly={safety.read_only} />}
          {/* Where the pills yield to width, a short type word stays beside the
              dot (the Popover label names the type in full). */}
          {hasConnection && safety.known && (
            <span className={`connection-pill-type is-${safety.type}${safety.read_only ? " is-read-only" : ""}`} aria-hidden="true">
              {safety.type === "development" ? "Dev" : "Prod"}
            </span>
          )}
          <span className="down-chevron"><Icon name="disclosure" size={12} /></span>
        </>}>
        <div className="picker-panel">
          <h3 className="popover-title">Connection in use</h3>
          <label className="field">Connection
            <Select label="Connection" value={active.id} onChange={id => {
              onSwitch(id);
              const chosen = profiles.find(p => p.id === id);
              if (chosen) void checkReadiness({ ...chosen, profile_id: chosen.id,
                password: id === active.id ? active.password : "" }).catch(() => {});
            }} options={[
              ...(profile ? [] : [[active.id, hasConnection ? `${name} (not saved)` : "No connection"] as [string, string]]),
              ...profiles.map((p): [string, string] => [p.id, p.name]),
            ]} />
          </label>
          {hasConnection && <>
          <p className="picker-endpoint">{connectionTarget(active.fields)}{active.fields.database ? ` · ${active.fields.database}` : ""}</p>
          <ReadinessFacts connection={{ ...active.fields, profile_id: active.id || undefined, type: profile?.type }} details />
          <p className="hint">TLS {active.fields.ssl_mode}</p>
          {hasConnection && safety.known && <SafetyTags type={safety.type} readOnly={safety.read_only} />}
          <p className={`picker-state${saved && !active.password ? " saved" : ""}`}>{state}</p>
          {showPassword ? <label className="field">Session password
            <span className="picker-password-row">
              <input ref={passwordField} type="password" className="picker-password" aria-label={`Password for ${name}`}
                placeholder={saved ? "Saved password" : "Password"} autoComplete="current-password"
                value={active.password} onChange={(e) => onPassword(e.target.value)}
                onBlur={() => commitPassword()}
                onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); commitPassword(); } }} />
              <button type="button" className="button picker-password-set" disabled={active.password === committedPassword.current}
                title="Use this password for this window"
                onClick={() => commitPassword()}>Set</button>
            </span>
            <span className="hint">Set, Enter or leaving the field applies it for this window. It is never saved.</span>
          </label> : <button className="button" onClick={() => setOverride(true)}>Use a different password</button>}
          {saved && showPassword && <button className="button" onClick={() => { onPassword(""); commitPassword(""); setOverride(false); }}>Use saved password</button>}
          </>}
          {!hasConnection && <p className="hint">Choose a connection to work with a server.</p>}
          {note && <p className="hint picker-note" role="status">{note}</p>}
          {hasConnection && privileges?.report?.account && <p className="hint picker-account">Connected as {privileges.report.account}</p>}
          <div className="picker-actions">
            {onAdd && <button type="button" className="button picker-new" onClick={() => { closePopovers(); onAdd(); }}>
              <Icon name="plus" size={14} /> New connection…</button>}
            <button type="button" className="button picker-manage" onClick={() => { closePopovers(); onManage(); }}>Manage connections…</button>
            {hasConnection && privileges && onAccount && <button type="button" className="button picker-account-button" aria-haspopup="dialog"
              onClick={() => { closePopovers(); onAccount(); }}>Account and privileges…</button>}
          </div>
        </div>
      </Popover>
    </div>
  );
}

/**
 * Account and privileges of the connection in use: the account a new session
 * logs in as, its active roles, its grants and the actions it is denied or
 * that SHOW GRANTS cannot verify. Opened from the connection picker or the
 * command palette; greyed actions keep their own reasons where they appear.
 */
export function AccountPrivilegesSheet({ name, hasConnection, onClose }: {
  /** Name of the connection in use, for the lead line. */
  name: string;
  hasConnection: boolean;
  onClose: () => void;
}) {
  const privileges = usePrivileges();
  const report = privileges?.report || null;
  // read_privileges returns credential-stripped grant lines at the top level.
  const grantLines = (report as { grants?: string[] } | null)?.grants;
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const leadId = useId();
  useFocusScope(dialog, { open: true, onClose, initialFocus: () => close.current,
    // Back to the picker when it is on screen, else where the sheet was opened.
    returnFocus: () => document.querySelector<HTMLElement>(".window-toolbar .connection-pill") });
  return createPortal(
    <dialog ref={dialog} className="confirm-sheet account-sheet" role="dialog" tabIndex={-1}
      aria-labelledby={titleId} aria-describedby={leadId}
      onCancel={(e) => { e.preventDefault(); onClose(); }}>
      <div className="dialog-heading"><div><h2 id={titleId}>Account and privileges</h2></div></div>
      <div className="sheet-body">
        <p id={leadId} className="sheet-lead">{hasConnection ? name : "No connection in use. Choose one in the connection picker or in Connections on Home."}</p>
        {hasConnection && (report ? <>
          <p>Connected as {report.account}</p>
          <p className="hint">New sessions. An editor tab can have different active roles.</p>
          <p>Active roles: {(report.current_roles || []).map(r => r.host ? `${r.name}@${r.host}` : r.name).join(", ") || "None"}</p>
          {(grantLines || report.model) && <details><summary>Grants</summary>
            <pre className="ops-pre">{grantLines ? grantLines.join("\n") : JSON.stringify(report.model, null, 2)}</pre>
          </details>}
          <details><summary>Denied and unverified actions</summary><ul>
            {Object.entries(report.actions).filter(([, answer]) => answer.state !== "allowed").map(([action, answer]) =>
              <li key={action}><strong>{actionLabel(action)}</strong>: {privilegeResult(answer, report.account).reason}</li>)}
          </ul></details>
        </> : <p className="hint">{privileges?.loading ? "Reading privileges…" : "Unverified. Privileges have not been read for this connection."}</p>)}
        {hasConnection && privileges?.error && <p role="alert" className="error">{privileges.error}</p>}
      </div>
      <div className="dialog-actions sheet-actions">
        {hasConnection && privileges && <button type="button" className={`button${privileges.loading ? " is-busy" : ""}`} aria-disabled={privileges.loading || undefined}
          onClick={() => { if (!privileges.loading) void privileges.refresh(true); }}>
          {privileges.loading ? "Reading privileges…" : "Refresh privileges"}
        </button>}
        <button ref={close} type="button" className="button primary" onClick={onClose}>Close</button>
      </div>
    </dialog>,
    document.body,
  );
}

// ---- Connect through SSH (myflames/ssh_tunnel.py) ----------------------------
//
// A connection can reach its server through an SSH tunnel that the backend
// opens with the system ssh and the user's own ~/.ssh/config, keys and agent.
// Host and Port then name the database as the SSH host reaches it.

/** The SSH fields of a connection as they are sent (pasted spaces dropped),
 * or none for a direct connection: SSH user and port count only with a host. */
export function sshFields(c: Partial<Connection>): Pick<Connection, "ssh_host" | "ssh_user" | "ssh_port"> {
  const host = trimPasted(c.ssh_host || "");
  if (!host) return {};
  const user = trimPasted(c.ssh_user || ""), port = Number(c.ssh_port) || 0;
  return { ssh_host: host, ...(user ? { ssh_user: user } : {}), ...(port ? { ssh_port: port } : {}) };
}
/** What is wrong with the SSH fields, as the server checks them
 * (ssh_tunnel.ssh_fields), or "". */
export function sshProblem(c: Partial<Connection>): string {
  const host = trimPasted(c.ssh_host || ""), user = trimPasted(c.ssh_user || ""), port = Number(c.ssh_port) || 0;
  if (!host) return user || port ? "Enter the SSH host, or clear SSH user and SSH port." : "";
  if (host.includes("@")) return "Enter the SSH user in SSH user, not in SSH host.";
  if (/[:/]/.test(host)) return "Enter only the SSH host name or alias in SSH host, and the port in SSH port.";
  if (!/^[A-Za-z0-9_][A-Za-z0-9._-]{0,252}$/.test(host))
    return "SSH host: letters, digits, dots, hyphens and underscores, not starting with a hyphen.";
  if (user && !/^[A-Za-z0-9_][A-Za-z0-9._@-]{0,127}$/.test(user))
    return "SSH user: letters, digits, dots, hyphens, underscores or @, not starting with a hyphen or @.";
  if (port && !(Number.isInteger(port) && port >= 1 && port <= 65535)) return "Enter an SSH port from 1 to 65535, or leave it empty.";
  return "";
}
/** ssh/hosts (ssh-hosts-1.0): Host aliases of ~/.ssh/config, and whether ssh is installed. */
type SshHosts = { hosts: string[]; ssh_found: boolean; message: string };

/** Connect through SSH: the SSH host (a pick list of the Host aliases in
 * ~/.ssh/config, or any host name), SSH user and SSH port. Open while the
 * form's connection goes through SSH. */
function SshSettings({ draft, edit, formKey }: {
  draft: Connection;
  edit: <K extends keyof Connection>(key: K, value: Connection[K]) => void;
  /** Changes when the form opens another connection. */
  formKey: number;
}) {
  const through = trimPasted(draft.ssh_host || "");
  const [open, setOpen] = useState(!!through);
  const [hosts, setHosts] = useState<SshHosts | null>(null);
  const asked = useRef(false);
  const listId = useId(), problemId = useId(), hintId = useId();
  const problem = sshProblem(draft);
  // Another connection in the form: open only when it goes through SSH.
  useEffect(() => setOpen(!!trimPasted(draft.ssh_host || "")), [formKey]);
  // The aliases are read once, when the section is first opened.
  useEffect(() => {
    if (!open || asked.current) return;
    asked.current = true;
    api<SshHosts>("ssh/hosts", {}).then(setHosts, () => setHosts({ hosts: [], ssh_found: true, message: "" }));
  }, [open]);
  return (
    // No class: a plain <details> gets the shared disclosure chevron (kit.css), like TLS settings.
    <details data-section="ssh" open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>Connect through SSH{through ? ` · ${through}` : ""}</summary>
      <div className="connection-fields">
        <label className="field">
          SSH host
          <input
            list={listId}
            value={draft.ssh_host || ""}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            placeholder="Host alias or host name"
            aria-invalid={problem ? true : undefined}
            aria-describedby={problem ? `${problemId} ${hintId}` : hintId}
            onChange={(e) => edit("ssh_host", e.target.value)}
          />
          <datalist id={listId}>
            {(hosts?.hosts || []).map((h) => (
              <option key={h} value={h} />
            ))}
          </datalist>
        </label>
        {/* Port before user, like the database's Port and User above. */}
        <div className="field-pair">
          <label className="field">
            SSH port
            <input
              type="number"
              min="1"
              max="65535"
              value={draft.ssh_port || ""}
              placeholder="22"
              onChange={(e) => edit("ssh_port", e.target.value === "" ? 0 : +e.target.value)}
            />
          </label>
          <label className="field">
            SSH user
            <input
              value={draft.ssh_user || ""}
              spellCheck={false}
              autoCapitalize="off"
              autoCorrect="off"
              placeholder="From ~/.ssh/config"
              onChange={(e) => edit("ssh_user", e.target.value)}
            />
          </label>
        </div>
      </div>
      {problem && <span className="form-error" id={problemId} role="alert">{problem}</span>}
      <p className="hint" id={hintId}>
        Uses your SSH keys or agent. Password sign-in to SSH is not supported.
        {through ? " Host and Port above are the database as the SSH host reaches it, often 127.0.0.1." : ""}
      </p>
      {hosts && !hosts.ssh_found && <p className="hint safety-hint is-warning" role="note">{hosts.message}</p>}
    </details>
  );
}
