// Code splitting for places and views that are not on the first screen
// (docs/DEVELOPER_REFERENCE.md, "Bundle and lazy loading"). Home, the sidebar
// and the shell stay in the entry chunk; a place or view loads its chunk the
// first time it mounts. Styles are not split (vite.config.ts cssCodeSplit).
import {
  Suspense,
  createElement,
  forwardRef,
  lazy,
  useEffect,
  useState,
  type ComponentType,
  type ExoticComponent,
} from "react";

/** A quiet placeholder while a chunk loads: nothing for the first 300 ms (a
 * chunk from the local server normally arrives well before), then one line. */
export function LazyFallback() {
  const [late, setLate] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setLate(true), 300);
    return () => window.clearTimeout(timer);
  }, []);
  return late ? (
    <div className="view-status">
      <p role="status" className="capture-status">
        <span className="spinner" /> Loading…
      </p>
    </div>
  ) : null;
}

/** Shown in place of a part whose chunk could not be read (for example files
 * replaced by an update while the window stayed open). */
function LoadFailed() {
  return (
    <div className="view-status">
      <p role="alert" className="error">
        This part of the workspace did not load.{" "}
        <button type="button" className="text-button" onClick={() => window.location.reload()}>
          Reload
        </button>
      </p>
    </div>
  );
}

type Loadable = ComponentType<any> | ExoticComponent<any>;

/** React.lazy with its own Suspense boundary and a preload. A mount keeps the
 * form it started with: before the chunk arrived it renders through Suspense
 * (the fallback, then the component), after it the component directly, so a
 * mounted part is never remounted, and never loses state, because its chunk
 * arrived. Refs are forwarded (the Query editor's handle). */
export function lazyComponent<T extends Loadable>(load: () => Promise<T>): T & { preload: () => Promise<T> } {
  let loaded: T | null = null;
  let pending: Promise<T> | null = null;
  const preload = () => {
    if (!pending) {
      pending = load().then(
        (component) => (loaded = component),
        (error) => {
          pending = null;
          throw error;
        },
      );
    }
    return pending;
  };
  const Lazy = lazy(() =>
    preload().then(
      (component) => ({ default: component as Loadable }),
      () => ({ default: LoadFailed as Loadable }),
    ),
  );
  const Part = forwardRef<unknown, Record<string, unknown>>(function LazyPart(props, ref) {
    const [direct] = useState(() => loaded);
    if (direct) return createElement(direct as Loadable, { ...props, ref });
    return <Suspense fallback={<LazyFallback />}>{createElement(Lazy, { ...props, ref })}</Suspense>;
  });
  return Object.assign(Part, { preload }) as unknown as T & { preload: () => Promise<T> };
}

/** Loads the chunks of *parts* one at a time while the page is idle, so a
 * first visit does not wait for them. Only code is loaded: nothing mounts and
 * nothing reads from a server. */
export function preloadWhenIdle(parts: { preload: () => Promise<unknown> }[]) {
  const idle = (next: () => void) =>
    typeof window.requestIdleCallback === "function" ? window.requestIdleCallback(next, { timeout: 2000 }) : setTimeout(next, 200);
  const queue = [...parts];
  const step = () => {
    const part = queue.shift();
    if (part) void part.preload().catch(() => {}).finally(() => idle(step));
  };
  idle(step);
}
