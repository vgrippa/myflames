// The release-notes renderer of the Software update dialog (releaseNotes.tsx)
// without a browser: the Markdown subset, text-only safety, CRLF input and the
// size cap, checked through renderToStaticMarkup, and the real 3.5.0 page.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { performance } from "node:perf_hooks";
import { build } from "esbuild";

const result = await build({
  stdin: {
    contents: `export { ReleaseNotes, parseReleaseNotes } from './src/releaseNotes';`,
    loader: "tsx", resolveDir: new URL("..", import.meta.url).pathname,
  },
  bundle: true, platform: "node", format: "cjs", write: false,
  jsx: "automatic", loader: { ".css": "empty", ".svg": "text" },
  external: ["react", "react-dom", "react-dom/server"], logLevel: "silent",
});
const out = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(createRequire(import.meta.url), out, out.exports);
const { ReleaseNotes, parseReleaseNotes } = out.exports;
const require = createRequire(import.meta.url);
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const html = (text) => renderToStaticMarkup(React.createElement(ReleaseNotes, { text }));
const textOf = (markup) => markup.replace(/<[^>]+>/g, "");

// Headings: one and two # are h3, deeper is h4.
const heads = html("# Title\n\n## Section\n\n### Detail");
assert.ok(heads.includes("<h3>Title</h3>") && heads.includes("<h3>Section</h3>") && heads.includes("<h4>Detail</h4>"), heads);

// Nested bold and inline code inside list items; a blank line and an indented
// continuation never split the list.
const list = html("- **first `code` item**\n- second\n\n- third\n  continues\n\n1. one\n2. two");
assert.equal(list, "<ul><li><strong>first <code>code</code> item</strong></li><li>second</li><li>third continues</li></ul>" +
  "<ol><li>one</li><li>two</li></ol>", list);

// Links render their text only; bare URLs stay plain text.
const link = html("See [the release](https://example.com/x) and https://example.com/plain");
assert.ok(!link.includes("<a"), link);
assert.ok(link.includes("the release") && link.includes("https://example.com/plain"), link);

// HTML in the notes is text, never elements.
const hostile = html('<img src=x onerror=alert(1)> and <script>alert(2)</script>');
assert.ok(!/<\s*(img|script)/i.test(hostile), hostile);
assert.ok(hostile.includes("&lt;img") && hostile.includes("&lt;script&gt;"), hostile);

// Unclosed markers render literally, as do intra-word underscores.
const unclosed = html("a ** b and __half and snake_case_name and *closed*");
assert.ok(unclosed.includes("a ** b and __half and snake_case_name and <em>closed</em>"), unclosed);
assert.ok(!unclosed.includes("<strong>"), unclosed);

// Backslash escapes keep marker characters literal.
assert.equal(html("\\*no emphasis\\*"), "<p>*no emphasis*</p>");

// CRLF input normalizes; --- rules drop out.
const crlf = html("## Hi\r\n\r\n- one\r\n- two\r\n\r\n---\r\n\r\nparagraph");
assert.equal(crlf, "<h3>Hi</h3><ul><li>one</li><li>two</li></ul><p>paragraph</p>", crlf);

// Fenced code blocks keep their text as is.
const fence = html("```sql\nSELECT **not bold**\n```");
assert.equal(fence, "<pre><code>SELECT **not bold**</code></pre>", fence);

// The cap renders up to 100,000 characters and names the rest.
const capped = html("x".repeat(100_001));
assert.ok(capped.endsWith("<p>The rest of the notes is on GitHub.</p>"), capped.slice(-120));
assert.ok(capped.length < 101_000, String(capped.length));
assert.equal(parseReleaseNotes("short").length, 1);

