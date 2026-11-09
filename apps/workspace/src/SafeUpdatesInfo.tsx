import { InfoPopover } from "./kit";

/** The official documentation of sql_safe_updates. The desktop app opens
 * these in the default browser through its link allowlist
 * (apps/desktop/backend.cjs `externalLink`, tested in backend.test.cjs). */
export const SAFE_UPDATES_DOCS = {
  mysql: "https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html#sysvar_sql_safe_updates",
  mariadb: "https://mariadb.com/docs/server/server-management/variables-and-modes/server-system-variables#sql_safe_updates",
} as const;

/** The ⓘ next to Safe updates in Run options. What the server refuses is
 * read from the source, not the manuals:
 * - MySQL: a single-table UPDATE or DELETE with no index usable for its WHERE
 *   and no LIMIT (sql_update.cc, sql_delete.cc: `quick_keys` empty); a
 *   multiple-table one that scans a table it changes (`CheckSqlSafeUpdate`).
 * - MariaDB: the same for UPDATE and DELETE (no quick select and no LIMIT),
 *   plus a DELETE whose WHERE is missing or constant even with a LIMIT
 *   (sql_delete.cc `const_cond`), and a multiple-table one with a full scan
 *   of any joined table (`error_if_full_join`) or, DELETE only, no WHERE
 *   (sql_parse.cc `multi_delete_precheck`, even under EXPLAIN).
 * Error 1175 (ER_UPDATE_WITHOUT_KEY_IN_SAFE_MODE) is raised before any row
 * changes; MySQL never refuses EXPLAIN. The server variable does not limit
 * SELECT: the mysql client's --safe-updates also sets sql_select_limit and
 * max_join_size, which myflames does not set. */
export function SafeUpdatesInfo() {
  return (
    <InfoPopover label="About safe updates">
      <p>
        Stops an UPDATE or DELETE from changing every row by mistake: the server refuses one whose WHERE cannot use
        an index, unless it has a LIMIT (error 1175, nothing changes). MariaDB also refuses a DELETE without a WHERE,
        even with a LIMIT. SELECT is not limited.
      </p>
      <p>
        It sets <code>sql_safe_updates</code> for this tab's session; on by default for Production, off for
        Development. Clear the box to turn it off for this tab (Production asks first).
      </p>
      <p className="popover-links">
        Documentation:{" "}
        <a href={SAFE_UPDATES_DOCS.mysql} target="_blank" rel="noopener noreferrer"
          aria-label="sql_safe_updates in the MySQL documentation (opens in your browser)">MySQL</a>
        {" · "}
        <a href={SAFE_UPDATES_DOCS.mariadb} target="_blank" rel="noopener noreferrer"
          aria-label="sql_safe_updates in the MariaDB documentation (opens in your browser)">MariaDB</a>
      </p>
    </InfoPopover>
  );
}
