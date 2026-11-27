// Pure product formatting shared by the shell and lazy Server views.
// A VillageSQL codebase changes capabilities, never the SQL engine.
export function hasPerconaFeatures(flavor?: string | null, base?: string | null): boolean {
  return flavor === "percona" || (flavor === "villagesql" && base === "percona");
}

/** Null for another product; keep a prerelease label and drop a build SHA
 * only after that label. Name the codebase only when the server reported it. */
export function villageSQLLabel(version?: string | null, base?: string | null, product?: string | null): string | null {
  const release = version?.match(/-villagesql-([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?)(?:-[0-9a-f]{7,40})?(?=[ \t\r\n\f\v]|$)/i)?.[1];
  if (!release && product !== "VillageSQL") return null;
  const upstream = version?.match(/^[ \t\r\n\f\v]*([0-9]+\.[0-9]+\.[0-9]+)/)?.[1] || "?";
  const name = `VillageSQL ${release || "?"}`;
  const codebase = base === "percona" ? "Percona Server " : base === "mysql" ? "MySQL " : "";
  return `${name} (${codebase}${upstream})`;
}
