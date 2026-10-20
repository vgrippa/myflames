// Home news (myflames/news.py): headlines from fixed official feeds, read by
// the Python backend, never by the page. The Settings switch is shared by the
// Settings popover and Home through one small store, so turning it off hides
// the section at once and no /api/news request follows.
import { useEffect, useState } from "react";
import { api } from "./api";

export type NewsItem = {
  title: string;
  /** HTTPS, on the source's official site (checked by the backend). */
  link: string;
  source: string;
  source_id: string;
  engine: "mysql" | "mariadb";
  /** UTC ISO 8601, or null when the feed gave no readable date. */
  published: string | null;
  summary: string;
};
export type NewsSource = {
  id: string;
  name: string;
  engine: string;
  url: string;
  fetched_at: string | null;
  checked_at: string | null;
  items: number;
  error?: string;
};
export type NewsReport = {
  schema_version: "news-1.0";
  enabled: boolean;
  items: NewsItem[];
  /** When the headlines shown were read (the oldest source), or null. */
  fetched_at: string | null;
  /** The last read of at least one source failed: some headlines come from the cache. */
  stale: boolean;
  error?: string;
  offline?: boolean;
  sources: NewsSource[];
};

export const readNews = (refresh: boolean, signal?: AbortSignal) =>
  api<NewsReport>("news", { refresh }, signal);

/** null until the stored setting is known; Home reads no news meanwhile. */
let enabled: boolean | null = null;
let loading: Promise<boolean> | null = null;
const listeners = new Set<(value: boolean | null) => void>();
function publish(value: boolean | null) {
  enabled = value;
  listeners.forEach((listener) => listener(value));
}
let writes = 0;
function loadSetting() {
  if (!loading) {
    const before = writes;
    loading = api<{ enabled: boolean }>("news/settings", {}).then(
      (r) => {
        // A change made while this read was on its way wins over it.
        if (writes === before) publish(r.enabled === true);
        return enabled === true;
      },
      (e) => { loading = null; throw e; },
    );
  }
  return loading;
}
// Writes go out one after another, in the order they were made, so the
// stored value is always the last choice, even across popover openings.
let queue: Promise<unknown> = Promise.resolve();
/** Turn Home news on or off (stored in the workspace store). Only the latest
 * write's answer is published; when it fails, the stored value is read again. */
export function setNewsEnabled(on: boolean): Promise<void> {
  const mine = ++writes;
  const sent = queue.then(() => api<{ enabled: boolean }>("news/settings", { enabled: on }));
  queue = sent.catch(() => undefined);
  return sent.then(
    (r) => {
      if (mine !== writes) return;
      loading = Promise.resolve(r.enabled === true);
      publish(r.enabled === true);
    },
    (e) => {
      if (mine === writes) {
        loading = null;
        loadSetting().catch(() => { /* the switch keeps what it shows */ });
      }
      throw e;
    },
  );
}
/** The Settings switch: null while loading or when it could not be read. */
export function useNewsEnabled(): boolean | null {
  const [value, setValue] = useState(enabled);
  useEffect(() => {
    listeners.add(setValue);
    setValue(enabled);
    loadSetting().catch(() => { /* unknown: Home shows no news */ });
    return () => { listeners.delete(setValue); };
  }, []);
  return value;
}
