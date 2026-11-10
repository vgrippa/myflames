// A missing MySQL or MariaDB command-line client is recoverable from the
// workspace: the ClientMissingNotice replaces the bare error text wherever a
// read was refused with the client-missing or client-auth-plugin code (api.ts
// isClientMissing / isClientAuthPlugin; the plugin refusal shows the server's
// own text), and its Choose client tools… opens the Client tools dialog
// (ClientTools.tsx), which resolves every program per server.
import { useRef, useState } from "react";
import { CLIENT_HINT, ClientToolsDialog } from "./ClientTools";
import "./client-missing.css";

export { CLIENT_HINT };

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
        <button ref={choose} type="button" className="button primary" onClick={() => setChoosing(true)}>
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
