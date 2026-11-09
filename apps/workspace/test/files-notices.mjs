// The dismissible Notice (kit.tsx) and the ViewStatus error dismiss in a real
// browser, without a workspace/database server. Run after reviewing
// kit-primitives.mjs: node test/files-notices.mjs.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
const here = path.dirname(fileURLToPath(import.meta.url));
const bundle = await build({ stdin: { resolveDir: path.join(here, '../src'), loader: 'tsx', contents: `
  import React, { useState } from 'react';
  import { createRoot } from 'react-dom/client';
  import { Notice, ViewStatus } from './kit';
  function Demo() {
    const [error, setError] = useState('First failure');
    const [note, setNote] = useState('A note');
    return <>
      <Notice variant="error">{error}</Notice>
      <Notice variant="note">{note}</Notice>
      <Notice variant="note" role="alert" className="notice">Assertive note</Notice>
      <Notice variant="error" className="variable-problems" noticeKey={error}>{error} in a sheet</Notice>
      <ViewStatus error={error} onRetry={() => {}} />
      <button id="change" onClick={() => setError('Second failure')}>Change error</button>
      <button id="clear" onClick={() => setError('')}>Clear error</button>
    </>;
  }
  createRoot(document.getElementById('root')).render(<Demo />);
` }, bundle: true, platform: 'browser', format: 'iife', write: false, jsx: 'automatic', loader: { '.css': 'empty' } });
const css = (await Promise.all(['style.css', 'kit.css'].map(file => readFile(path.join(here, '../src', file), 'utf8')))).join('\n');
if (process.argv.includes('--compile-only')) { console.log('files-notices browser fixture compiled; interactions not run'); process.exit(0); }
// --no-sandbox: Chrome cannot start without it in some CI containers.
const browser = await puppeteer.launch({ headless: 'shell', args: ['--no-sandbox', '--disable-dev-shm-usage'] });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.setViewport({ width: 1000, height: 900 });
  // Supply a real origin through interception, so localStorage is available.
  await page.setRequestInterception(true);
  page.on('request', request => request.respond({ status: 200, contentType: 'text/html', body: `<style>${css}</style><div id="root"></div>` }));
  await page.goto('http://kit.test/');
  await page.addScriptTag({ content: bundle.outputFiles[0].text });
  await page.waitForSelector('.notice[role="status"]');
  // The markup contract of the replaced lines holds: class and role per variant.
  assert.equal(await page.$$eval('p.error[role="alert"]', els => els.length), 1, 'the error keeps p.error[role=alert]');
  assert.equal(await page.$$eval('p.notice[role="status"]', els => els.length), 1, 'the note keeps p.notice[role=status]');
  assert.equal(await page.$$eval('p.notice[role="alert"]', els => els.length), 1, 'an assertive note keeps p.notice[role=alert]');
  assert.equal(await page.$$eval('p.variable-problems[role="alert"]', els => els.length), 1, 'a placed class replaces the variant class');
  // Every notice carries its Dismiss button inside the same element (the
  // ViewStatus error box has one too, hence five).
  assert.equal(await page.$$eval('.notice .notice-dismiss[aria-label="Dismiss"], .error .notice-dismiss[aria-label="Dismiss"], .variable-problems .notice-dismiss[aria-label="Dismiss"]', els => els.length), 5);
  // The button is icon-only: the announced text is the message alone.
  assert.equal((await page.$eval('p.error', e => e.textContent || '')).trim(), 'First failure');
  // Dismiss hides the current content; the other notices stay.
  await page.click('p.error .notice-dismiss');
  assert.equal(await page.$('p.error'), null, 'a dismissed error is gone');
  assert.equal(await page.$$eval('.notice', els => els.length), 2, 'the notes stay');
  // New content (a different message) shows again.
  await page.click('#change');
  await page.waitForSelector('p.error');
  assert.match(await page.$eval('p.error', e => e.textContent || ''), /Second failure/);
  // A message that clears and returns identical counts as new content.
  await page.click('p.error .notice-dismiss');
  await page.click('#clear');
  await page.click('#change');
  await page.waitForSelector('p.error');
  // ViewStatus: the error box gains a Dismiss beside Try again.
  await page.waitForSelector('.view-status-error .view-status-retry');
  assert.equal(await page.$$eval('.view-status-error .notice-dismiss[aria-label="Dismiss"]', els => els.length), 1);
  await page.click('.view-status-error .notice-dismiss');
  assert.equal(await page.$('.view-status-error'), null, 'a dismissed view error is gone');
  await page.click('#clear');
  await page.click('#change');
  await page.waitForSelector('.view-status-error');
  assert.deepEqual(errors, []);
  console.log('files-notices: ok');
} finally {
  await browser.close();
}
