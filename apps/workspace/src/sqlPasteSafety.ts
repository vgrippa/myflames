// Paste safety for SQL copied out of the workspace into the mysql client.
// Two checks:
//
// 1. Names a statement is built from must be printable ASCII without a
//    backslash, the same allowlist as myflames' privileges._UNSAFE_GRANT_SQL
//    ([^\x20-\x5b\x5d-\x7e]). Without a UTF-8 locale the client's line editor
//    (libedit) reads bytes 0x80-0xFF as editing keys, and a backslash can
//    start a client command.
// 2. findMysqlClientCommand conservatively scans for mysql/MariaDB client
//    commands. The destination's capabilities, SQL modes and client options
//    are unknown, so this warning deliberately accepts false positives.

/** Printable ASCII except backslash: the privileges._UNSAFE_GRANT_SQL rule. */
const SAFE_NAME = /^[\x20-\x5b\x5d-\x7e]*$/;

export function isPasteSafeName(name: string): boolean {
  return SAFE_NAME.test(name);
}

/** The reason a copy built from server names is refused, or null when every
 * name is safe to paste into the mysql client. */
export function unsafeNamesReason(names: string[]): string | null {
  if (names.every(isPasteSafeName)) return null;
  return "A name in this statement is not printable ASCII or contains a backslash. Pasted into the mysql client, bytes like those can act as editing keys in the line editor and a backslash can start a client command such as \\! (which runs a shell command), so this copy is not offered.";
}

// Union of the clients' COMMANDS tables, including MariaDB's costs and
// sandbox shortcuts. \N is NULL, never a command.
const CLIENT_COMMAND_CHARS = "?!#.cCdeGghnPpqQRrstTuWwx-";
const CLIENT_COMMAND_NAMES = new Set([
  "?", "charset", "clear", "connect", "costs", "delimiter", "edit", "ego",
  "exit", "go", "help", "nopager", "notee", "nowarning", "pager", "print",
  "prompt", "query_attributes", "quit", "rehash", "resetconnection",
  "sandbox", "source", "ssl_session_data_print", "status", "system", "tee",
  "use", "warnings",
]);

// The client tests an input byte, not JavaScript's Unicode whitespace class.
const asciiSpace = (ch: string | undefined) => !!ch && /[ \t\n\r\v\f]/.test(ch);

function findCommandInMode(sql: string, backslashEscapes: boolean, ansiQuotes: boolean): string | null {
  let quote = "";
  let lineComment = false;
  let blockComment = false;
  let hint = false;
  let commandStart = true;
  const n = sql.length;
  for (let i = 0; i < n; i++) {
    const ch = sql[i];
    if (ch === "\n" || ch === "\r") {
      lineComment = false;
      commandStart = true;
      continue;
    }
    if (lineComment) {
      continue;
    }
    if (quote) {
      commandStart = false;
      // MariaDB ANSI_QUOTES changes double quotes independently of single
      // quotes. Backticks close even after a backslash in either client.
      if (ch === "\\" && backslashEscapes && quote !== "`" && !(ansiQuotes && quote === '"')) {
        i++;
        continue;
      }
      if (ch === quote) {
        if (sql[i + 1] === quote) {
          i++; // '' "" `` escape the quote
          continue;
        }
        quote = "";
      }
      continue;
    }
    if (blockComment) {
      if (ch === "*" && sql[i + 1] === "/") {
        blockComment = false;
        i++;
      }
      continue;
    }
    // Optimizer hints ignore quotes, line-comment markers and nested /*.
    // Only */ ends the hint; backslash commands remain live throughout it.
    if (hint && ch === "*" && sql[i + 1] === "/") {
      hint = false;
      i++;
      continue;
    }
    if (!hint && ch === "#") {
      lineComment = true;
      continue;
    }
    if (!hint && ch === "-" && sql[i + 1] === "-" && (i + 2 >= n || asciiSpace(sql[i + 2]))) {
      lineComment = true;
      continue;
    }
    if (!hint && ch === "/" && sql[i + 1] === "*") {
      const kind = sql[i + 2];
      if (kind === "+") hint = true;
      else if (kind !== "!" && !(kind === "M" && sql[i + 3] === "!")) blockComment = true;
      // In a plain comment the third character can already be the closing
      // star of /**/. Consume only its opening pair, or later commands hide.
      i += blockComment ? 1 : kind === "M" && sql[i + 3] === "!" ? 3 : 2;
      continue;
    }
    if (asciiSpace(ch)) continue;
    if (!hint && ch === ";") {
      commandStart = true;
      continue;
    }
    if (commandStart) {
      // Named commands can run on a fresh line or after a statement. Warn
      // even if client options or a buffered statement would suppress them.
      // In particular, DELIMITER can make subsequent quotes into delimiters;
      // warning about it avoids trying to predict the resulting parser state.
      const word = /^(?:[a-z_]+|\?)(?=[ \t\n\r\v\f;]|$)/i.exec(sql.slice(i));
      if (word && CLIENT_COMMAND_NAMES.has(word[0].toLowerCase())) return word[0].toLowerCase();
    }
    commandStart = false;
    if (!hint && (ch === "'" || ch === '"' || ch === "`")) {
      quote = ch;
      continue;
    }
    if (ch === "\\") {
      const c = sql[i + 1];
      if (c && CLIENT_COMMAND_CHARS.includes(c)) return `\\${c}`;
      // Unknown shortcuts and \N consume the following byte as well. Do not
      // let that byte open a quote that the client itself would not open.
      if (c !== "\n" && c !== "\r") i++;
    }
  }
  return null;
}

/** A possible client command, or null. Dollar quotes deliberately do not hide
 * commands because their support depends on the destination server. Scan both
 * backslash modes and MariaDB's independent ANSI_QUOTES mode. */
export function findMysqlClientCommand(sql: string): string | null {
  // Deliberately over-warn on the first word of every physical line, even
  // when a quote/comment state or a nonempty client buffer might suppress a
  // named command. The destination's client settings are not available here.
  const firstWords = /(?:^|\n)[ \t\r\v\f]*([a-z_]+|\?)(?=[ \t\n\r\v\f;]|$)/gi;
  let word: RegExpExecArray | null;
  while ((word = firstWords.exec(sql))) {
    const command = word[1].toLowerCase();
    if (CLIENT_COMMAND_NAMES.has(command)) return command;
  }
  return findCommandInMode(sql, true, false)
    || findCommandInMode(sql, false, false)
    || findCommandInMode(sql, true, true);
}

/** A plain-words note about a possible client command; empty when none was
 * found. This is a warning about possible client parsing, not a SQL verdict. */
export function clientCommandNote(sql: string): string {
  const cmd = findMysqlClientCommand(sql);
  if (!cmd) return "";
  return ` Note: this text may contain a mysql client command (${cmd}). Depending on the client's settings and SQL mode, the mysql client can run it itself instead of sending it to the server, even inside /*! */ comments.`;
}

/** Drop the legacy caller-specific note before a shared clipboard notice is
 * shown. Match only the generated suffix, leaving the caller's base intact. */
export function withoutClientCommandNote(base: string): string {
  return base.replace(/ Note: this text may contain a mysql client command \([^\n)]*\)\. Depending on the client's settings and SQL mode, the mysql client can run it itself instead of sending it to the server, even inside \/\*! \*\/ comments\.$/, "");
}

/** A copy feedback message with the client-command note appended when the
 * copied text needs it. */
export function copyNote(base: string, sql: string): string {
  const note = clientCommandNote(sql);
  if (!note) return base;
  const s = base.trim();
  return (/[.!?]$/.test(s) ? s : `${s}.`) + note;
}
