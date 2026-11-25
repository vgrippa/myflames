// A missing MySQL or MariaDB command-line client is recoverable from the
// workspace: the ClientMissingNotice replaces the bare error text wherever a
// read was refused with the client-missing or client-auth-plugin code (api.ts
// isClientMissing / isClientAuthPlugin; the plugin refusal shows the server's
// own text), and its Choose client tools… opens the Client tools dialog
// (ClientTools.tsx), which resolves every program per server.
import { useRef, useState } from "react";
import { lazyComponent } from "./lazy";
import { MAC, WINDOWS } from "./platform";
import "./client-missing.css";

/** How to get client tools on this computer, by platform. Shown only when no
 * installation of either engine was found. Kept here, not in ClientTools, so
 * the notice stays in the entry chunk while the dialog loads on demand. */
export const CLIENT_HINT = WINDOWS
  ? "Install MySQL Server, MySQL Workbench or MariaDB, or choose mysql.exe."
  : MAC
    ? "Install MySQL's client with Homebrew (brew install mysql-client) or the MySQL installer."
    : "Install the MySQL or MariaDB client with your package manager, or choose the client binary.";

// The dialog (per-program resolution, file picking) is most of ClientTools;
// it opens only from Choose client tools…, so its chunk loads on demand.
const ClientToolsDialog = lazyComponent(() => import("./ClientTools").then((m) => m.ClientToolsDialog));
// Pointing at or focusing the opener starts the chunk on its way, so a click
// rarely waits (the UpdateBadge pattern). A failed warm-up is quiet: the
// click's own mount shows the fallback, then LoadFailed if it cannot load.
const warmClientTools = () => { void ClientToolsDialog.preload().catch(() => {}); };

const CLIENT_SENTENCE = "myflames needs the MySQL or MariaDB command-line client to read servers.";

/** Shown instead of the bare error text of a read refused for a missing
 * client (or one that lacks the server's authentication plugin): what is
 * wrong, how to fix it, Choose client tools… and Try again. */
export function ClientMissingNotice({
  onRetry,
  className = "",
  text = "",
}: {
  onRetry?: () => void;
  className?: string;
  /** A client-auth-plugin refusal shows the server's own text instead of the
   * generic sentence. */
  text?: string;
}) {
  const [choosing, setChoosing] = useState(false);
  const choose = useRef<HTMLButtonElement>(null);
  return (
    <div className={`client-missing ${className}`.trim()} data-client-missing>
      <p className="client-missing-text" role="alert">{text || CLIENT_SENTENCE}</p>
      <p className="hint client-missing-hint">{CLIENT_HINT}</p>
      <div className="client-missing-actions">
        <button ref={choose} type="button" className="button primary" onClick={() => setChoosing(true)}
          onPointerEnter={warmClientTools} onFocus={warmClientTools}>
          Choose client tools…
        </button>
        {onRetry && (
          <button type="button" className="button" onClick={onRetry}>
            Try again
          </button>
        )}
      </div>
      {choosing && <ClientToolsDialog returnFocus={choose} onClose={() => setChoosing(false)} />}
    </div>
  );
}
