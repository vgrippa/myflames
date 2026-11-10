// The one way a resolved client tool is written (client-tools-1.0): every
// place that names the program running a job builds the line here:
// "<program> <version> · <origin label> · <reason sentence>". The API sends
// reason codes; the sentences are the contract's.
import type { ClientToolsReason } from "./api";

/** Anything the helper can describe: a client-tools Use, or a lifecycle Tool
 * (its version can be a banner, so version_tuple wins when present). */
export type ToolLineSource = {
  program?: string;
  name?: string;
  version?: string;
  version_tuple?: number[];
  engine?: string;
  origin_label?: string;
  reason?: ClientToolsReason | string;
  warning?: string | null;
};
/** The server the tool was picked for (engine names the reason sentence). */
export type ToolLineServer = { engine?: string; version?: string | null; product?: string } | null;

const engineWord = (engine?: string) => (engine === "mariadb" ? "MariaDB" : "MySQL");
const seriesOf = (version?: string | null) => (version || "").match(/^\d+\.\d+/)?.[0] || "";

/** The contract's sentence for a reason code; "" when nothing should be said. */
export function reasonSentence(reason: string | undefined, tool: ToolLineSource, server?: ToolLineServer): string {
  const engine = engineWord(server?.engine);
  const series = seriesOf(server?.version);
  const thisServer = series ? `${engine} ${series}` : engine;
  switch (reason) {
    case "chosen":
      return "chosen in Settings";
    case "series":
      return `matches this server (${thisServer})`;
    case "newer":
      return `newer than this server (${thisServer})`;
    case "older":
      return tool.warning || `older than this server (${thisServer})`;
    case "unknown_server":
      return "this server's version is not known yet";
    case "other_engine":
      return server?.engine === "mariadb"
        ? "no MariaDB client tools found; the MySQL client is used"
        : "no MySQL client tools found; the MariaDB client is used";
    case "newest":
      return "newest MySQL Shell found";
    default:
      return "";
  }
}

/** The single line for a resolved program, and its warning (shown apart, in
 * the warning style), or null. */
export function toolUseLine(tool: ToolLineSource, server?: ToolLineServer): { line: string; warning: string | null } {
  const program = tool.program || tool.name || "";
  const version = tool.version_tuple?.length ? tool.version_tuple.join(".") : tool.version || "";
  const reason = reasonSentence(tool.reason, tool, server);
  const parts = [[program, version].filter(Boolean).join(" "), tool.origin_label || "", reason].filter(Boolean);
  return { line: parts.join(" · "), warning: tool.warning || null };
}
