// Which server a report, a selection, or a request belongs to.
//
// Every Server view stores its report together with the key of the
// connection it was read from, and every read remembers the connection and
// the parameters it was started with. A response whose connection is no
// longer the one in use, or that a newer read replaced, is dropped. A report
// from another connection stays on screen, greyed, with every action on it
// turned off until it is refreshed on the connection in use.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type Connection } from "./api";

/** A report and the connection it was read from. */
export type Bound<T> = {
  /** connectionKey() of the connection the report was read from. */
  key: string;
  /** What the screen calls that connection ("Production", "app@db1:3306"). */
  label: string;
  report: T;
};

type Identity = Pick<Connection, "host" | "port" | "user" | "ssl_mode"> &
  Partial<Pick<Connection, "profile_id" | "ssl_ca" | "name" | "ssh_host" | "ssh_user" | "ssh_port">> & {
    /** Write safety of a saved connection (dbops.py): "production" (the default) or "development". */
    type?: string;
    /** Refuses every write. */
    read_only?: boolean;
  };

/** The identity of a connection: saved connection id, host, port, user and
 * TLS settings, the SSH target for a connection through SSH (sshKey), then
 * its type and read-only flag when they are not the defaults (production,
 * read-write), so changing them rebinds every report and confirmation. A
 * connection without SSH keeps the key it had before SSH existed. Never the
 * password, and not the default schema (the same server answers either
 * way). Python twin: dbops.connection_identity. */
/** White space and the invisible characters a paste brings along (zero-width
 * spaces and joiners, word joiner, byte order mark), as live.py trims them. */
const BLANK = "[\\s\\x1c-\\x1f\\u0085\\u200b\\u200c\\u200d\\u2060\\ufeff]";
const AROUND = new RegExp(`^${BLANK}+|${BLANK}+$`, "g");
const AFTER = new RegExp(`${BLANK}+$`);
const INSIDE = new RegExp(BLANK);
/** *text* without pasted spaces at both ends (host names, file paths). */
export function trimPasted(text: string): string {
  return text.replace(AROUND, "");
}
/** *text* without pasted spaces at its end: a database name cannot end with
 * a space, but may start with one. */
export function trimPastedEnd(text: string): string {
  return text.replace(AFTER, "");
}
/** True when a host name still has a space inside it (the server refuses it). */
export function hostHasSpace(host: string): boolean {
  return INSIDE.test(trimPasted(host));
}

/** ["ssh", host, user, port] of a connection through SSH (user "" and port 0
 * when not set), or null. Python twin: dbops.ssh_identity. */
export function sshKey(c: Pick<Identity, "ssh_host" | "ssh_user" | "ssh_port">): (string | number)[] | null {
  const host = trimPasted(c.ssh_host || "");
  return host ? ["ssh", host, trimPasted(c.ssh_user || ""), Number(c.ssh_port) || 0] : null;
}

export function connectionKey(c: Identity): string {
  const key: (string | number | boolean | (string | number)[])[] = [c.profile_id || "", trimPasted(c.host || ""), Number(c.port) || 0, c.user || "", c.ssl_mode || "", trimPasted(c.ssl_ca || "")];
  const ssh = sshKey(c);
  if (ssh) key.push(ssh);
  const type = c.type === "development" ? "development" : "production";
  if (type !== "production" || c.read_only === true) key.push(type, c.read_only === true);
  return JSON.stringify(key);
}

/** The endpoint part of a connectionKey (without the type and read-only
 * flag): two keys with the same endpoint reach the same server through the
 * same saved connection. Python twin: dbops.identity_endpoint. */
export function endpointKey(key: string): string {
  try {
    const parts: unknown = JSON.parse(key);
    if (!Array.isArray(parts)) return key;
    return JSON.stringify(parts.slice(0, parts.length > 6 && Array.isArray(parts[6]) ? 7 : 6));
  } catch {
    return key;
  }
}

/** "Production (app@db1:3306)", or "app@db1:3306" without a name; a
 * connection through SSH adds "via bastion". */
export function connectionLabel(c: Identity): string {
  const via = trimPasted(c.ssh_host || "");
  const target = `${c.user ? `${c.user}@` : ""}${c.host}:${c.port}${via ? ` via ${via}` : ""}`;
  return c.name && c.name !== target ? `${c.name} (${target})` : target;
}

