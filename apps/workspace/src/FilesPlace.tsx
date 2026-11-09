// The File transfer place: saved SSH connections on the left
// (SshConnections.tsx) and, for the one open, the two-pane browser with its
// transfer queue (FileBrowser.tsx). It does not use the workspace's database
// connection: SSH connections are their own saved objects (filesApi.ts). The
// open connection is remembered per viewer in localStorage; the list reads
// once on the first visit and again after a save or delete. A row's menu
// actions (Edit…, Duplicate, Test connection, Copy address, Delete…) live
// here, so they never select or connect by accident.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Profile } from "./api";
import { FileBrowser } from "./FileBrowser";
import { HostKeySheet, SshConnectionList, SshConnectionSheet, targetLabel } from "./SshConnections";
import {
  ConfirmSheet,
  copyToClipboard,
  EmptyState,
  errorText,
  Notice,
  ToolbarButton,
  useAction,
  ViewStatus,
} from "./kit";
import {
  sshConnectionDelete,
  sshConnections,
  sshConnectionTest,
  sshErrorCode,
  type SshConnection,
  type SshConnectionDraft,
} from "./filesApi";
import "./files-place.css";

const SELECTED_KEY = "myflames.files.selected";

function storedSelection(): string {
  try {
    return localStorage.getItem(SELECTED_KEY) || "";
  } catch {
    return "";
  }
}

