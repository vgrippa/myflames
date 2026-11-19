import type { Connection } from "./api";

/** A member-bound tab whose connection is unavailable keeps its identity,
 * but no endpoint or credentials. The shell and editor must use the same
 * key so neither status nor privileges can borrow the window's connection.
 * Port 0 also refuses a login if a caller misses the editor's UI guard. */
export function unavailableMemberConnection(id: string, name?: string): Connection {
  return {
    profile_id: id, name: name || id, host: "", port: 0,
    user: "", database: "", ssl_mode: "", ssl_ca: "", password: "",
    type: "production", read_only: true,
  };
}