// Exercise real React hook reuse through server render-phase updates. Calling
// ReleaseNotes here puts its hook in the probe's retained hook list.
const originalUseMemo = React.useMemo;
let memoComputations = 0;
React.useMemo = (create, dependencies) => originalUseMemo(() => {
  memoComputations++;
  return create();
}, dependencies);
function RerenderNotes({ changeText }) {
  const [tick, setTick] = React.useState(0);
  if (tick < 2) setTick(tick + 1);
  return ReleaseNotes({ text: changeText && tick > 0 ? "**new**" : "**same**" });
}
try {
  assert.equal(renderToStaticMarkup(React.createElement(RerenderNotes, { changeText: false })),
    "<p><strong>same</strong></p>");
  assert.equal(memoComputations, 1, "three renders with the same text parse once");
  memoComputations = 0;
  assert.equal(renderToStaticMarkup(React.createElement(RerenderNotes, { changeText: true })),
    "<p><strong>new</strong></p>");
  assert.equal(memoComputations, 2, "changed text parses again and then reuses the new blocks");
} finally {
  React.useMemo = originalUseMemo;
}

// Every case stays within the cap and must finish parsing and rendering within
// one second. Escaped markers lose their backslashes; other text stays literal.
const pathological = [
  ["unclosed brackets", "x[".repeat(50000)],
  ["unclosed link destinations", "[a](".repeat(25000)],
  ["rejected strong asterisks", "**x ".repeat(25000)],
  ["rejected strong underscores", "__x ".repeat(25000)],
  ["escaped backtick candidates", "`" + "a\\`".repeat(30000), "`" + "a`".repeat(30000)],
  ["rejected emphasis asterisks", "*a ".repeat(33000)],
  ["intra-word underscores", "_a".repeat(49500)],
  ["shared closing bracket without parenthesis", "[".repeat(99998) + "]("],
];
for (const [name, input, expected = input.trim()] of pathological) {
  const originalIndexOf = String.prototype.indexOf;
  const markers = new Set(["`", "*", "**", "_", "__", "](", ")"]);
  let scanned = 0;
  let markup;
  let elapsed;
  try {
    // Bound the searched spans as well as wall time: fast native searches can
    // otherwise hide quadratic work within the one-second timing allowance.
    String.prototype.indexOf = function (marker, from = 0) {
      const at = originalIndexOf.call(this, marker, from);
      if (markers.has(marker)) scanned += (at === -1 ? this.length : at + marker.length) - from;
      return at;
    };
    const start = performance.now();
    markup = html(input);
    elapsed = performance.now() - start;
  } finally {
    String.prototype.indexOf = originalIndexOf;
  }
  assert.ok(scanned <= 10 * input.length, `${name}: ${scanned} searched characters exceeds the linear budget`);
  assert.ok(elapsed < 1000, `${name}: ${elapsed.toFixed(2)} ms, expected < 1000 ms`);
  assert.ok(markup.startsWith("<p>"), name);
  assert.ok(!/<(?:strong|em|code)>/.test(markup), name);
  assert.equal(markup, `<p>${expected}</p>`, name);
  console.log(`release notes timing: ${name}: ${elapsed.toFixed(2)} ms (${input.length} characters)`);
}

// The real 3.5.0 notes render: no raw marks left in the text, one li per bullet.
const page = await readFile(new URL("../../../docs/release-notes/3.5.0.md", import.meta.url), "utf8");
const rendered = html(page);
const renderedText = textOf(rendered);
assert.ok(!renderedText.includes("**"), "no bold marks left in the rendered text");
assert.ok(!renderedText.split("\n").some((l) => l.trimStart().startsWith("##")), "no heading marks left in the rendered text");
const bullets = page.split("\n").filter((l) => /^- /.test(l)).length;
assert.ok(bullets > 30, `the page has bullets, got ${bullets}`);
assert.equal(rendered.match(/<li>/g).length, bullets, "one li per bullet in the page");
assert.ok(!/<\s*(img|script|a)\b/i.test(rendered), "no img, script or anchor elements");

console.log(`release notes unit: ok (${bullets} bullets from 3.5.0)`);
