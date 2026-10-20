// News on Home: up to six MySQL and MariaDB headlines under Recent work.
// It asks the backend only while Home is on screen and the Settings switch is
// on; the backend reads the fixed feeds and keeps a six-hour cache. One calm
// line covers loading, offline and empty; nothing here ever blocks Home.
import { useEffect, useRef, useState } from "react";
import { Checkbox, IconButton, InfoPopover } from "./kit";
import { readNews, setNewsEnabled, useNewsEnabled, type NewsReport } from "./newsApi";
import { relativeTime } from "./session";

/** "2 days ago"; a date a little ahead of this computer's clock reads "a moment ago". */
function ago(iso: string) {
  return relativeTime(iso, Math.max(Date.now(), Date.parse(iso) || 0));
}

/** The last answer, so coming back to Home shows it at once while the
 * backend answers again (from its cache unless a read is due). */
let lastReport: NewsReport | null = null;

export function HomeNews() {
  const enabled = useNewsEnabled();
  const [report, setReport] = useState<NewsReport | null>(lastReport);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  // Each read asks the backend; only a Refresh asks it to read the feeds again.
  const [reads, setReads] = useState(0);
  const refresh = useRef(false);
  useEffect(() => {
    if (enabled !== true) return;
    const controller = new AbortController();
    const again = refresh.current;
    refresh.current = false;
    setLoading(true);
    setFailed(false);
    readNews(again, controller.signal)
      .then((r) => { if (!controller.signal.aborted) setReport((lastReport = r)); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [enabled, reads]);
  if (enabled !== true || report?.enabled === false) return null;
  const items = report?.items ?? [];
  let state = "";
  if (failed) state = "News could not be loaded.";
  else if (!report) state = loading ? "Loading news…" : "";
  else if (report.stale)
    state = `${report.error || "News is unavailable offline."}${report.fetched_at ? ` Last updated ${ago(report.fetched_at)}.` : ""}`;
  else if (!items.length) state = "No news right now.";
  return (
    <section className="home-pane home-news" aria-labelledby="news-title" aria-busy={loading}>
      <div className="pane-head">
        <h2 id="news-title">News</h2>
        <InfoPopover label="About News">
          <p>Headlines from Planet MySQL (posts by the MySQL team) and the MariaDB Foundation. myflames reads these two feeds over HTTPS while Home is open, at most every six hours, and sends nothing about you or your connections.</p>
          <p>Turn news off in Settings.</p>
        </InfoPopover>
        <IconButton icon="refresh" label="Refresh news" className="pane-action" disabled={loading}
          onClick={() => { refresh.current = true; setReads((n) => n + 1); }} />
      </div>
      {state && <p className="empty-small news-state" role="status">{state}</p>}
      {!!items.length && (
        <ul className="news-list" aria-label="MySQL and MariaDB news">
          {items.map((item) => (
            <li key={item.link} data-engine={item.engine}>
              <a className="news-item" href={item.link} target="_blank" rel="noopener noreferrer"
                title={item.summary || item.title}>
                <strong>{item.title}</strong>
                <small>
                  {item.source}
                  {item.published && <> · <time dateTime={item.published}>{ago(item.published)}</time></>}
                </small>
                <span className="visually-hidden"> (opens in your browser)</span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** The Settings popover switch. Off hides the section and stops every news read. */
export function NewsSetting() {
  const enabled = useNewsEnabled();
  const [error, setError] = useState("");
  // One change at a time: the switch waits for the stored answer.
  const [saving, setSaving] = useState(false);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  return (
    <>
      <Checkbox className="check-field session-toggle news-toggle" checked={enabled === true}
        disabled={enabled === null || saving} describedBy="news-toggle-hint"
        onChange={(on) => {
          setError("");
          setSaving(true);
          setNewsEnabled(on)
            .catch(() => { if (mounted.current) setError("Could not save this setting. Try again."); })
            .finally(() => { if (mounted.current) setSaving(false); });
        }}>
        Show MySQL and MariaDB news on Home
      </Checkbox>
      <p className={error ? "hint error" : "hint"} id="news-toggle-hint" role={error ? "alert" : undefined}>
        {error || "Headlines from Planet MySQL and the MariaDB Foundation, read while Home is open."}
      </p>
    </>
  );
}
