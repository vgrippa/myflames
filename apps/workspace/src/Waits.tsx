// Server > Waits: wait events from two Performance Schema snapshots a few
// seconds apart (waits-1.0 from myflames/waits.py). A table of events or of
// classes, over the window or since server start; the selected row's meaning,
// numbers and related view are in the inspector. Classes are never added up
// and never shown as a share of a total: table I/O includes the file I/O and
// lock waits inside it.
import { useEffect, useRef, useState } from "react";
import { useOpenRead } from "./viewOnScreen";
import { api, type Connection, type WaitClass, type WaitCoverage, type WaitEvent, type WaitsReport } from "./api";
import { WINDOW_INTERVALS, type ServerKind } from "./session";
import { useConnectionReads, type Bound } from "./binding";
import { PriorityTable, CompactNumber, CopySql, WindowSelect, count, engineLabel, ms } from "./PerfShared";
import { uptime } from "./Health";
import { InstrumentsLink } from "./Instruments";
import {
  AutoRefresh,
  Disclosure,
  EmptyState,
  FactStrip,
  Fields,
  GroupedList,
  GroupedRow,
  InspectorSection,
  Pill,
  PrerequisiteGate,
  Segmented,
  SummaryLine,
  ViewLayout,
  ViewStatus,
  type Column,
  type InspectorContent,
  type Selection,
} from "./kit";

const AUTO_INTERVALS = [30, 60];

/** Where a wait class points to in the Server place. */
function linkFor(eventClass: string): { view: ServerKind; label: string } | null {
  if (eventClass.startsWith("wait/lock/")) return { view: "locks", label: "Open Locks" };
  if (eventClass === "wait/io/table" || eventClass === "wait/io/file") return { view: "io", label: "Open I/O" };
  return null;
}

function maxWait<T>(rows: T[], pick: (row: T) => number | null | undefined): number {
  let max = 0;
  for (const row of rows) {
    const v = pick(row);
    if (typeof v === "number" && Number.isFinite(v) && v > max) max = v;
  }
  return max;
}

/** Inline decorative bar showing a row's share of the largest shown row, beside the tabular number. */
function WaitProportionCell({ value, max }: { value: number | null | undefined; max: number }) {
  const ratio =
    typeof value === "number" && Number.isFinite(value) && value > 0 && max > 0
      ? Math.min(1, value / max)
      : 0;
  return (
    <span className="waits-bar-cell">
      <span className="waits-bar" aria-hidden="true">
        <span className="waits-bar-fill" style={{ width: `${ratio * 100}%` }} />
      </span>
      <span className="waits-bar-value">
        <CompactNumber value={value} />
      </span>
    </span>
  );
}

type Grouping = "events" | "classes";
type Scope = "window" | "since";

