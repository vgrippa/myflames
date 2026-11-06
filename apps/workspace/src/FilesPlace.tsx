// The Files place: saved SSH connections on the left (SshConnections.tsx)
// and, for the one selected, the two-pane browser with its transfer queue
// (FileBrowser.tsx). It does not use the workspace's database connection:
// SSH connections are their own saved objects (filesApi.ts). The selection is
// remembered per viewer in localStorage; the list reads once on the first
// visit and again after a save or delete.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Profile } from "./api";
import { FileBrowser } from "./FileBrowser";
import { HostKeySheet, SshConnectionList, SshConnectionSheet } from "./SshConnections";
import { errorText } from "./kit";
import { sshConnections, type SshConnection } from "./filesApi";
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
  // The sheet: null closed, "new", or the connection being edited.
  const [sheet, setSheet] = useState<null | "new" | SshConnection>(null);
  const [hostKey, setHostKey] = useState<null | { id: string; retry: () => void }>(null);
  const request = useRef<AbortController | null>(null);

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
  // A remembered id that no longer exists falls back to the first connection.
  const selected = connections.find((c) => c.id === selectedId) || (loading ? undefined : connections[0]);

  return (
    <div className="files-place">
      <SshConnectionList
        connections={connections}
        loading={loading}
        error={error}
        selectedId={selected?.id || ""}
        onSelect={select}
        onNew={() => setSheet("new")}
        onEdit={(connection) => setSheet(connection)}
        onRetry={() => void load()}
      />
      <section className="files-place-main" aria-label={selected ? `Files on ${selected.name}` : "Files"}>
        {selected ? (
          <FileBrowser
            key={selected.id}
            connection={selected}
            onEditConnection={() => setSheet(selected)}
            onHostKeyUnknown={(retry) => setHostKey({ id: selected.id, retry })}
          />
        ) : null}
      </section>
      {sheet && (
        <SshConnectionSheet
          initial={sheet === "new" ? undefined : sheet}
          profiles={profiles}
          existingNames={connections.map((c) => c.name)}
          onClose={() => setSheet(null)}
          onSaved={(saved) => {
            setSheet(null);
            setConnections((list) =>
              [...list.filter((c) => c.id !== saved.id), saved].sort((a, b) => a.name.localeCompare(b.name)),
            );
            select(saved.id);
          }}
          onDeleted={(id) => {
            setSheet(null);
            setConnections((list) => list.filter((c) => c.id !== id));
            if (id === selectedId) select("");
          }}
        />
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
