// The command palette's workspace commands. The palette is lazy (it renders
// only while open), so its command list builds inside its chunk instead of the
// entry; the shell passes the actions and the palette builds the list with its
// own query text, in the same order the shell declared it.
import type { WorkspaceCommand } from "./CommandPalette";
import type { QueryLabHandle } from "./QueryLab";
import type { SettingsSection } from "./SettingsPage";
import type { Place } from "./Sidebar";
import type { ServerKind } from "./session";
import { SERVER_VIEWS, WORKLOAD_VIEWS } from "./serverViews";
import { THEME_CHOICES, applyTheme } from "./themes";

/** What a palette command can do: the shell's navigation and dialog actions. */
export type WorkspaceActions = {
  go: (next: Place) => void;
  setModeCompare: () => void;
  chooseServerView: (viewId: ServerKind) => void;
  openFullAnalysis: () => void;
  openImport: () => void;
  openAuditLog: () => void;
  openAccount: () => void;
  newConnection: () => void;
  openSettings: (section?: SettingsSection) => void;
  withLab: (call: (lab: QueryLabHandle) => void) => void;
};

export function buildWorkspaceCommands(actions: WorkspaceActions, queryText: string): WorkspaceCommand[] {
  const { go, withLab } = actions;
  return [
    // Places in the sidebar's order, then what belongs to one of them.
    { id: "place-home", label: "Home", group: "Places", icon: "home", run: () => go("home") },
    { id: "place-server", label: "Server", group: "Places", icon: "server", keywords: "database status", run: () => go("server") },
    { id: "place-workload", label: "Workload", group: "Places", icon: "statements", keywords: "queries top processes waits io", run: () => go("workload") },
    { id: "place-topology", label: "Topology", group: "Places", icon: "topology", keywords: "connection group members parameters accounts plugins map servers fleet drift", run: () => go("topology") },
    { id: "place-replication", label: "Replication", group: "Places", icon: "replication", keywords: "slave replica topology channels lag gtid", run: () => go("replication") },
    { id: "place-cluster", label: "Cluster", group: "Places", icon: "cluster", keywords: "galera percona xtradb pxc innodb cluster group replication wsrep members quorum router", run: () => go("cluster") },
    { id: "place-proxysql", label: "ProxySQL", group: "Places", icon: "proxy", keywords: "proxysql proxy hostgroups mysql_servers query rules digest connection pool admin", run: () => go("proxysql") },
    { id: "place-readyset", label: "Readyset", group: "Places", icon: "readyset", keywords: "readyset cache shallow deep querypilot resnapshot cdc replication", run: () => go("readyset") },
    { id: "place-tuning", label: "Tuning", group: "Places", icon: "tuning", keywords: "mysql tuning mariadb tuning advisor health score optimization", run: () => go("tuning") },
    { id: "place-files", label: "File transfer", group: "Places", icon: "files", keywords: "files sftp ssh transfer upload download scp filezilla logs dump", run: () => go("files") },
    { id: "place-query", label: "Query editor", group: "Places", icon: "query", keywords: "sql run query editor", run: () => go("query") },
    { id: "place-digest", label: "Query digest", group: "Places", icon: "analysis", keywords: "pt-query-digest digest profile statements workload slow queries", run: () => go("digest") },
    { id: "place-diagram", label: "Database diagram", group: "Places", icon: "diagram", keywords: "schema diagram er erd mermaid tables relationships foreign keys", run: () => go("diagram") },
    { id: "place-schema", label: "Schema versions", group: "Places", icon: "branches", keywords: "schema version versions snapshot compare diff migration drift ddl alter", run: () => go("schema") },
    { id: "place-plans", label: "Plans", group: "Places", icon: "plan", keywords: "explain analyze flame graph visual explain", run: () => go("plans") },
    { id: "place-investigations", label: "Investigations", group: "Places", icon: "investigation", keywords: "saved library notes", run: () => go("investigations") },
    { id: "place-learn", label: "Learn", group: "Places", icon: "learn", keywords: "lessons teach algorithms", run: () => go("learn") },
    // The Full analysis is the Tuning place's second tab (Server.tsx choose).
    { id: "tuning-report", label: "Full analysis", group: "Tuning", icon: "analysis", keywords: "full analysis report findings tuner", run: () => { go("tuning"); actions.openFullAnalysis(); } },
    // Compare is a mode of Plans (vocabulary), not a place.
    { id: "plans-compare", label: "Compare", group: "Plans", icon: "compare", keywords: "diff compare plans before after", run: () => actions.setModeCompare() },
    ...SERVER_VIEWS.map((v) => ({
      id: `server-${v.id}`,
      label: v.label,
      group: `Server · ${v.group}`,
      icon: v.icon,
      keywords: `${v.label} ${v.group} server`,
      run: () => { go("server"); actions.chooseServerView(v.id); },
    })),
    ...WORKLOAD_VIEWS.map((v) => ({
      id: `workload-${v.id}`,
      label: v.label,
      group: `Workload · ${v.group}`,
      icon: v.icon,
      keywords: `${v.label} ${v.group} workload`,
      run: () => { go("workload"); actions.chooseServerView(v.id); },
    })),
    ...THEME_CHOICES.map(theme => ({
      id: `theme-${theme.id}`, label: `Theme: ${theme.name}`, group: "Appearance",
      icon: "settings" as const,
      run: () => applyTheme(theme.id),
    })),
    { id: "action-import", label: "Import plan", group: "Actions", icon: "import", keywords: "open json explain analyze", run: actions.openImport },
    { id: "action-audit", label: "Audit log", group: "Actions", icon: "audit", keywords: "writes changes kill declined refused log", run: actions.openAuditLog },
    { id: "action-account", label: "Account and privileges", group: "Actions", icon: "key", keywords: "privileges grants roles user account connected as permissions show grants", run: () => actions.openAccount() },
    { id: "action-new-connection", label: "New connection", group: "Actions", icon: "plus", keywords: "add connection server host profile new", run: actions.newConnection },
    // Settings is a page (the gear, ⌘,): the palette's one Settings action
    // opens it, at the Query editor section when that is what was asked for.
    { id: "action-settings", label: "Settings", group: "Actions", icon: "settings", keywords: "settings preferences appearance theme dark light query editor startup restore updates news client tools about zoom", run: () => actions.openSettings(/editor/.test(queryText.toLowerCase()) ? "editor" : "appearance") },
    // Query editor actions on the current tab (QueryLabHandle).
    { id: "query-rename-tab", label: "Rename query tab", group: "Query editor", icon: "edit", keywords: "rename tab name title query editor", run: () => { go("query"); withLab((lab) => lab.renameTab()); } },
    { id: "query-format-sql", label: "Format SQL", group: "Query editor", icon: "format", keywords: "format indent beautify pretty sql query editor", run: () => { go("query"); withLab((lab) => lab.formatSql()); } },
    { id: "query-find", label: "Find", group: "Query editor", icon: "search", keywords: "find search text sql query editor", run: () => { go("query"); withLab((lab) => lab.runEditorCommand("find")); } },
    { id: "query-replace", label: "Find and replace", group: "Query editor", icon: "replace", keywords: "find replace search text sql query editor", run: () => { go("query"); withLab((lab) => lab.runEditorCommand("replace")); } },
    { id: "query-go-to-line", label: "Go to line", group: "Query editor", icon: "query", keywords: "go to line jump sql query editor", run: () => { go("query"); withLab((lab) => lab.runEditorCommand("goToLine")); } },
    { id: "query-save", label: "Save query…", group: "Query editor", icon: "plus", keywords: "save saved query favorite bookmark snippet folder keep query editor", run: () => { go("query"); withLab((lab) => lab.saveQuery()); } },
  ];
}
