// These requests belong to the lazy Client tools surface, rather than the
// entry API module. Their request bodies and safety boundaries stay shared.
import { api, type ClientTools, type ClientToolsEngine, type Connection, type ManagedShellStatus } from "./api";

/** The client tools on this computer; with a connection, also the programs the
 * resolver picked for its server. refresh re-probes every copy. */
export const clientTools = (connection?: Connection, refresh = false, signal?: AbortSignal) =>
  api<ClientTools>("client/tools", { ...(connection ? { connection } : {}), ...(refresh ? { refresh: true } : {}) }, signal);
/** Store the choice for one engine: the absolute path of a client program or
 * its folder, "bundled" for the bundled one, or null for Automatic. A refused
 * path answers 400 with a plain sentence. */
export const chooseClientTools = (engine: ClientToolsEngine, path: string | null, connection?: Connection) =>
  api<ClientTools>("client/tools/choose", { engine, path, ...(connection ? { connection } : {}) });
/** Read-only status of the managed MySQL Shell; never starts a download. */
export const shellStatus = () => api<ManagedShellStatus>("client/shell/status", {});
/** Start (or rejoin) the single download flight; a refusal answers 400. */
export const shellInstall = () => api<ManagedShellStatus>("client/shell/install", {});
/** Stop the running download; the id must be the running job's. */
export const shellCancel = (id: string) => api<ManagedShellStatus>("client/shell/cancel", { id });
