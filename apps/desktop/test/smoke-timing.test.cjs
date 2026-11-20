'use strict';
// The smoke timing helper (smoke-timing.cjs) is pure: the clock is injected.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { SmokePhases } = require('../smoke-timing.cjs');

function clock(start = 1000) {
  let now = start;
  return { now: () => now, advance: ms => { now += ms; } };
}

test('phases accumulate per name in first-use order across repeated stretches', () => {
  const c = clock(), timing = new SmokePhases(c.now);
  timing.begin('ready');
  c.advance(800);
  timing.begin('cookie');
  c.advance(20);
  timing.begin('session');
  c.advance(10);
  timing.begin('ready'); // after the reload the same names run again
  c.advance(40);
  timing.begin('cookie');
  c.advance(5);
  timing.end();
  assert.equal(timing.elapsed('ready'), 840);
  assert.equal(timing.elapsed('cookie'), 25);
  assert.equal(timing.elapsed('session'), 10);
  assert.equal(timing.elapsed('tools'), 0);
  assert.equal(timing.format(), 'ready 840 ms, cookie 25 ms, session 10 ms');
});

test('begin with the current phase keeps counting instead of restarting it', () => {
  const c = clock(), timing = new SmokePhases(c.now);
  timing.begin('ready');
  c.advance(100);
  timing.begin('ready');
  c.advance(100);
  timing.end();
  assert.equal(timing.elapsed('ready'), 200);
  assert.equal(timing.currentElapsed(), 0);
});

test('phase changes report the name and milliseconds since the start; repeats stay silent', () => {
  const c = clock(), progress = [];
  const timing = new SmokePhases(c.now, (name, ms) => progress.push(`${name} at ${ms} ms`));
  c.advance(40); // time before the first phase still counts from construction
  timing.begin('ready');
  c.advance(100);
  timing.begin('ready'); // already current: no line
  c.advance(50);
  timing.begin('cookie');
  assert.deepEqual(progress, ['ready at 40 ms', 'cookie at 190 ms']);
});

test('a deadline failure names the phase in progress and every completed phase', () => {
  const c = clock(), timing = new SmokePhases(c.now);
  timing.begin('ready');
  c.advance(840);
  timing.begin('cookie');
  c.advance(15);
  timing.begin('session');
  c.advance(22);
  timing.begin('tools');
  c.advance(1310);
  timing.begin('reload');
  c.advance(610);
  timing.begin('ready');
  c.advance(612);
  assert.equal(timing.describeTimeout(20000),
    'The 20 s readiness budget ran out in phase "ready" (612 ms into it); ' +
    'completed phases: ready 840 ms, cookie 15 ms, session 22 ms, tools 1310 ms, reload 610 ms.');
});

test('a deadline failure with nothing completed yet says so', () => {
  const c = clock(), timing = new SmokePhases(c.now);
  timing.begin('ready');
  c.advance(300);
  assert.equal(timing.describeTimeout(20000),
    'The 20 s readiness budget ran out in phase "ready" (300 ms into it); no phase had completed.');
  timing.end();
  assert.equal(timing.describeTimeout(20000),
    'The 20 s readiness budget ran out between phases; completed phases: ready 300 ms.');
});

test('the smoke wiring uses the helper, and the packaged app includes it', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8');
  assert.match(main, /require\('\.\/smoke-timing\.cjs'\)/);
  assert.match(main, /deadline \+ timing\.elapsed\('tools'\)/);
  assert.match(main, /Desktop smoke phase: /);
  assert.match(main, /Desktop smoke timings: /);
  const config = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'electron-builder.json'), 'utf8'));
  assert.ok(config.files.includes('smoke-timing.cjs'), 'smoke-timing.cjs is packaged');
});
