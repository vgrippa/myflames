import { useEffect, useState } from "react";
import { api, download, safePreview } from "./api";

type Lesson = { key: string; title: string; summary: string; family: string };
type Catalog = {
  lessons: Lesson[];
  curriculum: string[];
  families: { key: string; label: string }[];
};

export function Teach({
  active,
  initialLesson = "",
  onReturn,
}: {
  active: boolean;
  initialLesson?: string;
  onReturn?: () => void;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [lessonKey, setLessonKey] = useState("");
  const [search, setSearch] = useState("");
  const [family, setFamily] = useState("");
  const [html, setHtml] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (active && initialLesson) setLessonKey(initialLesson);
  }, [active, initialLesson]);
  const lesson = catalog?.lessons.find((item) => item.key === lessonKey);
  const position = catalog?.curriculum.indexOf(lessonKey) ?? -1;

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

  useEffect(() => {
    setHtml("");
    if (!active || !lessonKey) return;
    const controller = new AbortController();
    setError("");
    api<{ html: string }>(
      "teach/lesson",
      { lesson: lessonKey },
      controller.signal,
    )
      .then((result) => {
        if (!controller.signal.aborted) setHtml(result.html);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(error.message);
      });
    return () => controller.abort();
  }, [active, lessonKey, retry]);

  function openLesson(key: string) {
    setHtml("");
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
  return (
    <section className="teach-workspace" aria-label="myflames Teach">
      <div className="page-heading">
        <div>
          <div className="eyebrow">MYFLAMES TEACH</div>
          <h1>{lesson ? lesson.title : "Learn how your query runs."}</h1>
          <p>
            {lesson
              ? lesson.summary
              : "Explore database algorithms with interactive lessons. Change the inputs and watch what happens."}
          </p>
        </div>
        <div className="heading-actions">
          {onReturn && (
            <button className="button" onClick={onReturn}>
              Back to investigation
            </button>
          )}
          {lesson && (
            <>
              <button className="button" onClick={() => openLesson("")}>
                All lessons
              </button>
              <button
                className="button"
                disabled={!html}
                onClick={() =>
                  download(html, `${lesson.key}.html`, "text/html")
                }
              >
                Download lesson
              </button>
            </>
          )}
        </div>
      </div>
      {error && (
        <div className="error" role="alert">
          {error}
          <button
            className="button"
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
      {catalog && !lesson && (
        <>
          <div className="teach-start">
            <div>
              <div className="eyebrow">A GUIDED START</div>
              <h2>Start with a table scan</h2>
              <p>
                Follow the existing learning path, from scans and indexes to
                joins and caching.
              </p>
            </div>
            <button
              className="button primary"
              onClick={() => openLesson(catalog.curriculum[0])}
            >
              Start learning →
            </button>
          </div>
          <div className="teach-filters">
            <label className="field">
              Search lessons
              <input
                type="search"
                aria-label="Search lessons"
                value={search}
                placeholder="Try B-tree, hash join, or buffer pool…"
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
            <label className="field">
              Family
              <select
                aria-label="Lesson family"
                value={family}
                onChange={(e) => setFamily(e.target.value)}
              >
                <option value="">All families</option>
                {catalog.families.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="teach-count" role="status">
            {visible.length} of {catalog.lessons.length} lessons · Open any
            topic or follow the numbered learning path.
          </p>
          <div className="lesson-grid">
            {visible.map((item) => {
              const step = catalog.curriculum.indexOf(item.key);
              return (
                <button
                  className="lesson-card"
                  data-lesson={item.key}
                  key={item.key}
                  onClick={() => openLesson(item.key)}
                >
                  <span className="lesson-family">
                    {catalog.families.find((f) => f.key === item.family)?.label}
                  </span>
                  <h2>{item.title}</h2>
                  <p>{item.summary}</p>
                  <span className="lesson-link">
                    {step >= 0
                      ? `Learning path · ${step + 1} / ${catalog.curriculum.length}`
                      : "Explore this topic"}
                    <span aria-hidden="true">↗</span>
                  </span>
                </button>
              );
            })}
          </div>
          {visible.length === 0 && (
            <div className="card empty-note">
              No lessons match your search.{" "}
              <button
                className="text-button"
                onClick={() => {
                  setSearch("");
                  setFamily("");
                }}
              >
                Clear filters
              </button>
            </div>
          )}
        </>
      )}
      {lesson && (
        <>
          <nav className="lesson-navigation" aria-label="Learning path">
            <button
              className="button"
              disabled={position <= 0}
              onClick={() => openLesson(catalog!.curriculum[position - 1])}
            >
              ← Previous lesson
            </button>
            <span>
              {position >= 0
                ? `Learning path · ${position + 1} of ${catalog!.curriculum.length}`
                : "Additional topic"}
            </span>
            <button
              className="button"
              disabled={
                position < 0 || position >= catalog!.curriculum.length - 1
              }
              onClick={() => openLesson(catalog!.curriculum[position + 1])}
            >
              Next lesson →
            </button>
          </nav>
          <div className="card lesson-viewer">
            {!html && !error && (
              <div className="preview-state" role="status">
                <span className="spinner" />
                Loading lesson…
              </div>
            )}
            {html && (
              <iframe
                key={lessonKey}
                title={`Lesson: ${lesson.title}`}
                className="lesson-frame"
                sandbox="allow-scripts allow-downloads allow-modals"
                srcDoc={safePreview(html)}
              />
            )}
          </div>
        </>
      )}
    </section>
  );
}
