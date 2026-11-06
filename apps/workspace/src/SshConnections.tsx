// The Files place: the saved SSH connection list, the New/Edit SSH connection
// sheet, and the host-key trust sheet. Spec: docs/_lane_notes/files-ui-spec.md
// (lane C1); the wire contract is docs/_lane_notes/files-contract.md and every
// call goes through filesApi.ts. A typed secret (SSH password or key
// passphrase) leaves React state as soon as a save or test resolves and is
// never echoed into a notice, an error, a title, the console or localStorage.
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Profile } from "./api";
import {
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
  copyToClipboard,
  EmptyState,
  errorText,
  FormGrid,
  FormRow,
  IconButton,
  moveRowFocus,
  Notice,
  Segmented,
  Select,
  ToolbarButton,
  useAction,
  useFocusScope,
  ViewStatus,
} from "./kit";
import { FolderField } from "./FolderField";
import { WINDOWS } from "./platform";
import "./ssh-connections.css";

/** "user@host:port", leaving out the parts a connection does not have. */
function targetLabel(c: Pick<SshConnection, "user" | "host" | "port">): string {
  return `${c.user ? `${c.user}@` : ""}${c.host}${c.port != null ? `:${c.port}` : ""}`;
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
 * hold one line; these rows carry a muted target line and a hover Edit). */
export function SshConnectionList(props: {
  connections: SshConnection[];
  loading: boolean;
  error: string;
  selectedId: string;
  onSelect: (id: string) => void;
  onNew: () => void;
  onEdit: (connection: SshConnection) => void;
  onRetry: () => void;
}): JSX.Element {
  const { connections, loading, error, selectedId, onSelect, onNew, onEdit, onRetry } = props;
  return (
    <nav className="ssh-connections" aria-label="SSH connections">
      <div className="ssh-connections-head">
        <h2>SSH connections</h2>
        <ToolbarButton icon="plus" onClick={onNew}>New</ToolbarButton>
      </div>
      <ViewStatus loading={loading ? "Loading SSH connections…" : undefined} error={error || undefined} onRetry={onRetry} />
      {!loading && !error && connections.length === 0 && (
        <EmptyState
          icon="server"
          title="No SSH connections"
          action={<ToolbarButton primary icon="plus" onClick={onNew}>New SSH connection</ToolbarButton>}
        >
          Save an SSH connection to move files between this computer and a server.
        </EmptyState>
      )}
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
                    onKeyDown={(e) => {
                      const next = moveRowFocus(e);
                      if (next?.dataset.rowKey) onSelect(next.dataset.rowKey);
                    }}
                  >
                    <span className="ssh-connection-name">{c.name}</span>
                    <span className="ssh-connection-target">{targetLabel(c) || "No host set"}</span>
                  </button>
                  <IconButton icon="edit" className="ssh-connection-edit" label={`Edit ${c.name}`} onClick={() => onEdit(c)} />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </nav>
  );
}

type Problems = { name?: string; host?: string; port?: string; key_path?: string };

/** New SSH connection (no *initial*) or Edit <name>: fields, secrets, Test,
 * Save and Delete, plus the host-key trust sheet when the server asks. */
