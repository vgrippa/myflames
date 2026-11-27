// Exact statements or commands with a neutral Copy button, shared by
// OpsShared (Replication, Maintenance), ClusterPlace (GRANT in WhyDisabled),
// and WhyDisabledHost (privilege-gated disabled buttons). Kept in a leaf
// module so entry-chunk consumers do not pull in OpsShared, PerfShared,
// opsApi or filesApi.
import { useState } from "react";

/** The exact statements or commands a confirmation will send or an
 * administrator can run. Copy says only that the text was copied. */
export function Commands({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState("");
  return (
    <div className="perf-sql ops-commands">
      <pre className="ops-pre" tabIndex={0} aria-label={label}>
        <code>{text}</code>
      </pre>
      <button
        type="button"
        className="text-button"
        aria-label={`Copy: ${label}`}
        onClick={() => {
          void navigator.clipboard.writeText(text).then(
            () => setCopied("Copied."),
            () => setCopied("Select the text to copy it."),
          );
        }}
      >
        Copy
      </button>
      {copied && (
        <span className="hint" role="status">
          {copied}
        </span>
      )}
    </div>
  );
}
