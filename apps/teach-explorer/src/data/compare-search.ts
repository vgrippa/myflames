/** URLSearchParams decodes values once; malformed escapes remain plain text. */
export function readCompareSelection(
  search: string,
  available: readonly string[],
  fallback: readonly string[],
): string[] {
  const value = new URLSearchParams(search).get("compare");
  if (value === null) return [...fallback];
  return [...new Set(value.split(","))].filter((name) => available.includes(name));
}
