// Only the lazy Extensions view needs this request helper. Keeping it here
// leaves the workspace entry chunk unchanged by extension collection code.
import { api, type Connection, type ExtensionsReport } from "./api";

export function readExtensions(connection: Connection, signal?: AbortSignal): Promise<ExtensionsReport> {
  return api<ExtensionsReport>("live/extensions", { connection }, signal);
}
