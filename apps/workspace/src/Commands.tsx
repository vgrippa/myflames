// Exact statements or commands with a neutral Copy button, shared by
// OpsShared (Replication, Maintenance), ClusterPlace (GRANT in WhyDisabled),
// and WhyDisabledHost (privilege-gated disabled buttons). Kept in a leaf
// module so entry-chunk consumers do not pull in OpsShared, PerfShared,
// opsApi or filesApi.
import { useEffect, useRef, useState } from "react";
import { copyFeedback, copyToClipboardResult } from "./clipboard";

/** The exact statements or commands a confirmation will send or an
 * administrator can run. Copy says only that the text was copied (and whether
 * control characters were removed). */
export function Commands({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState<{ text: string; message: string }>({ text: "", message: "" });
  const currentText = useRef(text);
  currentText.current = text;
  const copySeq = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Reset feedback when text changes and invalidate any in-flight clipboard
  // completion for older text so stale "Copied." never appears over new SQL.
  useEffect(() => {
    copySeq.current += 1;
    setCopied({ text: "", message: "" });
  }, [text]);

  const status = copied.text === text ? copied.message : "";

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
          const seq = ++copySeq.current;
          const target = text;
          void copyToClipboardResult(target).then((res) => {
            if (mounted.current && copySeq.current === seq && currentText.current === target) {
              setCopied({
                text: target,
                message: res.ok ? copyFeedback("Copied.", res.removed) : "Select the text to copy it.",
              });
            }
          });
        }}
      >
        Copy
      </button>
      {status && (
        <span className="hint" role="status">
          {status}
        </span>
      )}
    </div>
  );
}