export function FilesPlace({ profiles }: { profiles: Profile[] }) {
  const [connections, setConnections] = useState<SshConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState(storedSelection);
  // Set by an explicit delete: nothing else opens until a row is clicked.
  const [cleared, setCleared] = useState(false);
  // The sheet: null closed, "new", a copy draft, or the connection being edited.
  const [sheet, setSheet] = useState<null | "new" | { draft: SshConnectionDraft } | SshConnection>(null);
  const [hostKey, setHostKey] = useState<null | { id: string; retry: () => void }>(null);
  const [deleting, setDeleting] = useState<SshConnection | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const request = useRef<AbortController | null>(null);
  const rowAction = useAction();
  const [rowActionId, setRowActionId] = useState("");
  const del = useAction();

  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError("");
    try {
      const answer = await sshConnections(controller.signal);
      if (controller.signal.aborted) return;
      setConnections(answer.connections);
    } catch (e) {
      if (!controller.signal.aborted) setError(errorText(e));
    } finally {
      if (request.current === controller) {
        request.current = null;
        setLoading(false);
      }
    }
  }, []);
  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, [load]);

  const select = (id: string) => {
    setSelectedId(id);
    try {
      localStorage.setItem(SELECTED_KEY, id);
    } catch {
      /* The selection is a convenience; the place works without storage. */
    }
  };
  // Opening is the only selection: a click, Enter, or the row menu's Open.
  const openConnection = (id: string) => {
    setCleared(false);
    select(id);
  };
  // A remembered id that no longer exists falls back to the first connection;
  // an explicit delete (cleared) never opens another server by itself.
  const selected = connections.find((c) => c.id === selectedId) || (loading || cleared ? undefined : connections[0]);

  const removeConnection = (id: string) => {
    setConnections((list) => list.filter((c) => c.id !== id));
    // The open one may have come from the fallback, not from selectedId.
    if (id === selectedId || id === selected?.id) {
      setCleared(true);
      select("");
    }
  };

  const testConnection = useCallback(
    async (c: SshConnection) => {
      setRowActionId(c.id);
      const result = await rowAction.run((signal) => sshConnectionTest({ id: c.id }, signal), {
        pending: `Testing ${c.name}…`,
        done: (v) => `Connected in ${(v.elapsed_ms / 1000).toFixed(1)} s.`,
        failed: (e) => errorText(e),
      });
      if (!result.ok && result.error && sshErrorCode(result.error) === "host_key_unknown") {
        // The trust prompt takes over: no stale error stays on the row.
        rowAction.reset();
        setHostKey({ id: c.id, retry: () => void testConnection(c) });
      }
    },
    [rowAction],
  );

  const copyAddress = (c: SshConnection) => {
    const text = targetLabel(c);
    if (!text) return;
    setRowActionId(c.id);
    void copyToClipboard(text).then((ok) =>
      rowAction.show(ok ? "done" : "failed", ok ? `Copied ${text}.` : "Could not copy to the clipboard."),
    );
  };

  const duplicate = (c: SshConnection) => {
    const names = new Set(connections.map((x) => x.name.toLowerCase()));
    let copy = `${c.name} copy`;
    for (let n = 2; names.has(copy.toLowerCase()); n += 1) copy = `${c.name} copy ${n}`;
    // A copy never carries the id or the saved secret of its source.
    setSheet({
      draft: {
        name: copy,
        host: c.host,
        port: c.port,
        user: c.user,
        auth: c.auth,
        key_path: c.key_path,
        remote_dir: c.remote_dir,
        local_dir: c.local_dir,
      },
    });
  };

  const askDelete = (c: SshConnection) => {
    setDeleteError("");
    setDeleting(c);
  };
  async function confirmDelete() {
    if (!deleting) return;
    const target = deleting;
    const index = connections.findIndex((c) => c.id === target.id);
    const result = await del.run(() => sshConnectionDelete(target.id), { pending: "Deleting…", write: true, done: "" });
    if (result.ok) {
      setDeleting(null);
      removeConnection(target.id);
      // The row is gone: focus moves to the nearest surviving one, or to New.
      window.requestAnimationFrame(() => {
        const rows = [...document.querySelectorAll<HTMLElement>(".files-place .ssh-connection-main")];
        const next = rows[Math.min(Math.max(index, 0), rows.length - 1)];
        (next || document.querySelector<HTMLElement>(".files-place .ssh-connections-head .button"))?.focus();
      });
    } else {
      setDeleteError(
        result.reason === "unknown"
          ? "No reply arrived. The connection may have been deleted; close this sheet and reload the list."
          : errorText(result.error),
      );
    }
  }

  return (
    <div className="files-place">
      <SshConnectionList
        connections={connections}
        loading={loading}
        error={error}
        selectedId={selected?.id || ""}
        rowStatus={{ id: rowActionId, state: rowAction }}
        onSelect={openConnection}
        onNew={() => setSheet("new")}
        onEdit={(connection) => setSheet(connection)}
        onDuplicate={duplicate}
        onTest={(connection) => void testConnection(connection)}
        onCopyAddress={copyAddress}
        onDelete={askDelete}
        onRetry={() => void load()}
      />
      <section className="files-place-main" aria-label={selected ? `File transfer: ${selected.name}` : "File transfer"}>
        {loading && connections.length === 0 ? (
          <ViewStatus loading="Loading SSH connections…" />
        ) : error && connections.length === 0 ? (
          <ViewStatus error={error} onRetry={() => void load()} />
        ) : connections.length === 0 ? (
          <EmptyState
            icon="files"
            title="No SSH connections"
            action={
              <ToolbarButton primary icon="plus" onClick={() => setSheet("new")}>
                New SSH connection
              </ToolbarButton>
            }
          >
            Save an SSH connection to move files between this computer and a server.
          </EmptyState>
        ) : selected ? (
          <FileBrowser
            key={selected.id}
            connection={selected}
            onEditConnection={() => setSheet(selected)}
            onHostKeyUnknown={(retry) => setHostKey({ id: selected.id, retry })}
          />
        ) : (
          <EmptyState icon="files" title="Choose an SSH connection">
            Open a saved SSH connection from the list to move files between this computer and the server.
          </EmptyState>
        )}
      </section>
      {sheet && (
        <SshConnectionSheet
          initial={typeof sheet === "object" && "id" in sheet ? sheet : undefined}
          draft={typeof sheet === "object" && "draft" in sheet ? sheet.draft : undefined}
          profiles={profiles}
          existingNames={connections.map((c) => c.name)}
          onClose={() => setSheet(null)}
          onSaved={(saved) => {
            setSheet(null);
            setConnections((list) =>
              [...list.filter((c) => c.id !== saved.id), saved].sort((a, b) => a.name.localeCompare(b.name)),
            );
            openConnection(saved.id);
          }}
          onDeleted={(id) => {
            setSheet(null);
            removeConnection(id);
          }}
        />
      )}
      {deleting && (
        <ConfirmSheet
          title={`Delete ${deleting.name}?`}
          description="Its saved password or passphrase is removed from this computer."
          busy={del.pending}
          busyLabel="Deleting…"
          confirmLabel="Delete"
          onCancel={() => setDeleting(null)}
          onConfirm={() => void confirmDelete()}
        >
          {deleteError && <Notice variant="error">{deleteError}</Notice>}
        </ConfirmSheet>
      )}
      {hostKey && (
        <HostKeySheet
          target={{ id: hostKey.id }}
          onCancel={() => setHostKey(null)}
          onTrusted={() => {
            const { retry } = hostKey;
            setHostKey(null);
            retry();
          }}
        />
      )}
    </div>
  );
}
