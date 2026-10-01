import { useEffect, useRef, useState } from "react";
import { api, type Capture, type Connection } from "./api";
const defaults: Connection = {
  name: "Local MySQL",
  host: "127.0.0.1",
  port: 3306,
  user: "",
  password: "",
  database: "",
  ssl_mode: "PREFERRED",
};
type Profile = Connection & { id: string };
type Tab = { id: string; name: string; sql: string; parameters: string };
type Job = {
  id: string;
  state: string;
  progress: number;
  repeat: number;
  error: string;
  result: Capture | null;
};
export function QueryLab({
  onCapture,
}: {
  onCapture: (capture: Capture, name: string) => Promise<void>;
}) {
  const [connection, setConnection] = useState(defaults);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profileId, setProfileId] = useState("");
  const [schemas, setSchemas] = useState<string[]>([]);
  const [tabs, setTabs] = useState<Tab[]>([
    { id: "1", name: "Query 1", sql: "SELECT 1", parameters: "{}" },
  ]);
  const [tabId, setTabId] = useState("1");
  const tab = tabs.find((t) => t.id === tabId)!;
  const [timeout, setTimeoutSeconds] = useState(30);
  const [repeat, setRepeat] = useState(1);
  const [trace, setTrace] = useState(false);
  const [conditions, setConditions] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [testing, setTesting] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [starting, setStarting] = useState(false);
  const busy =
    starting || (!!job && (job.state === "queued" || job.state === "running"));
  const captureName = useRef("");
  const received = useRef("");
  async function refresh() {
    const r = await api<{ items: Profile[] }>("profiles/list", {});
    setProfiles(r.items);
  }
  useEffect(() => {
    refresh().catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.state)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const next = await api<Job>(
          "live/status",
          { id: job.id },
          controller.signal,
        );
        setJob(next);
        if (
          next.state === "complete" &&
          next.result &&
          received.current !== next.id
        ) {
          received.current = next.id;
          setStatus("Capture complete. Open the plan explorer to investigate.");
          await onCapture(next.result, captureName.current);
        }
        if (next.error) setError(next.error);
      } catch (e) {
        if (!controller.signal.aborted) {
          setError((e as Error).message);
          setJob({ ...job, state: "disconnected" });
        }
      }
    }, 500);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [job, onCapture]);
  function edit<K extends keyof Connection>(key: K, value: Connection[K]) {
    setConnection((c) => ({ ...c, [key]: value }));
  }
  function changeTab(changes: Partial<Tab>) {
    setTabs((ts) => ts.map((t) => (t.id === tabId ? { ...t, ...changes } : t)));
  }
  async function run(mode: string) {
    setError("");
    setStatus("");
    setStarting(true);
    try {
      const parameters: unknown = JSON.parse(tab.parameters);
      captureName.current = tab.name;
      const result = await api<{ id: string }>("live/start", {
        connection,
        query: tab.sql,
        parameters,
        mode,
        timeout,
        repeat,
        trace,
        conditions,
      });
      setJob({
        id: result.id,
        state: "queued",
        progress: 0,
        repeat: mode === "explain" ? 1 : repeat,
        error: "",
        result: null,
      });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStarting(false);
    }
  }
  return (
    <section className="query-lab">
      <div className="page-heading">
        <div>
          <div className="eyebrow">LIVE WORKSPACE</div>
          <h1>Query laboratory</h1>
          <p>Capture a plan, test a change, and keep the evidence.</p>
        </div>
      </div>
      <div className="lab-grid">
        <aside className="card connection-panel">
          <h2>Connection</h2>
          <label className="field">
            Saved profile
            <select
              value={profileId}
              onChange={(e) => {
                setProfileId(e.target.value);
                const p = profiles.find((p) => p.id === e.target.value);
                setConnection(p ? { ...p, password: "" } : defaults);
                setSchemas([]);
                setStatus("");
              }}
            >
              <option value="">New connection</option>
              {profiles.map((p) => (
                <option value={p.id} key={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Profile name
            <input
              value={connection.name}
              onChange={(e) => edit("name", e.target.value)}
            />
          </label>
          <label className="field">
            Host
            <input
              value={connection.host}
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
                value={connection.port}
                onChange={(e) => edit("port", +e.target.value)}
              />
            </label>
            <label className="field">
              User
              <input
                autoComplete="username"
                value={connection.user}
                onChange={(e) => edit("user", e.target.value)}
              />
            </label>
          </div>
          <label className="field">
            Password
            <input
              type="password"
              autoComplete="current-password"
              value={connection.password || ""}
              onChange={(e) => edit("password", e.target.value)}
            />
          </label>
          <label className="field">
            Schema
            <input
              list="schema-list"
              value={connection.database}
              onChange={(e) => edit("database", e.target.value)}
            />
            <datalist id="schema-list">
              {schemas.map((s) => (
                <option key={s} value={s} />
              ))}
            </datalist>
          </label>
          <details>
            <summary>TLS settings</summary>
            <label className="field">
              TLS mode
              <select
                value={connection.ssl_mode}
                onChange={(e) => edit("ssl_mode", e.target.value)}
              >
                {[
                  "PREFERRED",
                  "REQUIRED",
                  "VERIFY_CA",
                  "VERIFY_IDENTITY",
                  "DISABLED",
                ].map((m) => (
                  <option key={m}>{m}</option>
                ))}
              </select>
            </label>
            <label className="field">
              CA file path
              <input
                value={connection.ssl_ca || ""}
                onChange={(e) => edit("ssl_ca", e.target.value)}
              />
            </label>
          </details>
          <div className="button-row">
            <button
              className="button"
              disabled={testing || busy}
              onClick={async () => {
                setTesting(true);
                setError("");
                try {
                  const r = await api<{ version: string; schemas: string[] }>(
                    "connection/test",
                    { connection },
                  );
                  setSchemas(r.schemas);
                  setStatus(`Connected · ${r.version}`);
                } catch (e) {
                  setError((e as Error).message);
                } finally {
                  setTesting(false);
                }
              }}
            >
              {testing ? "Connecting…" : "Test connection"}
            </button>
            <button
              className="button"
              disabled={busy}
              onClick={async () => {
                try {
                  const p = await api<Profile>("profiles/save", {
                    id: profileId || undefined,
                    value: connection,
                  });
                  setProfileId(p.id);
                  await refresh();
                  setStatus(
                    "Connection profile saved. Password stays in memory.",
                  );
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              Save profile
            </button>
          </div>
          {profileId && (
            <button
              className="text-button"
              onClick={async () => {
                try {
                  await api("profiles/delete", { id: profileId });
                  setProfileId("");
                  await refresh();
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              Delete profile
            </button>
          )}
          <p className="hint">
            Credentials stay in memory. Connection profiles store settings only.
            Requires a local mysql or mariadb client.
          </p>
        </aside>
        <div className="editor-panel card">
          <div className="query-tabs" role="tablist">
            {tabs.map((t) => (
              <button
                role="tab"
                aria-selected={t.id === tabId}
                key={t.id}
                onClick={() => setTabId(t.id)}
              >
                {t.name}
              </button>
            ))}
            <button
              aria-label="New query tab"
              onClick={() => {
                const id = crypto.randomUUID();
                setTabs((ts) => [
                  ...ts,
                  {
                    id,
                    name: `Query ${ts.length + 1}`,
                    sql: "SELECT 1",
                    parameters: "{}",
                  },
                ]);
                setTabId(id);
              }}
            >
              +
            </button>
          </div>
          <div className="editor-title">
            <input
              aria-label="Experiment name"
              value={tab.name}
              onChange={(e) => changeTab({ name: e.target.value })}
            />
            {tabs.length > 1 && (
              <button
                className="text-button"
                onClick={() => {
                  const rest = tabs.filter((t) => t.id !== tabId);
                  setTabs(rest);
                  setTabId(rest[0].id);
                }}
              >
                Close tab
              </button>
            )}
          </div>
          <textarea
            aria-label="SQL editor"
            className="sql-editor"
            spellCheck={false}
            value={tab.sql}
            onChange={(e) => changeTab({ sql: e.target.value })}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                if (!busy) run("explain");
              }
            }}
          />
          <div className="execution-options">
            <label className="field">
              Time limit per run
              <input
                type="number"
                min="1"
                max="300"
                value={timeout}
                onChange={(e) => setTimeoutSeconds(+e.target.value)}
              />
            </label>
            <label className="field">
              Analysis runs
              <input
                type="number"
                min="1"
                max="10"
                value={repeat}
                onChange={(e) => setRepeat(+e.target.value)}
              />
            </label>
            <label className="check-field">
              <input
                type="checkbox"
                checked={trace}
                onChange={(e) => setTrace(e.target.checked)}
              />{" "}
              Capture optimizer trace
            </label>
          </div>
          <details className="editor-parameters">
            <summary>Parameters and execution conditions</summary>
            <label className="field">
              Named parameters · JSON object
              <textarea
                aria-label="Query parameters"
                value={tab.parameters}
                onChange={(e) => changeTab({ parameters: e.target.value })}
              />
            </label>
            <p className="hint">
              Use :name in SQL, for example WHERE id = :id with {`{"id": 42}`}.
            </p>
            <label className="field">
              Conditions
              <input
                placeholder="Warm cache, local dataset, index experiment…"
                value={conditions}
                onChange={(e) => setConditions(e.target.value)}
              />
            </label>
          </details>
          <div className="editor-actions">
            <button
              className="button"
              disabled={busy}
              onClick={() => run("explain")}
            >
              Explain
            </button>
            <button
              className="button primary"
              disabled={busy}
              onClick={() => run("analyze")}
            >
              Run analysis
            </button>
            {busy && (
              <button
                className="button danger"
                onClick={async () => {
                  if (job)
                    try {
                      await api("live/cancel", { id: job.id });
                    } catch (e) {
                      setError((e as Error).message);
                    }
                }}
              >
                Cancel capture
              </button>
            )}
            <span className="hint">
              Explain estimates the plan. Run analysis executes the SELECT.
            </span>
          </div>
          {busy && (
            <p role="status" className="capture-status">
              <span className="spinner" /> Capturing {job?.progress || 0} /{" "}
              {job?.repeat || 1} runs…
            </p>
          )}
          {status && (
            <p role="status" className="notice">
              {status}
            </p>
          )}
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