export function WaitsView({
  connection,
  disabled,
  onBack,
  selection,
  interval,
  onIntervalChange,
  report: bound,
  onReport,
  onOpenView,
}: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  interval: number;
  onIntervalChange: (seconds: number) => void;
  report: Bound<WaitsReport> | null;
  onReport: (report: Bound<WaitsReport>) => void;
  onOpenView: (view: ServerKind) => void;
}) {
  const [auto, setAuto] = useState(0);
  const [grouping, setGrouping] = useState<Grouping>("events");
  const [scope, setScope] = useState<Scope>("window");
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const error = reads.error;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const root = useRef<HTMLDivElement>(null);

  async function load() {
    await reads.run(
      `waits:${interval}`,
      `Sampling wait events for ${interval} s…`,
      (c, signal) => api<WaitsReport>("live/waits", { connection: c, interval }, signal),
      onReport,
      { onError: () => setAuto(0) },
    );
  }
  const loadRef = useRef(load);
  loadRef.current = load;
  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled, busy: !!loading || reads.running(), read: () => { void load(); } });

  const running = reads.running;
  useEffect(() => {
    if (!auto) return;
    const timer = window.setInterval(() => {
      const view = root.current?.closest<HTMLElement>(".server-view");
      const visible = !!view && view.offsetParent !== null;
      if (!document.hidden && visible && !disabled && !running()) loadRef.current();
    }, auto * 1000);
    return () => window.clearInterval(timer);
  }, [auto, disabled, running]);

  const w = report?.window;
  const since = report?.since_start;
  const off = (report?.coverage || []).filter((c) => c.state !== "on");
  const windowOk = !!w && w.status === "ok";
  const events: WaitEvent[] = scope === "window" ? (windowOk ? w!.events : []) : since?.events || [];
  const classes: WaitClass[] = scope === "window" ? (windowOk ? w!.classes : []) : since?.classes || [];
  const maxEventWait = maxWait(events, (e) => e.wait_ms);
  const maxEventRate = maxWait(events, (e) => e.wait_ms_per_s);
  const maxClassWait = maxWait(classes, (c) => c.wait_ms);

  const eventColumns: Column<WaitEvent>[] = [
    {
      key: "event",
      flex: true,
      label: "Event",
      sort: (e) => e.event,
      render: (e) => (
        <span className="cell-name">
          <code>{e.event}</code>
          <Pill>{e.class_label}</Pill>
        </span>
      ),
    },
    { key: "count", label: "Waits", num: true, sort: (e) => e.count, render: (e) => <CompactNumber value={e.count} /> },
    {
      key: "wait",
      label: "Total wait (ms)",
      num: true,
      sort: (e) => e.wait_ms,
      render: (e) => <WaitProportionCell value={e.wait_ms} max={maxEventWait} />,
    },
    ...(scope === "window"
      ? [
          {
            key: "rate",
            label: "Wait (ms/s)",
            title: "Milliseconds of waiting per second of wall time, summed across threads",
            num: true,
            sort: (e: WaitEvent) => e.wait_ms_per_s ?? null,
            render: (e: WaitEvent) => <WaitProportionCell value={e.wait_ms_per_s} max={maxEventRate} />,
          },
        ]
      : []),
    { key: "avg", label: "Average (ms)", num: true, sort: (e) => e.avg_ms, render: (e) => <CompactNumber value={e.avg_ms} /> },
    ...(scope === "since"
      ? [{ key: "max", label: "Longest (ms)", num: true, sort: (e: WaitEvent) => e.max_ms ?? null, render: (e: WaitEvent) => <CompactNumber value={e.max_ms} /> }]
      : []),
  ];
  const classColumns: Column<WaitClass>[] = [
    {
      key: "class",
      flex: true,
      label: "Class",
      sort: (c) => c.label,
      render: (c) => (
        <span className="cell-name">
          {c.label} <code className="cell-muted">{c.class}</code>
        </span>
      ),
    },
    { key: "events", label: "Events", num: true, sort: (c) => c.events, render: (c) => <CompactNumber value={c.events} /> },
    { key: "count", label: "Waits", num: true, sort: (c) => c.count, render: (c) => <CompactNumber value={c.count} /> },
    {
      key: "wait",
      label: "Total wait (ms)",
      num: true,
      sort: (c) => c.wait_ms,
      render: (c) => <WaitProportionCell value={c.wait_ms} max={maxClassWait} />,
    },
  ];

  let inspector: InspectorContent | null = null;
  const [type, name] = selection.key.split(/:(.*)/s);
  if (report && type === "event") {
    const e = [...(w?.events || []), ...(since?.events || [])].find((x) => x.event === name);
    if (e) {
      const link = linkFor(e.class);
      inspector = {
        key: selection.key,
        title: e.event,
        meta: <Pill>{e.class_label}</Pill>,
        actions: link && (
          <button type="button" className="button" onClick={() => onOpenView(link.view)}>
            {link.label}
          </button>
        ),
        body: (
          <>
            {e.meaning && (
              <InspectorSection title="What it is">
                <p>{e.meaning}</p>
              </InspectorSection>
            )}
            {windowOk && w!.events.some((x) => x.event === e.event) && (
              <InspectorSection title="Over the window">
                {(() => {
                  const x = w!.events.find((v) => v.event === e.event)!;
                  return (
                    <Fields
                      rows={[
                        ["Waits", count(x.count)],
                        ["Total wait", ms(x.wait_ms)],
                        ["Per second", x.wait_ms_per_s == null ? "—" : `${count(x.wait_ms_per_s)} ms/s`],
                        ["Average", ms(x.avg_ms)],
                      ]}
                    />
                  );
                })()}
              </InspectorSection>
            )}
            {since?.events.some((x) => x.event === e.event) && (
              <InspectorSection title="Since server start">
                {(() => {
                  const x = since!.events.find((v) => v.event === e.event)!;
                  return (
                    <Fields
                      rows={[
                        ["Waits", count(x.count)],
                        ["Total wait", ms(x.wait_ms)],
                        ["Average", ms(x.avg_ms)],
                        ["Longest", ms(x.max_ms)],
                      ]}
                    />
                  );
                })()}
              </InspectorSection>
            )}
          </>
        ),
      };
    }
  } else if (report && type === "class") {
    // The class from the time range on screen first, then the other one; the
    // numbers of each range are in their own, labelled section.
    const inWindow = windowOk ? w!.classes.find((x) => x.class === name) : undefined;
    const inSince = since?.classes.find((x) => x.class === name);
    const c = scope === "since" ? inSince || inWindow : inWindow || inSince;
    const numbers = (x: WaitClass) => (
      <Fields
        rows={[
          ["Events", count(x.events)],
          ["Waits", count(x.count)],
          ["Total wait", ms(x.wait_ms)],
        ]}
      />
    );
    if (c) {
      const link = linkFor(c.class);
      const sections = [
        inWindow && (
          <InspectorSection key="window" title="Over the window">
            {numbers(inWindow)}
          </InspectorSection>
        ),
        inSince && (
          <InspectorSection key="since" title="Since server start">
            {numbers(inSince)}
          </InspectorSection>
        ),
      ];
      if (scope === "since") sections.reverse();
      inspector = {
        key: selection.key,
        title: c.label,
        meta: <code>{c.class}</code>,
        actions: link && (
          <button type="button" className="button" onClick={() => onOpenView(link.view)}>
            {link.label}
          </button>
        ),
        body: (
          <>
            <InspectorSection title="What it is">
              <p>{c.explain}</p>
            </InspectorSection>
            {sections}
          </>
        ),
      };
    }
  } else if (report && type === "off") {
    const c = off.find((x) => x.class === name);
    if (c) inspector = coverageInspector(selection.key, c, report);
  }

  return (
    <ViewLayout
      id="waits"
      title="Waits"
      className="waits-panel"
      onBack={onBack}
      info={
        <>
          <p>Where server threads spent their waiting time: two reads of the Performance Schema wait summary a few seconds apart.</p>
          <p>Wait time is summed across all threads, so it can exceed the window. Classes overlap (table I/O includes the file I/O and lock waits inside it), so they are never added up or shown as a share of a total.</p>
        </>
      }
      controls={
        <>
          <WindowSelect value={interval} options={WINDOW_INTERVALS} onChange={onIntervalChange} disabled={loading} />
          {report?.available && (
            <Segmented<Scope>
              label="Time range"
              value={scope}
              onChange={setScope}
              options={[
                { value: "window", label: "Over the window" },
                {
                  value: "since",
                  label: "Since server start",
                  disabled: !since,
                  title: "Since the server started or Performance Schema was reset",
                },
              ]}
            />
          )}
          <AutoRefresh value={auto} options={AUTO_INTERVALS} disabled={!report} onChange={setAuto} />
        </>
      }
      onRefresh={() => void load()}
      refreshDisabled={disabled}
      loading={loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={<p>Select a wait event or class to see what it is and its numbers.</p>}
      status={<ViewStatus loading={reads.loading} error={error} />}
    >
      <div ref={root}>
        {!report && !loading && !error && (
          <EmptyState
            icon="waits"
            title="No wait events loaded"
            action={
              <button type="button" className="button primary" disabled={disabled} onClick={() => void load()}>
                Load wait events
              </button>
            }
          >
            Where server threads spent their waiting time over a short window. It only reads.
          </EmptyState>
        )}
        {report && !report.available && (
          <PrerequisiteGate action={report.unavailable === "performance_schema_off" ? <InstrumentsLink row="ps" /> : undefined}>
            {report.notes[0] || "Wait events are not available on this server."}
          </PrerequisiteGate>
        )}
        {report && (
          <>
            <SummaryLine notes={report.available ? report.notes : report.notes.slice(1)}>
              {report.available && windowOk
                ? `${w!.events.length} ${w!.events.length === 1 ? "event" : "events"} waited over ${(report.elapsed_s ?? report.interval).toFixed(1)} s`
                : report.available
                  ? "No complete window"
                  : "Not available"}
              {off.length > 0 && ` · ${off.length} ${off.length === 1 ? "class" : "classes"} not instrumented`}
            </SummaryLine>
            <FactStrip
              label="Server"
              facts={[
                ["Server", engineLabel(report)],
                ["Uptime", uptime(report.uptime_s)],
                ...(report.available ? ([["Window", `${(report.elapsed_s ?? report.interval).toFixed(1)} s`]] as [string, string][]) : []),
              ]}
            />
            {report.available && (
              <section className="view-section" aria-labelledby="waits-table-h">
                <div className="view-section-head">
                  <h3 id="waits-table-h" className="visually-hidden">
                    Wait events
                  </h3>
                  <Segmented<Grouping>
                    label="Group by"
                    value={grouping}
                    onChange={setGrouping}
                    options={[
                      { value: "events", label: "Events" },
                      { value: "classes", label: "Classes" },
                    ]}
                  />
  
                </div>
                {scope === "window" && w && w.status !== "ok" ? (
                  <div className="table-empty">
                    {w.status === "restarted"
                      ? "The server restarted between the two reads. Refresh to sample again."
                      : "Wait counters went down between the two reads (a truncate or a wrap). Refresh to sample again."}
                  </div>
                ) : grouping === "events" ? (
                  <PriorityTable
                    id="waits.events"
                    detailColumns={["rate", "avg", "max"]}
                    label={scope === "window" ? "Wait events over the window" : "Wait events since server start"}
                    columns={eventColumns}
                    rows={events}
                    rowKey={(e) => `event:${e.event}`}
                    selection={selection}
                    rowAttrs={(e) => ({ "data-event": e.event })}
                    empty={
                      scope === "window" ? (
                        <>
                          No timed waits in this window.
                          {!!since?.events.length && (
                            <>
                              {" "}
                              <button type="button" className="text-button" onClick={() => setScope("since")}>
                                Show the {since.events.length} events since server start
                              </button>
                            </>
                          )}
                        </>
                      ) : (
                        "No waits recorded since server start."
                      )
                    }
                  />
                ) : (
                  <PriorityTable
                    id="waits.classes"
                    detailColumns={[]}
                    label={scope === "window" ? "Wait classes over the window" : "Wait classes since server start"}
                    columns={classColumns}
                    rows={classes}
                    rowKey={(c) => `class:${c.class}`}
                    selection={selection}
                    rowAttrs={(c) => ({ "data-class": c.class })}
                    empty="No wait classes in this range."
                  />
                )}
              </section>
            )}
            {off.length > 0 && (
              <Disclosure title="Not instrumented" count={off.length} className="waits-off">
                <GroupedList
                  id="waits-off-h"
                  title="Classes that are off"
                  info={<p>Waits of these classes are not recorded, so their absence above proves nothing.</p>}
                >
                  {off.map((c) => (
                    <GroupedRow
                      key={c.class}
                      rowKey={`off:${c.class}`}
                      selection={selection}
                      tone={c.state === "off" ? "off" : "watch"}
                      toneLabel={c.state === "off" ? "Off" : "Partly"}
                      label={c.label}
                      detail={<code>{c.class}</code>}
                      value={`${c.enabled} of ${c.total} on`}
                      attrs={{ "data-class": c.class }}
                    />
                  ))}
                </GroupedList>
              </Disclosure>
            )}
          </>
        )}
      </div>
    </ViewLayout>
  );
}

function coverageInspector(key: string, c: WaitCoverage, report: WaitsReport): InspectorContent {
  return {
    key,
    title: c.label,
    meta: (
      <>
        <code>{c.class}</code>
        <span>
          · {c.enabled} of {c.total} on
        </span>
      </>
    ),
    actions: (
      <InstrumentsLink row={c.class.startsWith("wait/synch/") ? "group:wait/synch" : `group:${c.class}`}>
        Cost, gain and presets in Instruments
      </InstrumentsLink>
    ),
    body: (
      <>
        <InspectorSection title="What it is">
          <p>{c.explain}</p>
        </InspectorSection>
        {c.enable && (
          <InspectorSection title="Turn it on">
            <p className="hint">Now, until the server restarts:</p>
            <CopySql sql={c.enable.runtime} label={`Turn on ${c.class} until restart`} />
            <p className="hint">To keep it on, under [mysqld] in my.cnf:</p>
            <CopySql sql={c.enable.config} label={`my.cnf line for ${c.class}`} />
            <p className="hint perf-caveat">{report.enable_caveat}</p>
          </InspectorSection>
        )}
      </>
    ),
  };
}
