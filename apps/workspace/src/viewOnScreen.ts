import { createContext, useContext, useEffect, useRef } from "react";
/** True while the view that reads it is the selected view of a place that is on
 * screen. Views read on open only while this is true; Refresh all and explicit
 * Refresh/Load buttons read regardless. */
export const ViewOnScreenContext = createContext(true);
export const useViewOnScreen = () => useContext(ViewOnScreenContext);

/** A view's automatic first read (AGENTS.md: a view reads when it is opened on
 * screen, once per connection; Refresh retries). It reads when the view comes on
 * screen without a report of the connection in use (none, or one from another
 * connection), and when it is on screen with no report at all and this
 * connection was not tried yet. Choosing another connection while the view
 * stays on screen keeps the report it shows greyed until Refresh. `ready` holds
 * the view's own gates (disabled, schema-size consent); the first time it is
 * ready on screen counts as the opening. */
export function useOpenRead({ key, reportKey, ready, busy, read }: {
  key: string;
  reportKey: string | null | undefined;
  ready: boolean;
  busy: boolean;
  read: () => void;
}) {
  const onScreen = useViewOnScreen();
  const opened = useRef(false);
  const tried = useRef<string | null>(null);
  const readRef = useRef(read);
  readRef.current = read;
  useEffect(() => {
    if (!onScreen) { opened.current = false; return; }
    if (!ready || !key || busy) return;
    const opening = !opened.current;
    opened.current = true;
    if (reportKey === key || tried.current === key) return;
    if (opening || !reportKey) {
      tried.current = key;
      readRef.current();
    }
  }, [onScreen, ready, key, reportKey, busy]);
}
