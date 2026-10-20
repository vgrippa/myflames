import { useEffect, useRef, useState } from "react";
import { api, download, safePreview } from "./api";
import { Icon } from "./components";
import { bindFrameAppearance, themeFrame } from "./charts/core/appearance";
import {
  EmptyState,
  GroupedList,
  Pill,
  SearchField,
  Segmented,
  Select,
  SummaryLine,
  moveRowFocus,
} from "./kit";

type LessonView = "2d" | "3d";
type Lesson = {
  key: string;
  title: string;
  summary: string;
  family: string;
  /** "3d" when the lesson also has a 3D view (an extra option; 2D is the default). */
  views?: LessonView[];
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
  // The page and the lesson view it was loaded for, so a view switch never
  // shows the previous view's page under the new one's title.
  const [loaded, setLoaded] = useState({ key: "", html: "" });
  const [view, setView] = useState<LessonView>("2d");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const lessonFrame = useRef<HTMLIFrameElement>(null);
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
    // Only lessons that list a 3D view can show it.
    if (lesson && view === "3d" && !lesson.views?.includes("3d")) setView("2d");
  }, [lesson, view]);

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

  // A lesson without a 3D view is always shown (and requested) in 2D, even
  // for the render before `view` is reset.
  const shown: LessonView = view === "3d" && lesson?.views?.includes("3d") ? "3d" : "2d";
  const frameKey = `${lessonKey}:${shown}`;
  const html = loaded.key === frameKey ? loaded.html : "";
  useEffect(() => {
    if (lessonFrame.current) return bindFrameAppearance(lessonFrame.current);
  }, [active, html, frameKey]);
  useEffect(() => {
    if (!active || !lessonKey) return;
    const controller = new AbortController();
    setError("");
    api<{ html: string }>(
      "teach/lesson",
      { lesson: lessonKey, view: shown },
      controller.signal,
    )
      .then((result) => {
        if (!controller.signal.aborted) setLoaded({ key: `${lessonKey}:${shown}`, html: result.html });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(error.message);
      });
    return () => controller.abort();
  }, [active, lessonKey, shown, retry]);

  function openLesson(key: string) {
    setError("");
    setView("2d");
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
            className="view-toolbar teach-toolbar"
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
                  <span className="teach-step-position">
                    {position >= 0
                      ? `Step ${position + 1} of ${catalog.curriculum.length}`
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
              {lesson.views?.includes("3d") && (
                <Segmented<LessonView>
                  label="Lesson view"
                  value={shown}
                  options={[
                    { value: "2d", label: "2D", title: "The animated lesson" },
                    {
                      value: "3d",
                      label: "3D",
                      title:
                        "The same lookups on the real shape of the tree, in 3D",
                    },
                  ]}
                  onChange={setView}
                />
              )}
              <button
                type="button"
                className="button small"
                disabled={!html}
                onClick={() =>
                  download(
                    html,
                    `${lesson.key}${shown === "3d" ? "-3d" : ""}.html`,
                    "text/html",
                  )
                }
              >
                <Icon name="export" size={14} />
                <span>Download lesson</span>
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
            <iframe
              ref={lessonFrame}
              key={frameKey}
              title={`Lesson: ${lesson.title}${shown === "3d" ? " (3D view)" : ""}`}
              className="lesson-frame"
              sandbox="allow-scripts allow-downloads allow-modals"
              srcDoc={safePreview(themeFrame(markEmbedded(html)))}
            />
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
                              {(step >= 0 || item.views?.includes("3d")) && (
                                <span className="row-value">
                                  {item.views?.includes("3d") && (
                                    <Pill tone="info">3D</Pill>
                                  )}
                                  {step >= 0 && (
                                    <span className="row-secondary">
                                      Step {step + 1} of{" "}
                                      {catalog.curriculum.length}
                                    </span>
                                  )}
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