/** One read: the connection it was sent to and the parameters it asked for. */
export type Ticket = {
  id: number;
  key: string;
  label: string;
  params: string;
  message: string;
  connection: Connection;
  /** The view it reads (readView(params)) and its generation (nextGeneration). */
  view: string;
  gen: number;
  /** Stops waiting for the answer (connection switch, a newer read, unmount). */
  controller: AbortController;
};

// ---- Reads shared across the place -------------------------------------------
//
// A view's own Refresh and the place's Refresh all can ask for the same read.
// Both go through sharedRead, so one request answers both, and each view has
// one generation counter, so the latest read of a view wins whichever path
// started it.

type Shared = { promise: Promise<unknown>; controller: AbortController; users: number };
const inflight = new Map<string, Shared>();
const generations = new Map<string, number>();

/** The view a read belongs to: its parameters up to the first ":" ("health:5" is Health). */
export function readView(params: string) {
  const at = params.indexOf(":");
  return at < 0 ? params : params.slice(0, at);
}
/** A new read of *view* starts: any older read of it, from either path, is dropped when it answers. */
export function nextGeneration(view: string) {
  const next = (generations.get(view) || 0) + 1;
  generations.set(view, next);
  return next;
}
/** The generation of the latest read of *view*. */
export function latestGeneration(view: string) {
  return generations.get(view) || 0;
}
/** The read a view asked for last: Refresh all repeats it, or joins it while
 * it runs, instead of rebuilding parameters the view keeps to itself (Locks'
 * Waits only, Tables' Fresh numbers). */
export type ViewRead = {
  key: string;
  params: string;
  request: (c: Connection, signal: AbortSignal) => Promise<unknown>;
};
const viewReads = new Map<string, ViewRead>();
/** The last read *view* asked for on the connection *key*, or null. */
export function lastViewRead(view: string, key: string): ViewRead | null {
  const read = viewReads.get(view);
  return read && read.key === key ? read : null;
}
/** A read of these parameters for the connection *key* is on its way. */
export function readRunning(key: string, params: string) {
  return inflight.has(`${key}|${params}`);
}
/**
 * One request per connection and parameters at a time: a second caller
 * joins the request already on its way instead of sending another. *fresh*
 * starts a new request even so (a re-read after a Kill). A caller's *signal*
 * stops its wait; the request itself is aborted only when no caller waits.
 */
export function sharedRead<R>(
  key: string,
  params: string,
  start: (signal: AbortSignal) => Promise<R>,
  options: { signal?: AbortSignal; fresh?: boolean } = {},
): Promise<R> {
  const id = `${key}|${params}`;
  let entry = options.fresh ? undefined : inflight.get(id);
  if (!entry) {
    const controller = new AbortController();
    const created: Shared = { promise: start(controller.signal), controller, users: 0 };
    const drop = () => {
      if (inflight.get(id) === created) inflight.delete(id);
    };
    created.promise.then(drop, drop);
    inflight.set(id, created);
    entry = created;
  }
  const shared = entry;
  shared.users += 1;
  const signal = options.signal;
  return new Promise<R>((resolve, reject) => {
    let settled = false;
    const release = () => {
      settled = true;
      shared.users -= 1;
      signal?.removeEventListener("abort", stop);
    };
    function stop() {
      if (settled) return;
      release();
      if (shared.users <= 0) {
        shared.controller.abort();
        if (inflight.get(id) === shared) inflight.delete(id);
      }
      reject(new DOMException("The read was stopped.", "AbortError"));
    }
    if (signal?.aborted) return stop();
    signal?.addEventListener("abort", stop);
    (shared.promise as Promise<R>).then(
      (value) => {
        if (settled) return;
        release();
        resolve(value);
      },
      (error) => {
        if (settled) return;
        release();
        reject(error);
      },
    );
  });
}

export type ConnectionReads = ReturnType<typeof useConnectionReads>;

/**
 * Reads of one view, bound to the connection in use.
 *
 * - Single flight per connection and parameters: asking again for the same
 *   read while it runs does nothing.
 * - Latest wins: a read with other parameters (or for another connection)
 *   replaces the one running, whose answer is then dropped.
 * - A response is shown only while its connection is still the one in use
 *   and no newer read replaced it; errors follow the same rule.
 */
