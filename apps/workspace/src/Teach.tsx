import { useCallback, useEffect, useRef, useState } from "react";
import { api, download, safePreview } from "./api";
import { Icon } from "./components";
import { bindFrameAppearance, themeFrame } from "./charts/core/appearance";
import {
  EmptyState,
  GroupedList,
  SearchField,
  Select,
  SummaryLine,
  moveRowFocus,
  useFocusScope,
} from "./kit";

/** Message type shared with the lesson page (teachFocus in myflames/teach/_html.py). */
const FOCUS_MESSAGE = "myflames-lesson-focus";
/** "native": the browser's full screen; "overlay": the lesson covers the
 *  window (the Fullscreen API is refused or missing). */
type FocusMode = "" | "native" | "overlay";

type Lesson = {
  key: string;
  title: string;
  summary: string;
  family: string;
};
type Catalog = {
  lessons: Lesson[];
  curriculum: string[];
  families: { key: string; label: string }[];
};

/** Mark the first <html start tag as embedded so the lesson hides its own chrome. */
function markEmbedded(doc: string) {
  return doc.replace(/<html\b(?![^>]*\bdata-embedded=)/i, '<html data-embedded="true"');
}

export function Teach({
  active,
  initialLesson = "",
  onReturn,
  onLessonChange,
}: {
  active: boolean;
  initialLesson?: string;
  onReturn?: () => void;
  /** The open lesson key ("" for the catalog) and title, for session restore and the top bar. */
  onLessonChange?: (key: string, title?: string) => void;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [lessonKey, setLessonKey] = useState(initialLesson);
  const [search, setSearch] = useState("");
  const [family, setFamily] = useState("");
  // Keep the page with its lesson key so navigation never shows the previous
  // lesson under the new title.
  const [loaded, setLoaded] = useState({ key: "", html: "" });
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const lessonFrame = useRef<HTMLIFrameElement>(null);
  const lessonHost = useRef<HTMLDivElement>(null);
  const fullScreenButton = useRef<HTMLButtonElement>(null);
  const exitButton = useRef<HTMLButtonElement>(null);
  const [focusMode, setFocusMode] = useState<FocusMode>("");
  const focusNow = useRef<FocusMode>("");
  focusNow.current = focusMode;
  const requesting = useRef(false);
  // Each request and each leave counts: a request that settles after the
  // lesson was left (another place, another lesson) changes nothing.
  const requestGen = useRef(0);
  const focused = focusMode !== "";
  useEffect(() => {
    if (active && initialLesson) setLessonKey(initialLesson);
  }, [active, initialLesson]);
  useEffect(() => {
    // A restored lesson may no longer exist in this version's catalog.
    if (
      catalog &&
      lessonKey &&
      !catalog.lessons.some((l) => l.key === lessonKey)
    ) {
      setLessonKey("");
      setError("");
    }
  }, [catalog, lessonKey]);
  const lesson = catalog?.lessons.find((item) => item.key === lessonKey);
  const position = catalog?.curriculum.indexOf(lessonKey) ?? -1;
  useEffect(() => {
    onLessonChange?.(lessonKey, lesson?.title);
  }, [active, lessonKey, lesson?.title]);

  useEffect(() => {
    if (!active || catalog) return;
    const controller = new AbortController();
    setError("");
    api<Catalog>("teach/catalog", {}, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setCatalog(result);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(error.message);
      });
    return () => controller.abort();
  }, [active, catalog, retry]);

  const html = loaded.key === lessonKey ? loaded.html : "";
  useEffect(() => {
    if (lessonFrame.current) return bindFrameAppearance(lessonFrame.current);
  }, [active, html, lessonKey]);
  useEffect(() => {
    if (!active || !lessonKey) return;
    const controller = new AbortController();
    setError("");
    api<{ html: string }>(
      "teach/lesson",
      { lesson: lessonKey },
      controller.signal,
    )
      .then((result) => {
        if (!controller.signal.aborted) setLoaded({ key: lessonKey, html: result.html });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(error.message);
      });
    return () => controller.abort();
  }, [active, lessonKey, retry]);

  // Full screen: the workspace owns it and tells the lesson to switch to its
  // focus layout (only the animation, its controls, caption and numbers).
  const tellLesson = (focus: boolean) =>
    lessonFrame.current?.contentWindow?.postMessage({ type: FOCUS_MESSAGE, focus }, "*");
  const leaveFullScreen = useCallback(() => {
    requestGen.current += 1;
    const host = lessonHost.current;
    const done = () => setFocusMode("");
    if (host && document.fullscreenElement === host) {
      // The rest of the page stays inert until the browser has left full
      // screen; end the mode then, so focus can return to the Full screen
      // button (fullscreenchange usually gets there first).
      document.exitFullscreen().then(done, done);
      return;
    }
    done();
  }, []);
  function enterFullScreen() {
    const host = lessonHost.current;
    if (!host || focusNow.current || requesting.current) return;
    if (!document.fullscreenEnabled || typeof host.requestFullscreen !== "function") {
      setFocusMode("overlay");
      return;
    }
    requesting.current = true;
    const gen = ++requestGen.current;
    const current = () => gen === requestGen.current && lessonHost.current === host;
    host.requestFullscreen().then(
      () => {
        if (current()) setFocusMode(document.fullscreenElement === host ? "native" : "overlay");
        // Granted after the lesson was left: give the screen back.
        else if (document.fullscreenElement === host) void document.exitFullscreen().catch(() => {});
      },
      () => { if (current()) setFocusMode("overlay"); },
    ).finally(() => {
      requesting.current = false;
    });
  }
  useEffect(() => {
    tellLesson(focused);
  }, [focused, html]);
  useEffect(() => {
    // Escape and the browser's own controls end native full screen.
    const change = () => {
      if (focusNow.current === "native" && document.fullscreenElement !== lessonHost.current) {
        setFocusMode("");
      }
    };
    document.addEventListener("fullscreenchange", change);
    return () => document.removeEventListener("fullscreenchange", change);
  }, []);
  useEffect(() => {
    // Escape pressed inside the lesson reaches only the lesson, which asks to
    // leave. Accept that request from this lesson's frame only.
    const receive = (event: MessageEvent) => {
      const frame = lessonFrame.current;
      const data = event.data;
      if (!frame || event.source !== frame.contentWindow) return;
      if (!data || data.type !== FOCUS_MESSAGE || data.focus !== false) return;
      leaveFullScreen();
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [leaveFullScreen]);
  useEffect(() => {
    if (!active || !html) leaveFullScreen();
  }, [active, html, leaveFullScreen]);
  // Escape in the workspace leaves, Tab stays inside, and focus returns to the
  // Full screen button.
  useFocusScope(lessonHost, {
    open: focused,
    onClose: leaveFullScreen,
    initialFocus: () => lessonFrame.current,
    returnFocus: () => fullScreenButton.current,
  });

  function openLesson(key: string) {
    setError("");
    setLessonKey(key);
    document.getElementById("main")?.scrollIntoView({ block: "start" });
  }
  if (!active) return null;
  const visible =
    catalog?.lessons.filter(
      (item) =>
        (!family || item.family === family) &&
        `${item.title} ${item.summary} ${item.key}`
          .toLowerCase()
          .includes(search.toLowerCase()),
    ) || [];
  const filtered = Boolean(search || family);
  // Learning path position; lessons off the path sort after every step.
  const stepOf = (key: string) => {
    const at = catalog?.curriculum.indexOf(key) ?? -1;
    return at < 0 ? Number.MAX_SAFE_INTEGER : at;
  };
  const firstStep = (familyKey: string) =>
    Math.min(Number.MAX_SAFE_INTEGER, ...(catalog?.lessons.filter((l) => l.family === familyKey).map((l) => stepOf(l.key)) || []));

  return (
    <section
      className={`teach-workspace${lesson ? " is-lesson-open" : ""}`}
      aria-label="Learn"
    >
      {lesson ? (
        <>
          <div
            className="view-toolbar teach-toolbar is-lesson"
            role="toolbar"
            aria-label="Lesson controls"
          >
            <div className="view-toolbar-left">
              {onReturn && (
                <button
                  type="button"
                  className="button small"
                  onClick={onReturn}
                >
                  <Icon name="back" size={14} />
                  <span>Back to plan</span>
                </button>
              )}
              <button
                type="button"
                className="button small"
                onClick={() => openLesson("")}
              >
                <Icon name="back" size={14} />
                <span>All lessons</span>
              </button>
              {catalog && (
                <div
                  className="teach-stepper"
                  role="group"
                  aria-label="Learning path"
                >
                  <button
                    type="button"
                    className="button small"
                    disabled={position <= 0}
                    onClick={() => openLesson(catalog.curriculum[position - 1])}
                  >
                    Previous lesson
                  </button>
                  {/* "Lesson", never "Step": a step is one beat of the
                      lesson's own animation (its step bar says "Step 2 of 8"). */}
                  <span className="teach-step-position">
                    {position >= 0
                      ? `Lesson ${position + 1} of ${catalog.curriculum.length}`
                      : "Additional topic"}
                  </span>
                  <button
                    type="button"
                    className="button small"
                    disabled={
                      position < 0 || position >= catalog.curriculum.length - 1
                    }
                    onClick={() => openLesson(catalog.curriculum[position + 1])}
                  >
                    Next lesson
                  </button>
                </div>
              )}
            </div>
            <div className="view-toolbar-right">
              <button
                type="button"
                className="button small"
                disabled={!html}
                aria-label="Download lesson"
                title="Download lesson"
                onClick={() =>
                  download(
                    html,
                    `${lesson.key}.html`,
                    "text/html",
                  )
                }
              >
                <Icon name="export" size={14} />
                {/* Icon only in a narrow window, so Full screen keeps one row. */}
                <span className="teach-wide-label">Download lesson</span>
              </button>
              <button
                ref={fullScreenButton}
                type="button"
                className="button small"
                disabled={!html}
                aria-label="Show the lesson animation full screen"
                onClick={enterFullScreen}
              >
                <Icon name="fullscreen" size={14} />
                <span>Full screen</span>
              </button>
            </div>
          </div>
          {error && (
            <div className="error teach-error" role="alert">
              {error}
              <button
                type="button"
                className="button small"
                onClick={() => setRetry((value) => value + 1)}
              >
                Try again
              </button>
            </div>
          )}
          {!html && !error && (
            <div className="preview-state teach-loading" role="status">
              <span className="spinner" />
              Loading lesson…
            </div>
          )}
          {html && (
            <div
              ref={lessonHost}
              className={`lesson-host${focused ? " is-focus" : ""}${focusMode === "overlay" ? " is-overlay" : ""}`}
              tabIndex={-1}
              role={focused ? "dialog" : undefined}
              aria-modal={focused ? true : undefined}
              aria-label={focused ? `${lesson.title}, full screen` : undefined}
            >
              {focused && (
                <div className="lesson-focus-bar">
                  <span className="lesson-focus-title">{lesson.title}</span>
                  <button
                    ref={exitButton}
                    type="button"
                    className="button small"
                    title="Exit full screen (Esc)"
                    onClick={leaveFullScreen}
                  >
                    <Icon name="exit-fullscreen" size={14} />
                    <span>Exit full screen</span>
                  </button>
                </div>
              )}
              <iframe
                ref={lessonFrame}
                key={lessonKey}
                title={`Lesson: ${lesson.title}`}
                className="lesson-frame"
                sandbox="allow-scripts allow-downloads allow-modals"
                allow="fullscreen"
                tabIndex={0}
                onLoad={() => tellLesson(focusNow.current !== "")}
                srcDoc={safePreview(themeFrame(markEmbedded(html)))}
              />
              {focused && (
                // Tab past the lesson's last control wraps to Exit; Shift+Tab
                // from Exit (wrapped here by the focus scope) enters the lesson.
                <span
                  className="lesson-focus-wrap"
                  tabIndex={0}
                  onFocus={(event) =>
                    event.relatedTarget === exitButton.current
                      ? lessonFrame.current?.focus()
                      : exitButton.current?.focus()
                  }
                />
              )}
            </div>
          )}
        </>
      ) : (
        <>
          <div
            className="view-toolbar teach-toolbar"
            role="toolbar"
            aria-label="Learn toolbar"
          >
            <div className="view-toolbar-left">
              {onReturn && (
                <button
                  type="button"
                  className="button small"
                  onClick={onReturn}
                >
                  <Icon name="back" size={14} />
                  <span>Back to plan</span>
                </button>
              )}
              <SearchField
                label="Search lessons"
                value={search}
                placeholder="Search lessons…"
                onChange={setSearch}
              />
              <label className="toolbar-field">
                <span>Family</span>
                <Select
                  label="Lesson family"
                  value={family}
                  options={[
                    ["", "All families"],
                    ...(catalog?.families.map(
                      (item): [string, string] => [item.key, item.label],
                    ) || []),
                  ]}
                  onChange={setFamily}
                />
              </label>
            </div>
            <div className="view-toolbar-right">
              {catalog && catalog.curriculum.length > 0 && (
                <button
                  type="button"
                  className="button small primary"
                  onClick={() => openLesson(catalog.curriculum[0])}
                >
                  Start learning
                </button>
              )}
            </div>
          </div>
          <div className="view-main teach-catalog">
            {error && (
              <div className="error" role="alert">
                {error}
                <button
                  type="button"
                  className="button small"
                  onClick={() => setRetry((value) => value + 1)}
                >
                  Try again
                </button>
              </div>
            )}
            {!catalog && !error && (
              <div className="preview-state" role="status">
                Loading lessons…
              </div>
            )}
            {catalog && (
              <>
                <SummaryLine>
                  {filtered
                    ? `${visible.length} of ${catalog.lessons.length} lessons, ${catalog.curriculum.length} in the learning path`
                    : `${catalog.lessons.length} lessons, ${catalog.curriculum.length} in the learning path`}
                </SummaryLine>
                {/* Follow the learning path: families by their earliest step,
                    lessons by step, then the additional topics by title. */}
                {[...catalog.families].sort((a, b) => firstStep(a.key) - firstStep(b.key)).map((group) => {
                  const items = visible
                    .filter((item) => item.family === group.key)
                    .sort((a, b) => stepOf(a.key) - stepOf(b.key) || a.title.localeCompare(b.title));
                  if (!items.length) return null;
                  return (
                    <GroupedList
                      key={group.key}
                      id={`teach-family-${group.key}`}
                      title={group.label}
                      count={items.length}
                    >
                      {items.map((item) => {
                        const step = catalog.curriculum.indexOf(item.key);
                        return (
                          <li className="grouped-item" key={item.key}>
                            <button
                              type="button"
                              className="grouped-row lesson-card"
                              data-lesson={item.key}
                              data-row-key={item.key}
                              onClick={() => openLesson(item.key)}
                              onKeyDown={(e) =>
                                moveRowFocus(
                                  e,
                                  "button.grouped-row[data-row-key]",
                                  ".teach-catalog",
                                )
                              }
                            >
                              <span className="row-label">
                                <span className="row-title">{item.title}</span>
                                <span className="row-detail">
                                  {item.summary}
                                </span>
                              </span>
                              {step >= 0 && (
                                <span className="row-value">
                                  <span className="row-secondary">
                                    Lesson {step + 1} of{" "}
                                    {catalog.curriculum.length}
                                  </span>
                                </span>
                              )}
                              <Icon name="disclosure" size={14} />
                            </button>
                          </li>
                        );
                      })}
                    </GroupedList>
                  );
                })}
                {visible.length === 0 && (
                  <EmptyState
                    icon="learn"
                    title="No lessons match these filters"
                    action={
                      <button
                        type="button"
                        className="button small"
                        onClick={() => {
                          setSearch("");
                          setFamily("");
                        }}
                      >
                        Clear filters
                      </button>
                    }
                  >
                    Clear the search or family filter to see all{" "}
                    {catalog.lessons.length} lessons.
                  </EmptyState>
                )}
              </>
            )}
          </div>
        </>
      )}
    </section>
  );
}
