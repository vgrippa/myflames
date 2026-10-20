// The lists of views of the Server and Workload places, apart from Server.tsx
// so the shell (sidebar, command palette) can name them without loading the
// Server chunk (lazy.tsx).
import type { IconName } from "./components";
import type { ServerKind } from "./session";

export const SERVER_VIEWS: { id: ServerKind; label: string; icon: IconName; group: string }[] = [
  { id: "health", label: "Health", icon: "health", group: "Overview" },
  { id: "innodb", label: "InnoDB", icon: "engine", group: "Overview" },
  { id: "errorlog", label: "Error log", icon: "errorlog", group: "Overview" },
  { id: "locks", label: "Locks", icon: "lock", group: "Contention" },
  { id: "indexes", label: "Indexes", icon: "index", group: "Schema" },
  { id: "tables", label: "Tables", icon: "table", group: "Schema" },
  { id: "diagram", label: "Database diagram", icon: "diagram", group: "Schema" },
  { id: "config", label: "Parameters", icon: "configuration", group: "Setup" },
  { id: "instruments", label: "Instruments", icon: "instruments", group: "Setup" },
  { id: "maintenance", label: "Maintenance", icon: "maintenance", group: "Setup" },
  { id: "accounts", label: "Users and roles", icon: "users", group: "Security" },
];
export const WORKLOAD_VIEWS: { id: ServerKind; label: string; icon: IconName; group: string }[] = [
  { id: "top", label: "Top statements", icon: "statements", group: "Queries" },
  { id: "sessions", label: "Processlist", icon: "sessions", group: "Queries" },
  { id: "waits", label: "Waits", icon: "waits", group: "Resources" },
  { id: "io", label: "I/O", icon: "io", group: "Resources" },
];
export const WORKLOAD_KINDS: ServerKind[] = ["top", "sessions", "waits", "io"];
export const ALL_SERVER_VIEWS = [...SERVER_VIEWS, ...WORKLOAD_VIEWS];
export const SERVER_GROUPS = ["Overview", "Contention", "Schema", "Setup", "Security"];
export const WORKLOAD_GROUPS = ["Queries", "Resources"];
