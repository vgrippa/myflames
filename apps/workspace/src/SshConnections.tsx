// The File transfer place: the saved SSH connection list, the New/Edit SSH
// connection sheet, and the host-key trust review. Spec: docs/_lane_notes/files-ui-spec.md
// (lane C1); the wire contract is docs/_lane_notes/files-contract.md and every
// call goes through filesApi.ts. A typed secret (SSH password or key
// passphrase) leaves React state as soon as a save or test resolves and is
// never echoed into a notice, an error, a title, the console or localStorage.
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Profile } from "./api";
import {
  baseName,
  parentPath,
  sshConfigHosts,
  sshConnectionDelete,
  sshConnectionSave,
  sshConnectionTest,
  sshErrorCode,
  sshHostKeyScan,
  sshHostKeyTrust,
  sshKeys,
  type HostKeyScan,
  type SshAuth,
  type SshConnection,
  type SshConnectionDraft,
  type SshTarget,
} from "./filesApi";
import {
  ActionButton,
  ActionStatus,
  ConfirmSheet,
  ContextMenu,
  copyToClipboard,
  errorText,
  FormGrid,
  FormRow,
  IconButton,
  IDLE_ACTION,
  moveRowFocus,
  Notice,
  Segmented,
  ToolbarButton,
  useAction,
  useFocusScope,
  ViewStatus,
  type ActionState,
  type TableContextAction,
} from "./kit";
import { FolderField } from "./FolderField";
import { WINDOWS } from "./platform";
import "./ssh-connections.css";

/** "user@host:port", leaving out the parts a connection does not have. */
export function targetLabel(c: Pick<SshConnection, "user" | "host" | "port">): string {
  const host = c.host.includes(":") ? `[${c.host}]` : c.host;
  return `${c.user ? `${c.user}@` : ""}${host}${c.port != null ? `:${c.port}` : ""}`;
}

/** The port field as a value to send: null when empty, a problem otherwise. */
function readPort(text: string): { port: number | null; problem: string } {
  const trimmed = text.trim();
  if (!trimmed) return { port: null, problem: "" };
  const value = Number(trimmed);
  if (!Number.isInteger(value) || value < 1 || value > 65535)
    return { port: null, problem: "Enter a port from 1 to 65535, or leave it empty." };
  return { port: value, problem: "" };
}

/** A pasted "[sftp://][user@]host[:port][/path]" split into its fields, or
 * null when the text is a plain host (or a ~/.ssh/config alias). */