export function SshConnectionSheet(props: {
  /** Absent: New SSH connection. */
  initial?: SshConnection;
  /** Saved database connections; those with ssh_host pre-fill the form ("Start from"). */
  profiles: Profile[];
  existingNames: string[];
  onClose: () => void;
  onSaved: (connection: SshConnection) => void;
  onDeleted: (id: string) => void;
}): JSX.Element {
  const { initial, profiles, existingNames, onClose, onSaved, onDeleted } = props;
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = "ssh-conn-sheet-title";
  const [name, setName] = useState(initial?.name || "");
  // Name follows user@host until the person types one of their own.
  const [nameEdited, setNameEdited] = useState(!!initial);
  const [host, setHost] = useState(initial?.host || "");
  const [port, setPort] = useState(initial?.port != null ? String(initial.port) : "");
  const [user, setUser] = useState(initial?.user || "");
  const [auth, setAuth] = useState<SshAuth>(initial?.auth || "agent");
  const [keyPath, setKeyPath] = useState(initial?.key_path || "");
  const [keyMode, setKeyMode] = useState<"pick" | "type">("pick");
  const [remoteDir, setRemoteDir] = useState(initial?.remote_dir || "");
  const [localDir, setLocalDir] = useState(initial?.local_dir || "");
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
    if (result.ok) {
      onSaved(result.value.connection);
      return;
    }
    if (result.error && sshErrorCode(result.error) === "host_key_unknown") setHostKeyTarget(target);
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

  const dismiss = useCallback(() => {
    if (!save.pending && !del.pending) onClose();
  }, [save.pending, del.pending, onClose]);
  useFocusScope(dialog, { open: true, onClose: dismiss });

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
    ...(keys || []).map((k): [string, string] => [k.path, `${k.path}${k.type ? ` · ${k.type}` : ""}${k.comment ? ` · ${k.comment}` : ""}`]),
    ...(keyPath && !listedKeys.includes(keyPath) ? [[keyPath, keyPath] as [string, string]] : []),
    ["__type", "Type a path…"],
  ];
  const keyPicker = keyMode === "pick" && keys !== null && keys.length > 0;
  const keyHint = keyPicker
    ? "Private keys in ~/.ssh that have a public key beside them."
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
          <h2 id={titleId}>{initial ? `Edit ${initial.name}` : "New SSH connection"}</h2>
        </div>
      </div>
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
                <Select
                  id="ssh-conn-key"
                  label="Key file"
                  value={keyPath}
                  onChange={(v) => {
                    if (v === "__type") setKeyMode("type");
                    else setKeyPath(v);
                  }}
                  options={keyOptions}
                />
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
      {hostKeyTarget && (
        <HostKeySheet
          target={hostKeyTarget}
          onCancel={() => {
            pendingSecret.current = "";
            setHostKeyTarget(null);
          }}
          onTrusted={() => {
            setHostKeyTarget(null);
            // Trust granted: the test runs again on its own, with the same secret.
            void runTest(pendingSecret.current || undefined);
          }}
        />
      )}
    </dialog>,
    document.body,
  );
}

/** Trust a server's host key: scan it, show the fingerprints, append them to
 * the myflames known_hosts file on "Trust and connect". */
export function HostKeySheet(props: { target: SshTarget; onTrusted: () => void; onCancel: () => void }): JSX.Element {
  const { target, onTrusted, onCancel } = props;
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = "hostkey-sheet-title";
  const [scan, setScan] = useState<HostKeyScan | null>(null);
  const [scanError, setScanError] = useState("");
  const [scanning, setScanning] = useState(true);
  const scanningRef = useRef(false);
  const trust = useAction();

  const scanNow = useCallback(() => {
    if (scanningRef.current) return;
    scanningRef.current = true;
    setScanning(true);
    setScanError("");
    sshHostKeyScan(target).then(
      (result) => setScan(result),
      (error) => setScanError(errorText(error)),
    ).finally(() => {
      scanningRef.current = false;
      setScanning(false);
    });
  }, [target]);
  useEffect(() => {
    scanNow();
  }, [scanNow]);

  const dismiss = useCallback(() => {
    if (!trust.pending) onCancel();
  }, [trust.pending, onCancel]);
  useFocusScope(dialog, { open: true, onClose: dismiss });

  async function runTrust() {
    if (!scan) return;
    const result = await trust.run(() => sshHostKeyTrust(scan.token), { pending: "Trusting…", write: true, done: "" });
    if (result.ok) onTrusted();
  }

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
          <h2 id={titleId}>{scan ? `Trust ${scan.host}?` : "Trust this server?"}</h2>
        </div>
      </div>
      <div className="sheet-body">
        {scanning && (
          <p className="capture-status">
            <span className="spinner" aria-hidden="true" /> Reading the server's host key…
          </p>
        )}
        {!scanning && scanError && (
          <>
            <Notice variant="error">{scanError}</Notice>
            <p>
              <button type="button" className="button hostkey-retry" onClick={scanNow}>Try again</button>
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
      </div>
      <div className="dialog-actions sheet-actions">
        <button type="button" className="button" disabled={trust.pending} onClick={dismiss}>Cancel</button>
        <ActionButton
          primary
          className="hostkey-trust-button"
          busy={trust.pending}
          busyLabel="Trusting…"
          disabled={!scan}
          onClick={() => void runTrust()}
        >
          Trust and connect
        </ActionButton>
      </div>
    </dialog>,
    document.body,
  );
}
