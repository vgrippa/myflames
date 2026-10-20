export function fmt(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

export function plural(n: number, word: string): string {
  return fmt(n) + " " + word + (n === 1 ? "" : "s");
}

export function kib(pageKib: number): string {
  return pageKib + " KiB";
}

export function bytes(n: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return (value >= 100 || unit === 0 ? value.toFixed(0) : value.toFixed(1)) + " " + units[unit];
}

/** Short row count for menus: 1M, 10K, 1B. */
export function short(n: number): string {
  if (n >= 1e9) return n / 1e9 + "B";
  if (n >= 1e6) return n / 1e6 + "M";
  if (n >= 1e3) return n / 1e3 + "K";
  return String(n);
}