export function splitSshAddress(text: string): { user: string; host: string; port: string; path: string } | null {
  const match = /^(?:s(?:ftp|sh):\/\/)?(?:([^@\s/]+)@)?(\[[^\]\s]+\]|[^:@\s/]+)(?::(\d{1,5}))?(\/\S*)?$/.exec(text.trim());
  if (!match || !(match[1] || match[3] || match[4] || /^s(ftp|sh):\/\//.test(text.trim()))) return null;
  return { user: match[1] || "", host: match[2].replace(/^\[|\]$/g, ""), port: match[3] || "", path: match[4] || "" };
}

/** Database hosts that cannot be an SSH target from here (this computer). */
const LOCAL_HOSTS = new Set(["", "localhost", "127.0.0.1", "::1"]);

/** A SourceList-style column of saved SSH connections (kit SourceList rows
 * hold one line; these rows carry a muted target line and a row menu). A row
 * opens only on click, Enter or the menu's Open: arrow keys move focus
 * without connecting, and right-click opens the menu where the row is. */
export function SshConnectionList(props: {
  connections: SshConnection[];
  loading: boolean;
  error: string;
  selectedId: string;
  /** The inline Test connection / Copy address feedback, keyed by row. */
  rowStatus: { id: string; state: ActionState };
  onSelect: (id: string) => void;
  onNew: () => void;
  onEdit: (connection: SshConnection) => void;
  onDuplicate: (connection: SshConnection) => void;
  onTest: (connection: SshConnection) => void;
  onCopyAddress: (connection: SshConnection) => void;
  onDelete: (connection: SshConnection) => void;
  onRetry: () => void;
}): JSX.Element {
  const { connections, loading, error, selectedId, rowStatus, onSelect, onNew, onEdit, onDuplicate, onTest, onCopyAddress, onDelete, onRetry } = props;
  const [menu, setMenu] = useState<null | { connection: SshConnection; x: number; y: number; opener: HTMLElement }>(null);
  const openMenu = (connection: SshConnection, x: number, y: number, opener: HTMLElement) =>
    setMenu({ connection, x, y, opener });

  function rowActions(c: SshConnection): TableContextAction[] {
    return [
      { label: "Open", icon: "chevron", onClick: () => onSelect(c.id) },
      { label: "Edit…", icon: "edit", shortcut: "F2", onClick: () => onEdit(c) },
      { label: "Duplicate", onClick: () => onDuplicate(c) },
      { label: "Test connection", onClick: () => onTest(c) },
      { label: "Copy address", icon: "copy", disabled: !targetLabel(c), onClick: () => onCopyAddress(c) },
      { divider: true },
      { label: "Delete…", icon: "trash", danger: true, onClick: () => onDelete(c) },
    ];
  }

  const empty = !loading && !error && connections.length === 0;
  return (
    <nav className="ssh-connections" aria-label="SSH connections">
      <div className="ssh-connections-head">
        <h2>SSH connections</h2>
        {!empty && <ToolbarButton icon="plus" onClick={onNew}>New</ToolbarButton>}
      </div>
      <ViewStatus loading={loading ? "Loading SSH connections…" : undefined} error={error || undefined} onRetry={onRetry} />
      {empty && <p className="ssh-connections-empty">None saved yet.</p>}
      {!loading && !error && connections.length > 0 && (
        <ul className="ssh-connection-rows" data-rows>
          {connections.map((c) => {
            const selected = c.id === selectedId;
            return (
              <li key={c.id}>
                <div className={`ssh-connection-row${selected ? " is-selected" : ""}`}>
                  <button
                    type="button"
                    className="ssh-connection-main"
                    data-row-key={c.id}
                    tabIndex={selected ? 0 : -1}
                    aria-current={selected ? "page" : undefined}
                    onClick={() => onSelect(c.id)}
                    onContextMenu={(e) => {
                      // Right-click opens the row's menu; it never selects or connects.
                      e.preventDefault();
                      openMenu(c, e.clientX, e.clientY, e.currentTarget);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
                        e.preventDefault();
                        const box = e.currentTarget.getBoundingClientRect();
                        openMenu(c, box.left + 24, box.bottom, e.currentTarget);
                        return;
                      }
                      if (e.key === "F2") {
                        e.preventDefault();
                        onEdit(c);
                        return;
                      }
                      if (e.key === "Delete" || e.key === "Backspace") {
                        e.preventDefault();
                        onDelete(c);
                        return;
                      }
                      // Arrows, Home and End move focus only; Enter and Space open.
                      moveRowFocus(e);
                    }}
                  >
                    <span className="ssh-connection-name">{c.name}</span>
                    <span className="ssh-connection-target">{targetLabel(c) || "No host set"}</span>
                  </button>
                  <IconButton
                    icon="more"
                    className="ssh-connection-more"
                    label={`More actions for ${c.name}`}
                    aria-haspopup="menu"
                    aria-expanded={menu?.connection.id === c.id}
                    onClick={(e) => {
                      const box = e.currentTarget.getBoundingClientRect();
                      openMenu(c, box.left, box.bottom, e.currentTarget);
                    }}
                  />
                </div>
                <ActionStatus className="ssh-connection-status" state={rowStatus.id === c.id ? rowStatus.state : IDLE_ACTION} />
              </li>
            );
          })}
        </ul>
      )}
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          label={`Actions for ${menu.connection.name}`}
          opener={menu.opener}
          actions={rowActions(menu.connection)}
          onClose={() => setMenu(null)}
        />
      )}
    </nav>
  );
}

type Problems = { name?: string; host?: string; port?: string; key_path?: string };

/** New SSH connection (no *initial*) or Edit <name>: fields, secrets, Test,
 * Save and Delete. *draft* pre-fills a New connection (Duplicate): it carries
 * no id and no saved secret. A host_key_unknown answer turns the sheet itself
 * into the trust step (one dialog at a time), and Back returns to the form. */
