'use strict';
// The output collector (scripts/smoke-output.cjs) is pure: chunks go in,
// a bounded scrubbed window comes out.
const test = require('node:test');
const assert = require('node:assert/strict');
const { OutputCollector, scrub } = require('../scripts/smoke-output.cjs');

test('a launch URL split across chunks leaves no secret character in the bounded window', () => {
  const token = 's3cr3tTOK_en-xyz';
  const line = `http://127.0.0.1:8000/#launch=${token} ` + 'x'.repeat(3980) + '\n';
  const collector = new OutputCollector(4000);
  // Split the URL across chunk boundaries, including inside the token.
  collector.push(line.slice(0, 10));
  collector.push(line.slice(10, 25));
  collector.push(line.slice(25, 41));
  collector.push(line.slice(41));
  const window = collector.end();
  assert.ok(window.length <= 4000);
  assert.ok(!window.includes('#launch'), 'no launch fragment survives');
  assert.ok(!window.includes(token), 'no token survives');
  // The scrubbed line is '[private URL]\n' plus the 3980 filler characters,
  // so the whole window is known: not one character of the URL or token remains.
  assert.equal(window, '[private URL] ' + 'x'.repeat(3980) + '\n');
});

test('an upper-case HTTPS scheme and a bare #launch token are scrubbed', () => {
  assert.equal(scrub('open HTTPS://EXAMPLE.COM/app#launch=Tok_1-2 now'), 'open [private URL] now');
  assert.equal(scrub('bare #launch=abc end'), 'bare #launch=[private] end');
  assert.equal(scrub('https://host/path ok'), '[private URL] ok');
});

test('the window keeps its bound across many lines', () => {
  const collector = new OutputCollector(4000);
  for (let i = 0; i < 500; i += 1) collector.push(`line ${i} ${'y'.repeat(50)}\n`);
  assert.equal(collector.read().length, 4000);
  const wide = new OutputCollector(20000);
  wide.push('z'.repeat(30000));
  assert.equal(wide.read().length, 20000);
  assert.equal(wide.end().length, 20000);
});

test('a partial last line is scrubbed when the stream ends or the window is read', () => {
  const collector = new OutputCollector(4000);
  collector.push('done\n');
  collector.push('tail http://internal:3306/#launch=partSECRET');
  assert.equal(collector.read(), 'done\ntail [private URL]');
  assert.equal(collector.end(), 'done\ntail [private URL]');
  // Reading before the end did not append the partial line twice.
  assert.equal(collector.read(), 'done\ntail [private URL]');
});
