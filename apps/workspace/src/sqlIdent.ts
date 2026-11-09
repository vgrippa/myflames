/** MySQL/MariaDB quoted identifiers have no backslash escapes. */
export function bt(name: string): string {
  return "`" + name.replace(/`/g, "``") + "`";
}

export function qualifiedTable(schema: string, table: string): string {
  return `${bt(schema)}.${bt(table)}`;
}

/** String literal for generated editor snippets under the default SQL mode.
 * Escape backslashes before quotes so a trailing backslash cannot consume
 * the closing quote. Identifier quoting must never use this helper. */
export function sqlLiteral(value: string): string {
  return "'" + value.replace(/\\/g, "\\\\").replace(/'/g, "''") + "'";
}

export function indexVisibilitySql(schema: string, table: string, index: string, engine: string, visible = true): string {
  const action = engine.toLowerCase() === "mariadb"
    ? visible ? "IGNORED" : "NOT IGNORED"
    : visible ? "INVISIBLE" : "VISIBLE";
  return `ALTER TABLE ${qualifiedTable(schema, table)} ALTER INDEX ${bt(index)} ${action}`;
}

export function dropIndexSql(schema: string, table: string, index: string): string {
  const action = index.toUpperCase() === "PRIMARY" ? "DROP PRIMARY KEY" : `DROP INDEX ${bt(index)}`;
  return `ALTER TABLE ${qualifiedTable(schema, table)} ${action}`;
}

export function tableMaintenanceSql(operation: "ANALYZE" | "OPTIMIZE" | "CHECK" | "CHECKSUM", schema: string, table: string): string {
  return `${operation} TABLE ${qualifiedTable(schema, table)}${operation === "CHECKSUM" ? " EXTENDED" : ""}`;
}

export function showIndexSql(schema: string, table: string, index: string): string {
  return `SHOW INDEX FROM ${qualifiedTable(schema, table)} WHERE Key_name = ${sqlLiteral(index)};`;
}