export function SshConnectionSheet(props: {
  /** Absent: New SSH connection. */
  initial?: SshConnection;
  /** Pre-filled fields for a New connection; never an id or a secret. */
  draft?: SshConnectionDraft;
  /** Saved database connections; those with ssh_host pre-fill the form ("Start from"). */
  profiles: Profile[];
  existingNames: string[];
  onClose: () => void;
  onSaved: (connection: SshConnection) => void;
  onDeleted: (id: string) => void;
}): JSX.Element {
  const { initial, draft, profiles, existingNames, onClose, onSaved, onDeleted } = props;
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = "ssh-conn-sheet-title";
  const from = initial || draft;
  const [name, setName] = useState(from?.name || "");
  // Name follows user@host until the person types one of their own.
  const [nameEdited, setNameEdited] = useState(!!from);
  const [host, setHost] = useState(from?.host || "");
  const [port, setPort] = useState(from?.port != null ? String(from.port) : "");
  const [user, setUser] = useState(from?.user || "");
  const [auth, setAuth] = useState<SshAuth>(from?.auth || "agent");
  const [keyPath, setKeyPath] = useState(from?.key_path || "");
  const [keyMode, setKeyMode] = useState<"pick" | "type">("pick");
  const [remoteDir, setRemoteDir] = useState(from?.remote_dir || "");
  const [localDir, setLocalDir] = useState(from?.local_dir || "");
  const [secret, setSecret] = useState("");
  const [replaceSecret, setReplaceSecret] = useState(false);
  const [forgetSecret, setForgetSecret] = useState(false);
  const [tried, setTried] = useState(false);
  const [startFrom, setStartFrom] = useState("");
  const [configHosts, setConfigHosts] = useState<string[] | null>(null);
  const [keys, setKeys] = useState<{ path: string; type: string; comment: string }[] | null>(null);
  const [keysError, setKeysError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [hostKeyTarget, setHostKeyTarget] = useState<SshTarget | null>(null);
  const hostKey = useHostKey(hostKeyTarget);
  const test = useAction();
  const save = useAction();
  const del = useAction();

  // The ~/.ssh/config aliases feed both "Start from" and the Host datalist.
  useEffect(() => {
    let live = true;
    sshConfigHosts().then(
      (r) => { if (live) setConfigHosts(r.hosts || []); },
      () => { if (live) setConfigHosts([]); },
    );
    return () => { live = false; };
  }, []);
  // The Key file pick list is read only when that authentication is chosen.
  useEffect(() => {
    if (auth !== "key" || keys !== null || keysError) return;
    let live = true;
    sshKeys().then(
      (r) => { if (live) setKeys(r.keys || []); },
      (e) => { if (live) setKeysError(errorText(e)); },
    );
    return () => { live = false; };
  }, [auth, keys, keysError]);

  const secretKind = auth === "key" ? "passphrase" : "password";
  const secretLabel = auth === "key" ? "Passphrase" : "Password";
  // The saved secret belongs to the authentication it was saved with; after a
  // switch the server drops it, so the sheet offers a fresh input instead.
  const hasSecret = !!initial && initial.has_secret && auth === initial.auth;

  function currentDraft(): SshConnectionDraft {
    return {
      ...(initial ? { id: initial.id } : {}),
      name: name.trim(),
      host: host.trim(),
      port: readPort(port).port,
      user: user.trim(),
      auth,
      key_path: auth === "key" ? keyPath.trim() : "",
      remote_dir: remoteDir.trim(),
      local_dir: localDir.trim(),
    };
  }
  function currentTarget(): SshTarget {
    return { connection: currentDraft(), ...(secret ? { secret } : {}) };
  }

  const problems: Problems = {};
  const trimmedName = name.trim();
  if (!trimmedName) problems.name = "Enter a name.";
  else {
    const clash = existingNames.find(
      (n) => n.toLowerCase() === trimmedName.toLowerCase() && (!initial || initial.name.toLowerCase() !== n.toLowerCase()),
    );
    if (clash) problems.name = `An SSH connection named “${clash}” already exists.`;
  }
  if (!host.trim()) problems.host = "Enter the host name or a Host alias from ~/.ssh/config.";
  const portRead = readPort(port);
  if (portRead.problem) problems.port = portRead.problem;
  if (auth === "key" && !keyPath.trim()) problems.key_path = "Choose a key file, or type its full path.";

  function focusFirstProblem() {
    for (const id of ["ssh-conn-name", "ssh-conn-host", "ssh-conn-port", "ssh-conn-key"]) {
      const key = id === "ssh-conn-key" ? "key_path" : id.replace("ssh-conn-", "");
      if (problems[key as keyof Problems]) {
        document.getElementById(id)?.focus();
        return;
      }
    }
  }

  // The typed secret, kept only while a host-key prompt waits to test again.
  const pendingSecret = useRef("");
  async function runTest(retrySecret?: string) {
    const draftTarget = currentTarget();
    const target: SshTarget = retrySecret && "connection" in draftTarget ? { ...draftTarget, secret: retrySecret } : draftTarget;
    const result = await test.run((signal) => sshConnectionTest(target, signal), {
      pending: `Testing the connection to ${targetLabel(currentDraft())}…`,
      done: (v) => `Connected in ${(v.elapsed_ms / 1000).toFixed(1)} s. Home folder: ${v.home}`,
      failed: (e) => errorText(e),
    });
    // The typed secret leaves state as soon as the test resolves; a host-key
    // prompt keeps it in a ref until the test after "Trust and connect".
    pendingSecret.current = "";
    if (!result.ok && result.error && sshErrorCode(result.error) === "host_key_unknown") {
      pendingSecret.current = "secret" in target && target.secret ? target.secret : "";
      setHostKeyTarget(target);
    }
    setSecret("");
  }

  async function runSave() {
    setTried(true);
    if (Object.keys(problems).length) {
      focusFirstProblem();
      return;
    }
    const target = currentTarget() as { connection: SshConnectionDraft; secret?: string };
    const result = await save.run(() => sshConnectionSave(target.connection, target.secret, forgetSecret || undefined), {
      pending: "Saving…",
      write: true,
      done: "",
    });
    setSecret("");
    if (result.ok) onSaved(result.value.connection);
  }

  async function runDelete() {
    if (!initial) return;
    const result = await del.run(() => sshConnectionDelete(initial.id), { pending: "Deleting…", write: true, done: "" });
    if (result.ok) {
      setConfirmDelete(false);
      onDeleted(initial.id);
    } else {
      setDeleteError(
        result.reason === "unknown"
          ? "No reply arrived. The connection may have been deleted; close this sheet and reload the list."
          : errorText(result.error),
      );
    }
  }

  // The trust step replaces the form while it is open: Escape and Back leave
  // the step, never the sheet, and the form state survives underneath.
  function backFromTrust() {
    pendingSecret.current = "";
    setHostKeyTarget(null);
    window.requestAnimationFrame(() => dialog.current?.querySelector<HTMLElement>(".ssh-test-button")?.focus());
  }
  const dismiss = useCallback(() => {
    if (hostKeyTarget) {
      if (!hostKey.trust.pending) backFromTrust();
      return;
    }
    if (!save.pending && !del.pending) onClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostKeyTarget, hostKey.trust.pending, save.pending, del.pending, onClose]);
  useFocusScope(dialog, { open: true, onClose: dismiss });
  // The trust step's heading takes focus, so keyboard and screen-reader users
  // land on the prompt instead of staying on a form that is no longer shown.
  useEffect(() => {
    if (!hostKeyTarget) return;
    window.requestAnimationFrame(() => dialog.current?.querySelector<HTMLElement>(`#${titleId}`)?.focus());
  }, [hostKeyTarget]);

  // Database connections offer their SSH host, or else their own host (SSH
  // usually runs on the database server, port 22); this computer is left out.
  const sshProfiles = profiles.filter((p) => p.ssh_host || !LOCAL_HOSTS.has((p.host || "").trim().toLowerCase()));
  function fillName(nextUser: string, nextHost: string) {
    if (!nameEdited && nextHost) setName(`${nextUser ? `${nextUser}@` : ""}${nextHost}`);
  }
  function applyStartFrom(choice: string) {
    if (choice.startsWith("profile:")) {
      const profile = sshProfiles.find((p) => `profile:${p.id}` === choice);
      if (!profile) return;
      const viaTunnel = !!profile.ssh_host;
      setHost(viaTunnel ? profile.ssh_host || "" : profile.host || "");
      setPort(viaTunnel && profile.ssh_port ? String(profile.ssh_port) : "");
      setUser(viaTunnel ? profile.ssh_user || "" : "");
      if (!nameEdited) setName(profile.name);
    } else if (choice.startsWith("host:")) {
      setHost(choice.slice(5));
      fillName(user, choice.slice(5));
    }
  }
  /** A pasted or finished "user@host:port/path" fills its fields (never per
   * keystroke: "host:2" is half a port while typing). */
  function splitHost(text: string): boolean {
    const parts = splitSshAddress(text);
    if (!parts) return false;
    setHost(parts.host);
    if (parts.user) setUser(parts.user);
    if (parts.port) setPort(parts.port);
    if (parts.path && !remoteDir) setRemoteDir(parts.path);
    fillName(parts.user || user, parts.host);
    return true;
  }

  const listedKeys = (keys || []).map((k) => k.path);
  const keyOptions: [string, string][] = [
    ["", "Choose a key…"],
    ...(keys || []).map((k): [string, string] => [k.path, keyLabel(k)]),
    ...(keyPath && !listedKeys.includes(keyPath) ? [[keyPath, `${baseName(keyPath)} · ${parentPath(keyPath)}`] as [string, string]] : []),
    ["__type", "Other key file…"],
  ];
  const keyPicker = keyMode === "pick" && keys !== null && keys.length > 0;
  const keyHint = keyPicker
    ? keyPath
      ? keyPath
      : "Private keys in ~/.ssh that have a public key beside them."
    : keysError
      ? `Could not list the keys in ~/.ssh (${keysError}). Type the full path of the key.`
      : keys !== null && keys.length === 0
        ? "No keys with a public key found in ~/.ssh. Type the full path of the key."
        : "Type the full path of a key outside ~/.ssh.";

  return createPortal(
    <dialog
      ref={dialog}
      className="confirm-sheet ssh-sheet"
      role="dialog"
      tabIndex={-1}
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        dismiss();
      }}
    >
      <div className="dialog-heading">
        <div>
          <h2 id={titleId} tabIndex={-1}>
            {hostKeyTarget
              ? hostKey.scan
                ? `Trust ${hostKey.scan.host}?`
                : "Trust this server?"
              : initial
                ? `Edit ${initial.name}`
                : "New SSH connection"}
          </h2>
        </div>
      </div>
      {hostKeyTarget ? (
        <>
          <div className="sheet-body">
            <HostKeyReview
              scan={hostKey.scan}
              scanning={hostKey.scanning}
              scanError={hostKey.scanError}
              onRetry={hostKey.scanNow}
              trust={hostKey.trust}
            />
          </div>
          <div className="dialog-actions sheet-actions">
            <button type="button" className="button ssh-hostkey-back" disabled={hostKey.trust.pending} onClick={backFromTrust}>
              Back
            </button>
            <ActionButton
              primary
              className="hostkey-trust-button"
              busy={hostKey.trust.pending}
              busyLabel="Trusting…"
              disabled={!hostKey.scan}
              onClick={() =>
                void hostKey.runTrust(() => {
                  setHostKeyTarget(null);
                  // Trust granted: the test runs again on its own, with the same secret.
                  void runTest(pendingSecret.current || undefined);
                })
              }
            >
              Trust and connect
            </ActionButton>
          </div>
        </>
      ) : (
        <>
          <div className="sheet-body">
            <FormGrid>
              {(sshProfiles.length > 0 || (configHosts && configHosts.length > 0)) && (
                <FormRow label="Start from" htmlFor="ssh-conn-start" hint="Fill Host, Port and User from a database connection or a ~/.ssh/config alias.">
                  <select id="ssh-conn-start" value={startFrom} onChange={(e) => { applyStartFrom(e.target.value); setStartFrom(""); }}>
                    <option value="">Choose…</option>
                    {sshProfiles.length > 0 && (
                      <optgroup label="Database connections">
                        {sshProfiles.map((p) => (
                          <option key={p.id} value={`profile:${p.id}`}>
                            {p.name} · {p.ssh_host ? `${p.ssh_user ? `${p.ssh_user}@` : ""}${p.ssh_host} (SSH tunnel)` : p.host}
                          </option>
                        ))}
                      </optgroup>
                    )}
                    {configHosts && configHosts.length > 0 && (
                      <optgroup label="~/.ssh/config">
                        {configHosts.map((h) => (
                          <option key={h} value={`host:${h}`}>{h}</option>
                          ))}
                      </optgroup>
                    )}
                  </select>
                </FormRow>
              )}
              <FormRow label="Name" htmlFor="ssh-conn-name" error={tried ? problems.name : undefined}>
                <input id="ssh-conn-name" value={name} spellCheck={false} autoComplete="off"
                  onChange={(e) => { setName(e.target.value); setNameEdited(true); }} />
              </FormRow>
              <FormRow label="Host" htmlFor="ssh-conn-host" error={tried ? problems.host : undefined} hint="A host name or a Host alias from ~/.ssh/config. Pasting user@host:port fills User and Port too.">
                <input
                  id="ssh-conn-host"
                  list="ssh-conn-host-aliases"
                  value={host}
                  spellCheck={false}
                  autoCapitalize="off"
                  autoCorrect="off"
                  autoComplete="off"
                  placeholder="Host alias or host name"
                  onChange={(e) => { setHost(e.target.value); fillName(user, e.target.value.trim()); }}
                  onBlur={(e) => void splitHost(e.target.value)}
                  onPaste={(e) => {
                    const pasted = e.clipboardData.getData("text");
                    if (splitHost(pasted)) e.preventDefault();
                  }}
                />
              </FormRow>
              <datalist id="ssh-conn-host-aliases">
                {(configHosts || []).map((h) => (
                  <option key={h} value={h} />
                ))}
              </datalist>
              <FormRow label="Port" htmlFor="ssh-conn-port" error={tried ? problems.port : undefined}>
                <input
                  id="ssh-conn-port"
                  type="number"
                  min={1}
                  max={65535}
                  value={port}
                  placeholder="22"
                  onChange={(e) => setPort(e.target.value)}
                />
              </FormRow>
              <FormRow label="User" htmlFor="ssh-conn-user">
                <input
                  id="ssh-conn-user"
                  value={user}
                  spellCheck={false}
                  autoCapitalize="off"
                  autoCorrect="off"
                  autoComplete="off"
                  placeholder="From ~/.ssh/config or your user name"
                  onChange={(e) => { setUser(e.target.value); fillName(e.target.value.trim(), host.trim()); }}
                />
              </FormRow>
              <FormRow label="Authentication">
                <Segmented<SshAuth>
                  label="Authentication"
                  className="ssh-auth"
                  value={auth}
                  onChange={setAuth}
                  options={[
                    { value: "agent", label: "SSH agent or keys" },
                    { value: "key", label: "Key file" },
                    { value: "password", label: "Password", disabled: WINDOWS },
                  ]}
                />
              </FormRow>
              {WINDOWS && (
                <FormRow label="">
                  <p className="hint">On Windows, myflames signs in with the SSH agent or a key without a passphrase; password and passphrase sign-in are not available yet.</p>
                </FormRow>
              )}
              {auth === "key" && (
                <FormRow label="Key file" htmlFor="ssh-conn-key" error={tried ? problems.key_path : undefined} hint={keyHint}>
                  {keys === null && !keysError ? (
                    <select id="ssh-conn-key" disabled aria-label="Key file">
                      <option>Reading keys…</option>
                    </select>
                  ) : keyPicker ? (
                    <select
                      id="ssh-conn-key"
                      className="select"
                      aria-label="Key file"
                      value={keyPath}
                      title={keyPath || undefined}
                      onChange={(e) => {
                        if (e.target.value === "__type") setKeyMode("type");
                        else setKeyPath(e.target.value);
                      }}
                    >
                      {keyOptions.map(([value, text]) => (
                        <option key={value || text} value={value}>{text}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      id="ssh-conn-key"
                      value={keyPath}
                      spellCheck={false}
                      autoCapitalize="off"
                      autoCorrect="off"
                      autoComplete="off"
                      placeholder="/full/path/to/key"
                      onChange={(e) => setKeyPath(e.target.value)}
                    />
                  )}
                </FormRow>
              )}
              {auth === "key" && keyMode === "type" && keys !== null && keys.length > 0 && (
                <FormRow label="">
                  <button type="button" className="text-button" onClick={() => setKeyMode("pick")}>Choose from the keys in ~/.ssh</button>
                </FormRow>
              )}
              {auth !== "agent" && hasSecret && !replaceSecret && !forgetSecret && (
                <FormRow label={secretLabel}>
                  <div className="ssh-secret-saved">
                    <span className="ssh-secret-state">Saved securely</span>
                    <button type="button" className="text-button" onClick={() => setReplaceSecret(true)}>Replace</button>
                    <button type="button" className="text-button danger" onClick={() => setForgetSecret(true)}>Forget</button>
                  </div>
                </FormRow>
              )}
              {auth !== "agent" && hasSecret && forgetSecret && (
                <FormRow label={secretLabel}>
                  <div className="ssh-secret-saved">
                    <span className="hint">The saved {secretKind} is removed when you save.</span>
                    <button type="button" className="text-button" onClick={() => setForgetSecret(false)}>Undo</button>
                  </div>
                </FormRow>
              )}
              {auth !== "agent" && !WINDOWS && (!hasSecret || replaceSecret) && !forgetSecret && (
                <FormRow
                  label={secretLabel}
                  htmlFor="ssh-conn-secret"
                  hint={auth === "key" ? "Optional. Saved securely with the connection." : "Saved securely with the connection."}
                >
                  <input id="ssh-conn-secret" type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} />
                </FormRow>
              )}
              <FormRow label="Default remote folder" htmlFor="ssh-conn-remote">
                <input
                  id="ssh-conn-remote"
                  value={remoteDir}
                  spellCheck={false}
                  autoCapitalize="off"
                  autoCorrect="off"
                  autoComplete="off"
                  placeholder="Home folder"
                  onChange={(e) => setRemoteDir(e.target.value)}
                />
              </FormRow>
              <FolderField row label="Default local folder" value={localDir} onChange={setLocalDir} hint="The folder opened first on this computer." />
            </FormGrid>
          </div>
          <div className="ssh-sheet-status">
            <ActionStatus state={test} />
            <ActionStatus state={save} />
          </div>
          <div className="dialog-actions sheet-actions ssh-sheet-actions">
            {initial && (
              <ActionButton
                danger
                className="ssh-delete-button"
                disabled={save.pending || del.pending}
                onClick={() => {
                  setDeleteError("");
                  setConfirmDelete(true);
                }}
              >
                Delete…
              </ActionButton>
            )}
            <ActionButton
              className="ssh-test-button"
              busy={test.pending}
              busyLabel="Testing…"
              disabled={!test.pending && (save.pending || del.pending)}
              onClick={() => void runTest()}
            >
              Test connection
            </ActionButton>
            <button type="button" className="button" disabled={save.pending || del.pending} onClick={dismiss}>Cancel</button>
            <ActionButton
              primary
              className="ssh-save-button"
              busy={save.pending}
              busyLabel="Saving…"
              disabled={!save.pending && (test.pending || del.pending)}
              onClick={() => void runSave()}
            >
              Save
            </ActionButton>
          </div>
        </>
      )}
      {confirmDelete && initial && (
        <ConfirmSheet
          title={`Delete ${initial.name}?`}
          description="Its saved password or passphrase is removed from this computer."
          busy={del.pending}
          busyLabel="Deleting…"
          confirmLabel="Delete"
          onCancel={() => setConfirmDelete(false)}
          onConfirm={() => void runDelete()}
        >
          {deleteError && <Notice variant="error">{deleteError}</Notice>}
        </ConfirmSheet>
      )}
    </dialog>,
    document.body,
  );
}

/** The type short enough for a pick list: "ssh-ed25519" reads "ed25519". */
function shortKeyType(type: string): string {
  const short = type.replace(/^ssh-/i, "").toLowerCase();
  return short.startsWith("ecdsa-sha2-") ? "ecdsa" : short;
}

/** A key option label: the file name first (it tells keys apart), the folder
 * last as secondary context, so truncation never cuts the name at the start. */
function keyLabel(key: { path: string; type: string; comment: string }): string {
  return `${baseName(key.path)}${key.type ? ` (${shortKeyType(key.type)})` : ""}${key.comment ? ` · ${key.comment}` : ""} · ${parentPath(key.path)}`;
}

/** The host-key scan and trust state, shared by the standalone HostKeySheet
 * and the connection sheet's trust step. A null target scans nothing. */
function useHostKey(target: SshTarget | null) {
  const [scan, setScan] = useState<HostKeyScan | null>(null);
  const [scanError, setScanError] = useState("");
  const [scanning, setScanning] = useState(true);
  // Only the latest scan of the current target may answer: a late answer for
  // an earlier target (Back, then another host) never offers its keys here.
  const scanSeq = useRef(0);
  const trust = useAction();

  const scanNow = useCallback(() => {
    if (!target) return;
    const seq = ++scanSeq.current;
    setScan(null);
    setScanning(true);
    setScanError("");
    sshHostKeyScan(target).then(
      (result) => { if (scanSeq.current === seq) setScan(result); },
      (error) => { if (scanSeq.current === seq) setScanError(errorText(error)); },
    ).finally(() => {
      if (scanSeq.current === seq) setScanning(false);
    });
  }, [target]);
  useEffect(() => {
    if (!target) {
      scanSeq.current++;
      setScan(null);
      setScanError("");
      setScanning(false);
      return;
    }
    scanNow();
    return () => { scanSeq.current++; };
  }, [target, scanNow]);

  /** Trust the scanned keys, then run *onTrusted* (a retried test or listing). */
  const runTrust = useCallback(
    async (onTrusted: () => void) => {
      if (!scan) return;
      const result = await trust.run(() => sshHostKeyTrust(scan.token), { pending: "Trusting…", write: true, done: "" });
      if (result.ok) onTrusted();
    },
    [scan, trust],
  );

  return { scan, scanError, scanning, scanNow, trust, runTrust };
}

/** What both host-key prompts show: the scan progress, the scan error with
 * Try again, the key list with Copy buttons, and the trust outcome. */
export function HostKeyReview(props: {
  scan: HostKeyScan | null;
  scanning: boolean;
  scanError: string;
  onRetry: () => void;
  trust: ActionState & { pending: boolean };
}): JSX.Element {
  const { scan, scanning, scanError, onRetry, trust } = props;
  return (
    <>
      {scanning && (
        <p className="capture-status">
          <span className="spinner" aria-hidden="true" /> Reading the server's host key…
        </p>
      )}
      {!scanning && scanError && (
        <>
          <Notice variant="error">{scanError}</Notice>
          <p>
            <button type="button" className="button hostkey-retry" onClick={onRetry}>Try again</button>
          </p>
        </>
      )}
      {scan && (
        <>
          <p className="sheet-lead">
            This is the first connection to {scan.host} from myflames. Compare the fingerprint with the one your server
            administrator gives you.
          </p>
          <ul className="hostkey-list">
            {scan.keys.map((key) => (
              <li key={`${key.type}-${key.fingerprint}`}>
                <span className="hostkey-kind">{key.type}{key.bits ? ` (${key.bits} bits)` : ""}</span>
                <code className="hostkey-fingerprint">{key.fingerprint}</code>
                <button type="button" className="text-button hostkey-copy" onClick={() => void copyToClipboard(key.fingerprint)}
                  aria-label={`Copy the ${key.type} fingerprint`}>Copy</button>
              </li>
            ))}
          </ul>
        </>
      )}
      <ActionStatus state={trust} />
    </>
  );
}

/** Trust a server's host key from outside the connection sheet (the browser,
 * a pane, the transfer queue): scan it, show the fingerprints, append them to
 * the myflames known_hosts file on "Trust and connect". */
export function HostKeySheet(props: { target: SshTarget; onTrusted: () => void; onCancel: () => void }): JSX.Element {
  const { target, onTrusted, onCancel } = props;
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = "hostkey-sheet-title";
  const hostKey = useHostKey(target);

  const dismiss = useCallback(() => {
    if (!hostKey.trust.pending) onCancel();
  }, [hostKey.trust.pending, onCancel]);
  useFocusScope(dialog, { open: true, onClose: dismiss });

  return createPortal(
    <dialog
      ref={dialog}
      className="confirm-sheet hostkey-sheet"
      role="dialog"
      tabIndex={-1}
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        dismiss();
      }}
    >
      <div className="dialog-heading">
        <div>
          <h2 id={titleId}>{hostKey.scan ? `Trust ${hostKey.scan.host}?` : "Trust this server?"}</h2>
        </div>
      </div>
      <div className="sheet-body">
        <HostKeyReview
          scan={hostKey.scan}
          scanning={hostKey.scanning}
          scanError={hostKey.scanError}
          onRetry={hostKey.scanNow}
          trust={hostKey.trust}
        />
      </div>
      <div className="dialog-actions sheet-actions">
        <button type="button" className="button" disabled={hostKey.trust.pending} onClick={dismiss}>Cancel</button>
        <ActionButton
          primary
          className="hostkey-trust-button"
          busy={hostKey.trust.pending}
          busyLabel="Trusting…"
          disabled={!hostKey.scan}
          onClick={() => void hostKey.runTrust(onTrusted)}
        >
          Trust and connect
        </ActionButton>
      </div>
    </dialog>,
    document.body,
  );
}
