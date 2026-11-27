// Paste safety for SQL copied out of the workspace into the mysql client.
// Two checks:
//
// 1. Names a statement is built from must be printable ASCII without a
//    backslash, the same allowlist as myflames' privileges._UNSAFE_GRANT_SQL
//    ([^\x20-\x5b\x5d-\x7e]). Without a UTF-8 locale the client's line editor
//    (libedit) reads bytes 0x80-0xFF as editing keys, and a backslash can
//    start a client command.
// 2. findMysqlClientCommand scans copied SQL for mysql client commands
//    (\!, \., \G, ...) the way the client reads them (client/mysql.cc
//    add_line): a backslash command is live anywhere outside a string or a
//    quoted identifier, outside # and "-- " line comments, and outside plain
//    /* */ comments. It stays live inside executable /*! ... */ and hint
//    /*+ ... */ comments, which the client still scans.

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

// Command characters from the COMMANDS table in client/mysql.cc: ? c r d e G
// q g h n t P p R # . s ! T u C W w x. \N is NULL, never a command.
const CLIENT_COMMAND_CHARS = "?!#.cCdeGghnPpqRrstTuWwx";

/** The first mysql client command (\!, \., \G, ...) the mysql client would
 * run from this text, like "\\!", or null when there is none outside strings,
 * quoted identifiers and non-executable comments. */
export function findMysqlClientCommand(sql: string): string | null {
  let quote = ""; // ' " ` or "$" (a dollar-quoted body)
  let dollarTag = "";
  let lineComment = false;
  let blockComment = false; // a plain /* */ comment; /*! and /*+ stay live
  const n = sql.length;
  for (let i = 0; i < n; i++) {
    const ch = sql[i];
    if (lineComment) {
      if (ch === "\n" || ch === "\r") lineComment = false;
      continue;
    }
    if (quote === "$") {
      if (ch === "$" && sql.startsWith(dollarTag, i)) {
        i += dollarTag.length - 1;
        quote = "";
        dollarTag = "";
      }
      continue;
    }
    if (quote) {
      // A backslash escape inside a string is never a command. Identifiers
      // quoted with backticks have no backslash escapes.
      if (ch === "\\" && quote !== "`") {
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
    if (ch === "#") {
      lineComment = true;
      continue;
    }
    // "-- " starts a line comment only when whitespace (or the end of the
    // text) follows, as the client requires.
    if (ch === "-" && sql[i + 1] === "-" && (i + 2 >= n || /\s/.test(sql[i + 2]))) {
      lineComment = true;
      continue;
    }
    if (ch === "/" && sql[i + 1] === "*") {
      const kind = sql[i + 2];
      if (kind !== "!" && kind !== "+") blockComment = true;
      i++;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === "`") {
      quote = ch;
      continue;
    }
    if (ch === "$") {
      // Dollar-quoted body ($$...$$, $tag$...$tag$), as the client reads it;
      // not after an identifier character (a$5 is one token).
      const tag = /^\$[A-Za-z0-9_]*\$/.exec(sql.slice(i));
      if (tag && (i === 0 || !/[A-Za-z0-9_]/.test(sql[i - 1]))) {
        quote = "$";
        dollarTag = tag[0];
        i += tag[0].length - 1;
        continue;
      }
    }
    if (ch === "\\") {
      const c = sql[i + 1];
      if (c && CLIENT_COMMAND_CHARS.includes(c)) return `\\${c}`;
    }
  }
  return null;
}

/** A plain-words note for copy feedback when the copied SQL contains a mysql
 * client command outside quotes; empty when there is none. */
export function clientCommandNote(sql: string): string {
  const cmd = findMysqlClientCommand(sql);
  if (!cmd) return "";
  return ` Note: this text contains a mysql client command (${cmd}) outside quotes. The mysql client reads those even inside /*! */ comments and would run ${cmd} itself instead of sending it to the server.`;
}

/** A copy feedback message with the client-command note appended when the
 * copied text needs it. */
export function copyNote(base: string, sql: string): string {
  const note = clientCommandNote(sql);
  if (!note) return base;
  const s = base.trim();
  return (/[.!?]$/.test(s) ? s : `${s}.`) + note;
}
