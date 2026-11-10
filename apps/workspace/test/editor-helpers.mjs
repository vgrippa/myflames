// Shared helpers for the CodeMirror Query editor in the browser suites.
// .sql-editor is the contentDOM; editor/automation.ts gives it read-only
// textarea-shaped accessors (value, selectionStart, selectionEnd,
// setSelectionRange, select()) for exactly these tests.
// Run after `npm run build`.
import assert from "node:assert/strict";

export const EDITOR = ".sql-editor";

/** Marks on the CodeMirror DOM: the statement tint, the error underline, the
 * bracket pair (matched or not), the flash of the statement that ran, and the
 * gutter's current and error lines. */
export const MARK = {
  statement: ".cm-content .sql-current",
  error: ".cm-content .sql-error-mark",
  match: ".cm-content .sql-match",
  unmatched: ".cm-content .sql-unmatched",
  flash: ".cm-content .sql-flash",
  gutterCurrent: ".cm-gutterElement.is-current",
  gutterError: ".cm-gutterElement.is-error",
};

/** Wait until the editor holds exactly *text*. */
export async function waitForEditorText(page, text) {
  await page.waitForFunction((sel, t) => document.querySelector(sel)?.value === t, {}, EDITOR, text);
}

/** Replace the whole text with *text* (one paste; fast even for 1 MB). */
export async function setEditorText(page, text) {
  // Focus can be taken back asynchronously (closing a panel returns focus to
  // a result row after this helper focused the editor), so focus the editor
  // and confirm document.activeElement stays inside it across two animation
  // frames, re-focusing when something took it. No fixed sleeps.
  const focused = () => page.$eval(EDITOR, (el) => el.contains(document.activeElement));
  const frame = () => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve())));
  let stable = 0;
  for (let tries = 0; tries < 40 && stable < 2; tries++) {
    if (!(await focused())) await page.$eval(EDITOR, (el) => el.focus());
    await frame();
    stable = (await focused()) ? stable + 1 : 0;
  }
  assert.equal(stable, 2, "the editor could not keep focus for two frames");
  await page.$eval(EDITOR, (el, t) => {
    el.select();
    const data = new DataTransfer();
    data.setData("text/plain", t);
    el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  }, text);
  await waitForEditorText(page, text);
  assert.ok(await focused(), "the editor kept focus through the paste");
}

/** Insert *text* at the main selection (document.execCommand, one undo step). */
export async function insertEditorText(page, text) {
  await page.$eval(EDITOR, (el, t) => {
    el.focus();
    document.execCommand("insertText", false, t);
  }, text);
}

/** The main selection as { start, end }. */
export async function editorSelection(page) {
  return page.$eval(EDITOR, (el) => ({ start: el.selectionStart, end: el.selectionEnd }));
}

/** Focus the editor and make [start, end) the main selection. */
export async function selectEditor(page, start, end = start) {
  await page.$eval(EDITOR, (el, s, e) => {
    el.focus();
    el.setSelectionRange(s, e);
  }, start, end);
}

/** The platform modifier, detected the way the suites do. */
async function modKey(page) {
  return (await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent))) ? "Meta" : "Control";
}

/** Undo with the platform shortcut (⌘Z, Ctrl+Z elsewhere): CodeMirror's
 * history answers its keymap and the beforeinput of native commands, and
 * browsers fire no beforeinput for a script-called execCommand("undo"), so
 * the key press is the only scriptable path. The Edit menu path (Electron
 * webContents.undo/redo) is checked by the packaged smoke instead. */
export async function editorUndo(page) {
  const mod = await modKey(page);
  await page.$eval(EDITOR, (el) => el.focus());
  await page.keyboard.down(mod);
  await page.keyboard.press("z");
  await page.keyboard.up(mod);
}

/** Redo with the platform shortcut (⇧⌘Z, Ctrl+Y elsewhere). */
export async function editorRedo(page) {
  const mac = (await modKey(page)) === "Meta";
  await page.$eval(EDITOR, (el) => el.focus());
  await page.keyboard.down(mac ? "Meta" : "Control");
  if (mac) await page.keyboard.down("Shift");
  await page.keyboard.press(mac ? "z" : "y");
  if (mac) await page.keyboard.up("Shift");
  await page.keyboard.up(mac ? "Meta" : "Control");
}

/** Assert the editor's whole text. */
export async function assertEditorText(page, text, message) {
  assert.equal(await page.$eval(EDITOR, (el) => el.value), text, message);
}
