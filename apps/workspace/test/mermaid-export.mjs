import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const workspace = fileURLToPath(new URL("../", import.meta.url));
const root = fileURLToPath(new URL("../../../", import.meta.url));
const generated = spawnSync(process.env.PYTHON || "python3", [path.join(workspace, "test", "mermaid_export_cases.py")], {
  cwd: root, encoding: "utf8", maxBuffer: 16 * 1024 * 1024,
});
assert.equal(generated.status, 0, generated.stderr || "Could not generate Mermaid export cases");
const cases = JSON.parse(generated.stdout);
const dollarLabels = spawnSync(process.env.PYTHON || "python3", ["-c", `
import json
from myflames import schema_diagram as d
model = d.import_sql('''CREATE TABLE \u0060$$x$$\u0060 (\u0060$$column$$\u0060 INT PRIMARY KEY);
  CREATE TABLE child(parent_id INT, CONSTRAINT \u0060$$relationship$$\u0060
  FOREIGN KEY(parent_id) REFERENCES \u0060$$x$$\u0060(\u0060$$column$$\u0060));''')
print(json.dumps(d.export_mermaid(model)))
`], { cwd: root, encoding: "utf8", maxBuffer: 16 * 1024 * 1024 });
assert.equal(dollarLabels.status, 0, dollarLabels.stderr || "Could not generate dollar-label export");
cases.push({ name: "dollar labels remain text", ...JSON.parse(dollarLabels.stdout),
  expectedText: ["$$x$$", "name: $$column$$", "$$relationship$$"] });
const browser = await puppeteer.launch({ headless: "shell", args: ["--no-sandbox", "--disable-gpu"] });
let results = [];
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1200, height: 800 });
  await page.setContent('<!doctype html><html><body><div id="preview"></div></body></html>');
  await page.addScriptTag({ path: path.join(workspace, "node_modules", "mermaid", "dist", "mermaid.min.js") });
  await page.evaluate(() => mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "default" }));
  for (const [index, item] of cases.entries()) {
    const result = await page.evaluate(async ({ text, index, expectedText }) => {
      const outcome = { parse: true, render: true, parseError: "", renderError: "" };
      try { await mermaid.parse(text); }
      catch (error) { outcome.parse = false; outcome.parseError = String(error?.message || error); }
      try {
        const rendered = await mermaid.render(`mermaid_export_${index}`, text);
        const preview = document.getElementById("preview");
        preview.innerHTML = rendered.svg;
        if (!preview.querySelector("svg")) throw new Error("Mermaid returned no SVG");
        if (expectedText) {
          for (const label of expectedText) {
            if (!preview.textContent.includes(label)) throw new Error(`Missing literal label: ${label}`);
          }
          if (preview.querySelector(".katex, math")) throw new Error("Dollar labels rendered as KaTeX math");
        }
        preview.textContent = "";
      } catch (error) { outcome.render = false; outcome.renderError = String(error?.message || error); }
      return outcome;
    }, { text: item.text, index, expectedText: item.expectedText });
    results.push({ name: item.name, ...result });
  }
} finally {
  await browser.close();
}

const width = Math.max(...results.map(result => result.name.length), 4);
console.log(`${"Case".padEnd(width)}  Parse  Render`);
console.log(`${"-".repeat(width)}  -----  ------`);
for (const result of results) {
  console.log(`${result.name.padEnd(width)}  ${result.parse ? "ok" : "FAIL"}     ${result.render ? "ok" : "FAIL"}`);
  if (!result.parse) console.log(`  parse: ${result.parseError}`);
  if (!result.render) console.log(`  render: ${result.renderError}`);
}
const failed = results.filter(result => !result.parse || !result.render);
assert.deepEqual(failed, [], `${failed.length} Mermaid export cases failed`);
console.log(`mermaid export: ${results.length} cases parse and render with Mermaid 11.17.2`);
