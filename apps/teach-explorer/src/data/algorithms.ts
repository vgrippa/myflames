import data from "./algorithms.json";
import type { AlgorithmsData, Algorithm } from "./types";

const FAMILY_DIRS: Record<string, string> = {
  scan_family: "scan",
  index_family: "index",
  join_family: "join",
  cache_family: "cache",
  planner_family: "planner",
};

// In dev, Vite serves docs/teach/ at /teach/. In prod, the built app
// lives at docs/teach-explorer/, so we go up one level.
const TEACH_BASE = import.meta.env.DEV ? "/teach" : "../teach";

function rewriteLessonHref(a: Algorithm): Algorithm {
  const dir = FAMILY_DIRS[a.family] ?? "";
  return {
    ...a,
    lessonHref: dir ? `${TEACH_BASE}/${dir}/${a.name}.html` : a.lessonHref,
  };
}

const raw = data as AlgorithmsData;
export const algorithmsData: AlgorithmsData = {
  ...raw,
  lessons: raw.lessons.map(rewriteLessonHref),
};
export const algorithms = algorithmsData.lessons;

export function findAlgorithm(name: string) {
  return algorithms.find((a) => a.name === name);
}
