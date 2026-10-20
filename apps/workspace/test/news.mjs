// News on Home: headlines from a mocked /api/news (the real server answers
// everything else, including the Settings switch in the workspace store).
// Links open outside the app, one calm line covers offline, empty and failed
// reads, the switch off sends no news request and hides the section, and the
// section never moves Connections or Recent work at 1440x900 or 720x520.
// Run after `npm run build`: node test/news.mjs
import assert from "node:assert/strict";
import { open, pause } from "./perf-harness.mjs";

const hours = (h) => new Date(Date.now() - h * 3600e3).toISOString().replace(/\.\d+Z$/, "Z");
const item = (title, engine, h, link) => ({
  title, link, engine, published: h === null ? null : hours(h), summary: `${title}: summary.`,
  source: engine === "mysql" ? "Planet MySQL" : "MariaDB Foundation",
  source_id: engine === "mysql" ? "planet-mysql" : "mariadb-foundation",
});
const items = [
  item("Amazic joins MariaDB Foundation as a Silver Sponsor", "mariadb", 8, "https://mariadb.org/amazic/"),
  item("Troubleshooting MySQL with OpenTelemetry Metrics", "mysql", 15, "https://blogs.oracle.com/mysql/otel"),
  item("Upgrading MySQL HeatWave High Availability from MySQL 8.0.46 to MySQL 8.4 LTS, with a title long enough to wrap twice in the column", "mysql", 50, "https://blogs.oracle.com/mysql/heatwave"),
  item("MariaDB Foundation: Exploring TidesDB Performance with TAF", "mariadb", 92, "https://mariadb.org/tidesdb/"),
  item("Just Because You Can SELECT Everything Doesn't Mean You Should", "mysql", 120, "https://blogs.oracle.com/mysql/select-star"),
  item("MariaDB Foundation Newsletter", "mariadb", null, "https://mariadb.org/newsletter/"),
];
const sources = [
  { id: "planet-mysql", name: "Planet MySQL", engine: "mysql", url: "https://planet.mysql.com/rss20.xml", fetched_at: hours(0), checked_at: hours(0), items: 3 },
  { id: "mariadb-foundation", name: "MariaDB Foundation", engine: "mariadb", url: "https://mariadb.org/feed/", fetched_at: hours(0), checked_at: hours(0), items: 3 },
];
const report = (over = {}) => ({ status: 200, body: { schema_version: "news-1.0", enabled: true, items, fetched_at: hours(0), stale: false, sources, ...over } });
const h = await open("/api/news", "news", {}, { fallback: report() });
const { page, state } = h;
const outside = [];
page.on("request", (r) => { const u = new URL(r.url()); if (u.hostname !== "127.0.0.1") outside.push(r.url()); });
const box = (selector) => page.$eval(selector, (e) => { const r = e.getBoundingClientRect(); return { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), right: Math.round(r.right) }; });
/** Refresh with *mock* as the answer and wait until the state line reads *line* ("" for none). */
async function refresh(mock, line) {
  state.queue.push(mock);
  const before = state.seen.length;
  await page.click('.home-news button[aria-label="Refresh news"]');
  for (let i = 0; i < 100 && state.seen.length === before; i++) await pause(20);
  assert.equal(state.seen.length, before + 1, "Refresh asks once");
  assert.deepEqual(state.seen.at(-1), { refresh: true });
  await page.waitForFunction((line) => (document.querySelector(".home-news .news-state")?.textContent || "") === line &&
    !document.querySelector('.home-news[aria-busy="true"]'), { timeout: 5000 }, line);
}
async function toggleNews() {
  await page.click('button[aria-label="Settings"]');
  await page.waitForSelector("#settings-popover .news-toggle input");
  // One change at a time: the switch is disabled until the stored answer.
  const disabledWhileSaving = await page.evaluate(async () => {
    const input = document.querySelector("#settings-popover .news-toggle input");
    input.click();
    for (let i = 0; i < 3; i++) await Promise.resolve();
    return input.disabled;
  });
  assert.equal(disabledWhileSaving, true, "the switch waits for its saved answer");
  await page.waitForFunction(() => !document.querySelector("#settings-popover .news-toggle input").disabled);
  const checked = await page.$eval("#settings-popover .news-toggle input", (e) => e.checked);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#settings-popover"));
  return checked;
}
async function go(place) {
  await h.button(place, ".main-nav ");
  await pause(400);
}
try {
  // Headlines: one read on Home, newest first, external links.
  await page.waitForSelector(".home-news .news-item");
  assert.deepEqual(state.seen, [{ refresh: false }], "Home asks for news once");
  const rows = await page.$$eval(".home-news .news-item", (links) => links.map((a) => ({
    href: a.href, target: a.target, rel: a.rel, title: a.querySelector("strong").textContent,
    meta: a.querySelector("small").textContent, time: a.querySelector("time")?.getAttribute("datetime") || null,
    tooltip: a.title, name: a.textContent })));
  assert.equal(rows.length, 6);
  assert.deepEqual(rows.map((r) => r.href), items.map((i) => i.link));
  for (const r of rows) {
    assert.equal(r.target, "_blank");
    assert.equal(r.rel, "noopener noreferrer");
    assert.match(r.href, /^https:\/\//);
    assert.match(r.name, /\(opens in your browser\)$/);
  }
  assert.equal(rows[0].meta, "MariaDB Foundation · 8 hours ago");
  assert.equal(rows[1].meta, "Planet MySQL · 15 hours ago");
  assert.equal(rows[2].meta, "Planet MySQL · 2 days ago");
  assert.equal(rows[5].meta, "MariaDB Foundation", "no date, no relative time");
  assert.equal(rows[1].time, items[1].published);
  assert.equal(rows[0].tooltip, items[0].summary);
  assert.equal(await page.$(".home-news .news-state"), null, "no state line with fresh headlines");
  const about = await h.popover("About News");
  assert.match(about, /Planet MySQL/);
  assert.match(about, /MariaDB Foundation/);
  assert.match(about, /Settings/);

  // Offline with a cache, empty, and a failed request: one calm line each.
  await refresh(report({ stale: true, offline: true, error: "News is unavailable offline.", fetched_at: hours(3) }),
    "News is unavailable offline. Last updated 3 hours ago.");
  assert.equal(await page.$$eval(".home-news .news-item", (l) => l.length), 6, "the cache stays on screen");
  assert.equal(await page.$eval(".home-news .news-state", (e) => e.getAttribute("role")), "status");
  await refresh(report({ items: [], stale: true, offline: true, error: "News is unavailable offline.", fetched_at: null }),
    "News is unavailable offline.");
  await refresh(report({ stale: true, offline: false, error: "Planet MySQL could not be read.", fetched_at: hours(26) }),
    "Planet MySQL could not be read. Last updated yesterday.");
  await refresh(report({ items: [] }), "No news right now.");
  assert.equal(await page.$(".home-news .news-item"), null);
  await refresh({ status: 500, body: { error: "Something failed" } }, "News could not be loaded.");
  await refresh(report(), "");
  assert.equal(await page.$$eval(".home-news .news-item", (l) => l.length), 6);

  // Layout: News sits under Recent work and never moves Connections or Recent work.
  const sizes = [[1440, 900], [720, 520]];
  const withNews = {};
  for (const [width, height] of sizes) {
    await page.setViewport({ width, height });
    await pause(300);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), 0, `no sideways scroll at ${width}`);
    const connections = await box(".home .connections"), recent = await box(".recent-work"), newsBox = await box(".home-news");
    withNews[width] = { connections, recent };
    assert.ok(newsBox.top >= recent.bottom - 1, `News below Recent work at ${width}`);
    if (width === 1440) {
      assert.equal(newsBox.left, recent.left, "same column as Recent work");
      assert.ok(newsBox.left >= connections.right - 1, "beside Connections");
      assert.ok(newsBox.bottom <= height, "the six headlines fit at 1440x900");
    } else {
      assert.ok(newsBox.top >= connections.bottom - 1, "after Connections at 720");
      assert.ok(connections.top < height, "Connections starts in view at 720x520");
    }
  }
  await page.setViewport({ width: 1440, height: 1000 });

  // The switch off: the section goes, and no news request follows anywhere.
  assert.equal(await toggleNews(), false);
  await page.waitForFunction(() => !document.querySelector(".home-news"));
  for (const [width, height] of sizes) {
    await page.setViewport({ width, height });
    await pause(300);
    const connections = await box(".home .connections"), recent = await box(".recent-work");
    assert.deepEqual(connections, withNews[width].connections, `Connections unchanged by News at ${width}`);
    assert.equal(recent.top, withNews[width].recent.top, `Recent work unchanged by News at ${width}`);
  }
  await page.setViewport({ width: 1440, height: 1000 });
  const seen = state.seen.length;
  await go("Server");
  await go("Home");
  await page.reload({ waitUntil: "networkidle0" });
  await pause(800);
  await go("Server");
  await go("Home");
  assert.equal(state.seen.length, seen, "no /api/news request while the switch is off");
  assert.equal(await page.$(".home-news"), null);
  await page.click('button[aria-label="Settings"]');
  await page.waitForSelector("#settings-popover .news-toggle input");
  assert.equal(await page.$eval("#settings-popover .news-toggle input", (e) => e.checked), false, "the switch is stored");
  assert.match(await page.$eval("#news-toggle-hint", (e) => e.textContent), /while Home is open/);
  await page.keyboard.press("Escape");

  // On again: one read, and none while another place is on screen.
  assert.equal(await toggleNews(), true);
  await page.waitForSelector(".home-news .news-item");
  assert.deepEqual(state.seen.slice(seen), [{ refresh: false }], "on again: one read, not a forced refresh");
  await go("Server");
  await pause(500);
  assert.equal(state.seen.length, seen + 1, "no read while Home is not on screen");
  // Back on Home: the last headlines show at once while the backend answers again.
  state.delay = 1500;
  await h.button("Home", ".main-nav ");
  await page.waitForSelector(".home-news .news-item", { timeout: 1000 });
  assert.equal(await page.$(".home-news .news-state"), null, "no loading line over the last headlines");
  for (let i = 0; i < 100 && state.seen.length === seen + 1; i++) await pause(20);
  assert.deepEqual(state.seen.slice(seen + 1), [{ refresh: false }]);
  state.delay = 0;
  await pause(1700);

  assert.deepEqual(outside, [], "the page never fetches a feed or another site itself");
  assert.deepEqual(h.errors, []);
  console.log("News on Home: headlines, external links, calm states, the Settings switch and layout passed.");
} finally {
  await h.close();
}