export function useConnectionReads(connection: Connection) {
  const key = connectionKey(connection);
  const label = connectionLabel(connection);
  const latest = useRef({ key, label, connection });
  latest.current = { key, label, connection };
  const current = useRef<Ticket | null>(null);
  const seq = useRef(0);
  const [pending, setPending] = useState<Ticket | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string; code: string } | null>(null);
  // When the last read for a connection was shown (ViewStatus announces it).
  const [done, setDone] = useState<{ key: string; at: number } | null>(null);

  /** Stop waiting for *t*: its answer is dropped, and the request is aborted
   * when no one else (Refresh all, the same view) waits for it. */
  const drop = useCallback((t: Ticket | null) => {
    if (!t) return;
    if (current.current?.id === t.id) {
      current.current = null;
      setPending(null);
    }
    t.controller.abort();
  }, []);
  // Switching the connection drops the read on its way: switching back later
  // never shows an answer read before the switch.
  useEffect(() => {
    const t = current.current;
    if (t && t.key !== key) drop(t);
  }, [key, drop]);
  // Unmounted: nothing waits any more.
  useEffect(() => () => drop(current.current), [drop]);

  const live = useCallback(
    (t: Ticket) => current.current?.id === t.id && latest.current.key === t.key && latestGeneration(t.view) === t.gen,
    [],
  );
  /** Start a read for the connection in use, or null when the same read is running. */
  const begin = useCallback((params: string, message: string, force = false): Ticket | null => {
    const running = current.current;
    if (!force && running && running.key === latest.current.key && running.params === params) return null;
    const view = readView(params);
    const t: Ticket = {
      id: ++seq.current,
      params,
      message,
      ...latest.current,
      view,
      gen: nextGeneration(view),
      controller: new AbortController(),
    };
    // A newer read replaces the one running: stop waiting for it.
    running?.controller.abort();
    current.current = t;
    setPending(t);
    setFailure(null);
    return t;
  }, []);
  /** The read ended: clear the loading state if it is still the current one. */
  const finish = useCallback((t: Ticket) => {
    if (current.current?.id !== t.id) return false;
    current.current = null;
    setPending(null);
    return true;
  }, []);
  /**
   * Run one read: *request* gets the connection the read was started with
   * and the signal to pass to api() (aborted when nothing waits any more);
   * *accept* gets the result only while it may still be shown.
   * Returns the bound result, or null when it was skipped or dropped.
   */
  const run = useCallback(
    async <R,>(
      params: string,
      message: string,
      request: (c: Connection, signal: AbortSignal) => Promise<R>,
      accept: (result: Bound<R>) => void,
      options: { force?: boolean; onError?: (message: string) => void } = {},
    ): Promise<Bound<R> | null> => {
      const t = begin(params, message, options.force);
      if (!t) return null;
      try {
        viewReads.set(t.view, { key: t.key, params, request: (c, signal) => request(c, signal) });
        // Joins the same read when Refresh all (or this view) already asked for it.
        const result = await sharedRead(t.key, params, (signal) => request(t.connection, signal), {
          fresh: options.force,
          signal: t.controller.signal,
        });
        if (!live(t)) return null;
        const b: Bound<R> = { key: t.key, label: t.label, report: result };
        accept(b);
        setDone({ key: t.key, at: Date.now() });
        return b;
      } catch (e) {
        if (live(t)) {
          const text = (e as Error).message;
          setFailure({ key: t.key, message: text, code: e instanceof ApiError ? e.code : "" });
          options.onError?.(text);
        }
        return null;
      } finally {
        finish(t);
      }
    },
    [begin, finish, live],
  );

  return {
    /** connectionKey() of the connection in use. */
    key,
    label,
    run,
    begin,
    finish,
    live,
    /** The loading text of the read running for the connection in use. */
    loading: pending && pending.key === key ? pending.message : "",
    /** The last error of a read for the connection in use. */
    error: failure && failure.key === key ? failure.message : "",
    /** Its machine-readable reason (ApiError.code, "" when the server sent none). */
    errorCode: failure && failure.key === key ? failure.code : "",
    /** When the last read for the connection in use was shown (Date.now()), or 0. */
    doneAt: done && done.key === key ? done.at : 0,
    /** Stop waiting for the read running now: nothing is shown from it, the
     * view keeps its report, and the request is aborted unless Refresh all
     * waits for the same read. */
    stop: useCallback(() => drop(current.current), [drop]),
    clearError: useCallback(() => setFailure(null), []),
    /** connectionKey() of the connection in use now (after an await, not the render's). */
    currentKey: useCallback(() => latest.current.key, []),
    /** A read for the connection in use is running (automatic refresh skips a tick). */
    running: useCallback(() => !!current.current && current.current.key === latest.current.key, []),
  };
}
