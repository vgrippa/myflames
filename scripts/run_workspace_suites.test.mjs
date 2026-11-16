import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const RUNNER = path.join(ROOT, 'scripts', 'run_workspace_suites.mjs');
const SHA = '0123456789abcdef0123456789abcdef01234567';
const pass = 'console.log("PASS_MARKER");';

async function fixture(t, specifications = { 'pass.mjs': { source: pass } }) {
  // Keep the fake checkout and all subprocess temporary files inside this copy.
  const root = await fs.mkdtemp(path.join(ROOT, '.workspace-runner-test-'));
  t.after(async () => {
    // Also clean up if an assertion or the runner watchdog fires. All these
    // pids were written exclusively by fixture processes that this test starts.
    for (const name of ['started-pids.jsonl', 'owned-pids.json', 'owned-tree.json', 'owned-child.json']) {
      let pids = [];
      try {
        const contents = await fs.readFile(path.join(root, name), 'utf8');
        const record = name.endsWith('.jsonl') ? contents.trim().split('\n').filter(Boolean).map(Number) : JSON.parse(contents);
        pids = (Array.isArray(record) ? record : [record]).map((entry) => Array.isArray(entry) ? entry[0] : entry);
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      for (const pid of pids) {
        assert.ok(Number.isSafeInteger(pid) && pid > 0 && pid !== process.pid);
        try { process.kill(pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
      }
    }
    await fs.rm(root, { recursive: true, force: true });
  });
  const workspace = path.join(root, 'apps', 'workspace');
  const testDir = path.join(workspace, 'test');
  const scripts = path.join(root, 'scripts');
  const temporary = path.join(root, 'temporary');
  const bin = path.join(root, 'bin');
  await Promise.all([fs.mkdir(testDir, { recursive: true }), fs.mkdir(scripts), fs.mkdir(temporary), fs.mkdir(bin)]);
  const entries = {};
  for (const [file, specification] of Object.entries(specifications)) {
    const kind = specification.kind || 'suite';
    entries[file] = { kind, script: null, reason: 'Runner regression fixture.' };
    if (kind === 'suite' || kind === 'live') entries[file].timeout = specification.timeout ?? 5;
    if (Object.hasOwn(specification, 'quarantine')) entries[file].quarantine = specification.quarantine;
    if (specification.source === null) await fs.mkdir(path.join(testDir, file));
    else {
      const recordPid = kind === 'suite' || kind === 'live'
        ? `import { appendFileSync as recordFixturePid } from 'node:fs';\nrecordFixturePid(${JSON.stringify(path.join(root, 'started-pids.jsonl'))}, process.pid + '\\n');\n` : '';
      await fs.writeFile(path.join(testDir, file), recordPid + (specification.source ?? pass));
    }
  }
  const manifest = path.join(scripts, 'workspace-suites.json');
  await fs.writeFile(manifest, JSON.stringify({ version: 1, entries }));
  const env = { ...process.env, TMPDIR: temporary, TMP: temporary, TEMP: temporary,
    PATH: `${bin}${path.delimiter}${process.env.PATH || ''}` };
  return { root, workspace, testDir, scripts, temporary, bin, manifest, entries, env };
}

async function executable(fake, name, source) {
  const file = path.join(fake.bin, `${name}.mjs`);
  await fs.writeFile(file, source);
  if (process.platform === 'win32') {
    await fs.writeFile(path.join(fake.bin, `${name}.cmd`), `@"${process.execPath}" "${file}" %*\r\n`);
  } else {
    await fs.writeFile(path.join(fake.bin, name), `#!/usr/bin/env node\n${source}`, { mode: 0o755 });
  }
}

async function fakeGit(fake, { top = fake.root, dirty = false } = {}) {
  await executable(fake, 'git', `
const args = process.argv.slice(2);
if (args.includes('--show-toplevel')) console.log(${JSON.stringify(top)});
else if (args.includes('--verify')) console.log(${JSON.stringify(SHA)});
else if (args.includes('status')) process.stdout.write(${JSON.stringify(dirty ? ' M changed.txt\n' : '')});
else process.exitCode = 2;
`);
}

async function run(fake, args = [], { build = false, signalAfter, signalFile, deadline = 15_000 } = {}) {
  const child = spawn(process.execPath, [RUNNER, '--root', fake.root, ...(build ? [] : ['--no-build']), ...args],
    { cwd: ROOT, env: fake.env, stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '';
  let stderr = '';
  child.stdout.setEncoding('utf8').on('data', (data) => { stdout += data; });
  child.stderr.setEncoding('utf8').on('data', (data) => { stderr += data; });
  let signalTimer;
  if (signalAfter) signalTimer = setTimeout(() => child.kill('SIGTERM'), signalAfter);
  // Signal only after the fixture is ready, independently of runner startup load.
  if (signalFile) signalTimer = setInterval(() => {
    fs.access(signalFile).then(() => {
      clearInterval(signalTimer);
      child.kill('SIGTERM');
    }, () => {});
  }, 25);
  // A broken runner must fail this test rather than hang the test process.
  const watchdog = setTimeout(() => child.kill('SIGKILL'), deadline);
  try {
    return await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.once('close', (code, signal) => resolve({ code, signal, stdout, stderr, output: stdout + stderr }));
    });
  } finally {
    clearTimeout(watchdog);
    clearTimeout(signalTimer);
  }
}

const listed = (result) => result.stdout.trim().split('\n').filter(Boolean);
const logDirectory = (result) => /^Workspace suite logs: (.+)$/m.exec(result.stdout)?.[1];

test('inventory mismatch reports both lists before a build, suite, or selection', async (t) => {
  const fake = await fixture(t, { 'pass.mjs': { source: 'throw new Error("SUITE_MUST_NOT_RUN");' } });
  await fs.writeFile(path.join(fake.testDir, 'unlisted.mjs'), pass);
  fake.entries['missing.mjs'] = { kind: 'suite', script: null, timeout: 5, reason: 'Missing fixture.' };
  await fs.writeFile(fake.manifest, JSON.stringify({ version: 1, entries: fake.entries }));
  await executable(fake, 'npm', 'console.error("BUILD_MUST_NOT_RUN"); process.exitCode = 9;');
  const result = await run(fake, ['--only', 'pass.mjs'], { build: true });
  assert.equal(result.code, 2, result.output);
  assert.match(result.output, /Unlisted test entries:\n  unlisted\.mjs/);
  assert.match(result.output, /Missing test entries:\n  missing\.mjs/);
  assert.doesNotMatch(result.output, /BUILD_MUST_NOT_RUN|SUITE_MUST_NOT_RUN|Workspace suite logs/);
  assert.equal((await run(fake, ['--list'])).code, 2);
});

test('failures and timeouts remain failures, print their last 60 lines, and enter the receipt', async (t) => {
  const fake = await fixture(t, {
    'pass.mjs': { source: pass },
    'fail.mjs': { source: 'for (let n = 0; n < 75; n++) console.error(`FAIL_LINE_${n}`); process.exitCode = 7;' },
    'hang.mjs': { source: 'console.error("HANG_TAIL"); setInterval(() => {}, 1000);', timeout: 0.35 },
  });
  await fakeGit(fake);
  const receiptPath = path.join(fake.root, 'receipt.json');
  const result = await run(fake, ['--jobs', '3', '--receipt', receiptPath, '--log-dir', path.join(fake.root, 'logs')]);
  assert.equal(result.code, 1, result.output);
  assert.match(result.stderr, /fail\.mjs \(FAILED; exit 7\)/);
  assert.match(result.stderr, /hang\.mjs \(TIMED OUT;/);
  assert.match(result.stderr, /FAIL_LINE_15\n/);
  assert.match(result.stderr, /FAIL_LINE_74/);
  assert.doesNotMatch(result.stderr, /FAIL_LINE_14\n/);
  assert.match(result.stderr, /HANG_TAIL/);
  const logs = await fs.readdir(logDirectory(result));
  assert.deepEqual(logs.sort(), ['fail.mjs.log', 'hang.mjs.log', 'pass.mjs.log']);
  assert.match(await fs.readFile(path.join(logDirectory(result), 'fail.mjs.log'), 'utf8'), /FAIL_LINE_0\n/);
  const receipt = JSON.parse(await fs.readFile(receiptPath, 'utf8'));
  assert.deepEqual(receipt.counts, { passed: 1, failed: 1, timed_out: 1, quarantined_failed: 0, quarantined_passed: 0 });
  assert.equal(receipt.exit_status, result.code);
  assert.equal(receipt.commit, SHA);
  assert.equal(receipt.clean_tree, true);
  assert.equal(receipt.node, process.version);
  assert.ok(Date.parse(receipt.started) <= Date.parse(receipt.finished));
  assert.match(receipt.command, /run_workspace_suites\.mjs/);
  assert.deepEqual(Object.fromEntries(receipt.suites.map((suite) => [suite.file, suite.status])),
    { 'pass.mjs': 'passed', 'fail.mjs': 'failed', 'hang.mjs': 'timed_out' });
  assert.ok(receipt.suites.every((suite) => Number.isFinite(suite.seconds) && suite.seconds >= 0));
  assert.ok((await fs.readdir(fake.temporary)).every((file) => !file.startsWith('myflames-workspace-home-')));
});

test('quarantined failures and timeouts still run, retain their tails, and do not fail the run', async (t) => {
  const quarantine = 'Known failure on 44ff65de; fix tracked in the QA plan Phase 2 follow-ups.';
  const fake = await fixture(t, {
    'pass.mjs': {},
    'fail.mjs': { quarantine,
      source: 'for (let n = 0; n < 75; n++) console.error(`QUARANTINE_LINE_${n}`); process.exitCode = 7;' },
    'hang.mjs': { quarantine, source: 'console.error("QUARANTINE_HANG"); setInterval(() => {}, 1000);', timeout: 0.35 },
  });
  await fakeGit(fake);
  const receiptPath = path.join(fake.root, 'receipt.json');
  const result = await run(fake, ['--jobs', '3', '--receipt', receiptPath]);
  assert.equal(result.code, 0, result.output);
  assert.match(result.stderr, /Quarantined failures:/);
  assert.ok(result.stderr.includes(quarantine));
  assert.match(result.stderr, /fail\.mjs \(FAILED; exit 7\)/);
  assert.match(result.stderr, /hang\.mjs \(TIMED OUT;/);
  assert.match(result.stderr, /QUARANTINE_LINE_15\n/);
  assert.match(result.stderr, /QUARANTINE_LINE_74/);
  assert.doesNotMatch(result.stderr, /QUARANTINE_LINE_14\n/);
  assert.match(result.stderr, /QUARANTINE_HANG/);
  assert.match(await fs.readFile(path.join(logDirectory(result), 'fail.mjs.log'), 'utf8'), /QUARANTINE_LINE_0\n/);
  const receipt = JSON.parse(await fs.readFile(receiptPath, 'utf8'));
  assert.deepEqual(receipt.counts, { passed: 1, failed: 0, timed_out: 0, quarantined_failed: 2, quarantined_passed: 0 });
  assert.equal(receipt.exit_status, 0);
  assert.deepEqual(Object.fromEntries(receipt.suites.map((suite) => [suite.file, suite.status])),
    { 'pass.mjs': 'passed', 'fail.mjs': 'quarantined-failed', 'hang.mjs': 'quarantined-failed' });
  // No outright pass is required when every selected suite is quarantined.
  const onlyQuarantined = await run(fake, ['--only', 'fail.mjs,hang.mjs', '--receipt', receiptPath]);
  assert.equal(onlyQuarantined.code, 0, onlyQuarantined.output);
  assert.deepEqual(JSON.parse(await fs.readFile(receiptPath, 'utf8')).counts,
    { passed: 0, failed: 0, timed_out: 0, quarantined_failed: 2, quarantined_passed: 0 });
});

test('quarantined passes request quarantine removal and have their own receipt status', async (t) => {
  const quarantine = 'Intermittent failure on 44ff65de; fix tracked in the QA plan Phase 2 follow-ups.';
  const fake = await fixture(t, { 'pass.mjs': { quarantine } });
  await fakeGit(fake);
  const receiptPath = path.join(fake.root, 'receipt.json');
  const result = await run(fake, ['--receipt', receiptPath]);
  assert.equal(result.code, 0, result.output);
  assert.match(result.stdout, /pass\.mjs: passed while quarantined; remove the quarantine/);
  assert.ok(result.stdout.includes(quarantine));
  assert.doesNotMatch(result.stderr, /Quarantined failures:|=== pass\.mjs/);
  const receipt = JSON.parse(await fs.readFile(receiptPath, 'utf8'));
  assert.deepEqual(receipt.counts, { passed: 0, failed: 0, timed_out: 0, quarantined_failed: 0, quarantined_passed: 1 });
  assert.equal(receipt.suites[0].status, 'quarantined-passed');
  assert.equal(receipt.exit_status, 0);
});

test('quarantine cannot hide an ordinary suite failure or timeout', async (t) => {
  const fake = await fixture(t, {
    'quarantined.mjs': { quarantine: 'Known failure on 44ff65de; tracked in QA.', source: 'process.exitCode = 7;' },
    'ordinary.mjs': { source: 'console.error("ORDINARY_FAILURE"); process.exitCode = 9;' },
    'timeout.mjs': { source: 'console.error("ORDINARY_TIMEOUT"); setInterval(() => {}, 1000);', timeout: 0.35 },
  });
  await fakeGit(fake);
  const receiptPath = path.join(fake.root, 'receipt.json');
  const result = await run(fake, ['--jobs', '3', '--receipt', receiptPath]);
  assert.equal(result.code, 1, result.output);
  assert.match(result.stderr, /ordinary\.mjs \(FAILED; exit 9\)/);
  assert.match(result.stderr, /timeout\.mjs \(TIMED OUT;/);
  assert.match(result.stderr, /ORDINARY_FAILURE/);
  assert.match(result.stderr, /ORDINARY_TIMEOUT/);
  const receipt = JSON.parse(await fs.readFile(receiptPath, 'utf8'));
  assert.deepEqual(receipt.counts, { passed: 0, failed: 1, timed_out: 1, quarantined_failed: 1, quarantined_passed: 0 });
  assert.equal(receipt.exit_status, 1);
});

test('quarantine requires a nonempty one-line string and is rejected on every non-suite kind', async (t) => {
  const fake = await fixture(t);
  for (const quarantine of ['', ' \t ', null, false, 0, [], {}, 'first\nsecond', 'first\rsecond',
    'first\u0085second', 'first\u2028second', 'first\u2029second']) {
    fake.entries['pass.mjs'].quarantine = quarantine;
    await fs.writeFile(fake.manifest, JSON.stringify({ version: 1, entries: fake.entries }));
    const result = await run(fake, ['--list']);
    assert.equal(result.code, 2, result.output);
    assert.match(result.stderr, /quarantine must be a nonempty, one-line string/);
    assert.doesNotMatch(result.stdout, /Workspace suite logs/);
  }
  for (const kind of ['live', 'helper', 'generator', 'fixture']) {
    fake.entries['pass.mjs'] = { kind, script: null, reason: 'Fixture.', quarantine: 'Failure on 44ff65de; tracked in QA.' };
    if (kind === 'live') fake.entries['pass.mjs'].timeout = 5;
    await fs.writeFile(fake.manifest, JSON.stringify({ version: 1, entries: fake.entries }));
    const result = await run(fake, ['--list']);
    assert.equal(result.code, 2, result.output);
    assert.match(result.stderr, /only suite entries may have quarantine/);
    assert.doesNotMatch(result.stdout, /Workspace suite logs/);
  }
});

test('an interrupted all-quarantined run remains nonzero and cannot pass with unstarted suites', async (t) => {
  const quarantine = 'Known failure on 44ff65de; tracked in QA.';
  const fake = await fixture(t, {
    'a-hang.mjs': { quarantine, source: `
import fs from 'node:fs';
console.error('QUARANTINE_STARTED');
fs.writeFileSync('../../quarantine-ready', 'ready');
setInterval(() => {}, 1000);
` },
    'b-pass.mjs': { quarantine },
  });
  const result = await run(fake, ['--jobs', '1'], { signalFile: path.join(fake.root, 'quarantine-ready') });
  assert.equal(result.code, 1, result.output);
  assert.match(result.stderr, /QUARANTINE_STARTED/);
  assert.match(result.stdout, /1\/2 ran/);
  assert.deepEqual(await fs.readdir(logDirectory(result)), ['a-hang.mjs.log']);
});

test('each suite gets a fresh HOME, USERPROFILE, TMPDIR and allowlisted environment, then cleanup', async (t) => {
  const source = `
import fs from 'node:fs';
import path from 'node:path';
fs.writeFileSync(path.join(process.env.HOME, 'suite-home-write'), 'isolated');
const name = path.basename(new URL(import.meta.url).pathname);
fs.writeFileSync(path.resolve('../../' + name + '.json'), JSON.stringify(process.env));
`;
  const fake = await fixture(t, { 'home-a.mjs': { source }, 'home-b.mjs': { source } });
  Object.assign(fake.env, { MYFLAMES_SECRET_BACKEND: 'keychain', CI: 'runner-test',
    MYFLAMES_NO_SANDBOX: '1', MYFLAMES_BROWSER_ARGS: '--disable-gpu', PUPPETEER_EXECUTABLE_PATH: '/fixture/chrome',
    NODE_OPTIONS: '--no-warnings', MYSQL_PWD: 'must-not-leak', PRIVATE_RUNNER_TEST: 'must-not-leak' });
  const result = await run(fake, ['--jobs', '2']);
  assert.equal(result.code, 0, result.output);
  const observations = await Promise.all(['home-a.mjs', 'home-b.mjs'].map(async (file) =>
    JSON.parse(await fs.readFile(path.join(fake.root, `${file}.json`), 'utf8'))));
  assert.notEqual(observations[0].HOME, observations[1].HOME);
  for (const env of observations) {
    assert.notEqual(env.HOME, process.env.HOME);
    assert.equal(env.HOME, env.USERPROFILE);
    assert.equal(env.TMPDIR, path.join(env.HOME, 'tmp'));
    assert.equal(env.TMPDIR, env.TEMP);
    assert.equal(env.MYFLAMES_SECRET_BACKEND, 'vault');
    for (const key of ['PATH', 'CI', 'MYFLAMES_NO_SANDBOX', 'MYFLAMES_BROWSER_ARGS', 'PUPPETEER_EXECUTABLE_PATH']) {
      assert.equal(env[key], fake.env[key]);
    }
    for (const key of ['MYSQL_PWD', 'PRIVATE_RUNNER_TEST', 'NODE_OPTIONS']) assert.equal(env[key], undefined);
    await assert.rejects(fs.stat(env.HOME), { code: 'ENOENT' });
  }
});

test('longest-first scheduling and stable duration-balanced shards cover each suite once', async (t) => {
  const fake = await fixture(t, {
    'a.mjs': {}, 'b.mjs': {}, 'c.mjs': {}, 'd.mjs': {}, 'e.mjs': {}, 'f.mjs': {},
    'live.mjs': { kind: 'live' }, 'helper.mjs': { kind: 'helper' },
    'generator.mjs': { kind: 'generator' }, fixtures: { kind: 'fixture', source: null },
  });
  const durations = { 'a.mjs': 100, 'b.mjs': 80, 'c.mjs': 50, 'd.mjs': 30, 'e.mjs': 20, 'f.mjs': 0 };
  const durationsPath = path.join(fake.scripts, 'workspace_suite_durations.json');
  await fs.writeFile(durationsPath, JSON.stringify(durations));
  const full = await run(fake, ['--list']);
  assert.equal(full.code, 0, full.output);
  assert.deepEqual(listed(full), Object.keys(durations));
  const shards = [];
  for (let index = 1; index <= 3; index += 1) {
    const result = await run(fake, ['--list', '--shard', `${index}/3`]);
    assert.equal(result.code, 0, result.output);
    shards.push(listed(result));
  }
  assert.deepEqual(shards, [['a.mjs'], ['b.mjs', 'e.mjs'], ['c.mjs', 'd.mjs', 'f.mjs']]);
  assert.deepEqual(shards.flat().sort(), Object.keys(durations));
  assert.deepEqual(listed(await run(fake, ['--list', '--shard', '2/3'])), shards[1]);
  assert.deepEqual(listed(await run(fake, ['--kind', 'live', '--list'])), ['live.mjs']);
  assert.equal((await run(fake, ['--kind', 'live'])).code, 0);
  // A list has no build, logs, worker HOME, duration writes or receipt side effects.
  const outputPath = path.join(fake.root, 'list-must-not-write.json');
  assert.equal((await run(fake, ['--list', '--durations-out', outputPath, '--receipt', outputPath])).code, 0);
  await assert.rejects(fs.stat(outputPath), { code: 'ENOENT' });
  assert.equal(await fs.readFile(durationsPath, 'utf8'), JSON.stringify(durations));
});

test('missing duration weights default to 60 seconds; equal and zero weights shard deterministically', async (t) => {
  const fake = await fixture(t, { 'a.mjs': {}, 'b.mjs': {}, 'c.mjs': {}, 'd.mjs': {} });
  const custom = path.join(fake.root, 'custom.json');
  await fs.writeFile(custom, JSON.stringify({ 'a.mjs': 0, 'b.mjs': 70, 'd.mjs': 0 }));
  assert.deepEqual(listed(await run(fake, ['--list', '--durations', custom])), ['b.mjs', 'c.mjs', 'a.mjs', 'd.mjs']);
  await fs.writeFile(custom, JSON.stringify({ 'a.mjs': 0, 'b.mjs': 0, 'c.mjs': 0, 'd.mjs': 0 }));
  assert.deepEqual(listed(await run(fake, ['--list', '--durations', custom, '--shard', '1/2'])), ['a.mjs', 'c.mjs']);
  assert.deepEqual(listed(await run(fake, ['--list', '--durations', custom, '--shard', '2/2'])), ['b.mjs', 'd.mjs']);
});

test('--only rejects every non-suite and unknown file, and runs only named suites', async (t) => {
  const fake = await fixture(t, { 'pass.mjs': {}, 'helper.mjs': { kind: 'helper' }, 'live.mjs': { kind: 'live' },
    'generator.mjs': { kind: 'generator' }, fixtures: { kind: 'fixture', source: null } });
  for (const file of ['helper.mjs', 'live.mjs', 'generator.mjs', 'fixtures', 'missing.mjs', '../pass.mjs']) {
    const result = await run(fake, ['--only', file]);
    assert.equal(result.code, 2, result.output);
    assert.doesNotMatch(result.stdout, /Workspace suite logs/);
  }
  const result = await run(fake, ['--only', 'pass.mjs']);
  assert.equal(result.code, 0, result.output);
  assert.match(result.stdout, /1\/1 ran/);
  assert.deepEqual(await fs.readdir(logDirectory(result)), ['pass.mjs.log']);
});

test('a failed or timed-out build is nonzero, runs no suites, and reports its log', async (t) => {
  const fake = await fixture(t, { 'pass.mjs': { source: 'console.log("MUST_NOT_RUN");' } });
  await executable(fake, 'npm', 'console.error("BUILD_FAILURE_TAIL"); process.exitCode = 9;');
  const result = await run(fake, [], { build: true });
  assert.equal(result.code, 1, result.output);
  assert.match(result.stderr, /BUILD_FAILURE_TAIL/);
  assert.doesNotMatch(result.output, /MUST_NOT_RUN/);
  assert.deepEqual(await fs.readdir(logDirectory(result)), ['build.log']);
  await executable(fake, 'npm', 'console.error("BUILD_HANG_TAIL"); setInterval(() => {}, 1000);');
  const hung = await run(fake, ['--timeout', '0.25'], { build: true });
  assert.equal(hung.code, 1, hung.output);
  assert.match(hung.stderr, /build \(TIMED OUT;/);
  assert.match(hung.stderr, /BUILD_HANG_TAIL/);
});

test('build runs once, duration input stays unchanged, and only --durations-out writes measurements', async (t) => {
  const fake = await fixture(t, { 'a.mjs': {}, 'b.mjs': {} });
  await executable(fake, 'npm', `import fs from 'node:fs'; fs.appendFileSync('../../build-count.txt', 'built\\n');`);
  const durationPath = path.join(fake.scripts, 'workspace_suite_durations.json');
  const original = '{"a.mjs":12,"b.mjs":11}\n';
  await fs.writeFile(durationPath, original);
  const result = await run(fake, [], { build: true });
  assert.equal(result.code, 0, result.output);
  assert.equal(await fs.readFile(path.join(fake.root, 'build-count.txt'), 'utf8'), 'built\n');
  assert.equal(await fs.readFile(durationPath, 'utf8'), original);
  const measured = path.join(fake.root, 'measured.json');
  const next = await run(fake, ['--only', 'b.mjs', '--durations-out', measured]);
  assert.equal(next.code, 0, next.output);
  const seconds = JSON.parse(await fs.readFile(measured, 'utf8'));
  assert.deepEqual(Object.keys(seconds), ['b.mjs']);
  assert.ok(seconds['b.mjs'] > 0);
  assert.equal(await fs.readFile(durationPath, 'utf8'), original);
});

test('receipt records successful dirty provenance, and unavailable provenance cannot certify success', async (t) => {
  const fake = await fixture(t);
  const receiptPath = path.join(fake.root, 'receipt.json');
  await fakeGit(fake, { dirty: true });
  const success = await run(fake, ['--receipt', receiptPath]);
  assert.equal(success.code, 0, success.output);
  let receipt = JSON.parse(await fs.readFile(receiptPath, 'utf8'));
  assert.equal(receipt.commit, SHA);
  assert.equal(receipt.clean_tree, false);
  assert.deepEqual(receipt.counts, { passed: 1, failed: 0, timed_out: 0, quarantined_failed: 0, quarantined_passed: 0 });
  assert.equal(receipt.exit_status, 0);
  await fakeGit(fake, { top: path.dirname(fake.root) });
  const unavailable = await run(fake, ['--receipt', receiptPath]);
  assert.equal(unavailable.code, 1, unavailable.output);
  receipt = JSON.parse(await fs.readFile(receiptPath, 'utf8'));
  assert.equal(receipt.commit, null);
  assert.equal(receipt.clean_tree, false);
  assert.equal(receipt.exit_status, 1);
  assert.match(unavailable.stderr, /no verifiable Git SHA/);
});

test('invalid flags, invalid manifest/durations, and empty runs cannot pass', async (t) => {
  const fake = await fixture(t);
  for (const args of [['--jobs', '0'], ['--jobs', '1.5'], ['--timeout', 'NaN'], ['--timeout', '-2'],
    ['--shard', '0/2'], ['--shard', '3/2'], ['--shard', '1/0'], ['--kind', 'helper'],
    ['--only', 'pass.mjs,pass.mjs'], ['--only', 'pass.mjs,'], ['--jobs'], ['--unknown']]) {
    const result = await run(fake, args);
    assert.equal(result.code, 2, `${args.join(' ')}: ${result.output}`);
  }
  const empty = await run(fake, ['--shard', '2/2']);
  assert.equal(empty.code, 2, empty.output);
  assert.match(empty.stderr, /No suites selected/);
  assert.equal((await run(fake, ['--kind', 'live'])).code, 2);
  const durations = path.join(fake.scripts, 'workspace_suite_durations.json');
  for (const invalid of ['[]', '{"pass.mjs":true}', '{"pass.mjs":-1}', '{"pass.mjs":1e999}', '{']) {
    await fs.writeFile(durations, invalid);
    assert.equal((await run(fake, ['--list'])).code, 2);
  }
  await fs.rm(durations);
  fake.entries['pass.mjs'].timeout = 0;
  await fs.writeFile(fake.manifest, JSON.stringify({ version: 1, entries: fake.entries }));
  assert.equal((await run(fake)).code, 2);
  await fs.writeFile(fake.manifest, JSON.stringify({ version: 2, entries: {} }));
  assert.equal((await run(fake)).code, 2);
});

test('--jobs bounds actual concurrency and a timeout override applies to every suite', async (t) => {
  const source = `
import fs from 'node:fs';
const file = '../../events.jsonl';
fs.appendFileSync(file, JSON.stringify({ phase: 'start', at: Date.now(), pid: process.pid }) + '\\n');
await new Promise((resolve) => setTimeout(resolve, 100));
fs.appendFileSync(file, JSON.stringify({ phase: 'end', at: Date.now(), pid: process.pid }) + '\\n');
`;
  const fake = await fixture(t, { 'a.mjs': { source }, 'b.mjs': { source }, 'c.mjs': { source }, 'd.mjs': { source } });
  const result = await run(fake, ['--jobs', '2']);
  assert.equal(result.code, 0, result.output);
  const events = (await fs.readFile(path.join(fake.root, 'events.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  let active = 0;
  let maximum = 0;
  for (const event of events) { active += event.phase === 'start' ? 1 : -1; maximum = Math.max(maximum, active); }
  assert.equal(active, 0);
  assert.equal(maximum, 2);
  await fs.writeFile(path.join(fake.testDir, 'a.mjs'), 'console.log("OVERRIDE_HANG"); setInterval(() => {}, 1000);');
  const timeout = await run(fake, ['--only', 'a.mjs', '--timeout', '0.25']);
  assert.equal(timeout.code, 1, timeout.output);
  assert.match(timeout.stderr, /a\.mjs \(TIMED OUT;/);
});

test('timeout and interruption kill owned descendants, including detached sessions when ps is available',
  { skip: process.platform === 'win32' }, async (t) => {
    const canScan = spawnSync('ps', ['-A', '-o', 'pid=,ppid='], { encoding: 'utf8' }).status === 0;
    t.diagnostic(canScan ? 'Real ps available: detached descendants exercised.'
      : 'Real ps denied: exercised process-group fallback; detached sweep has a separate controlled-inventory test.');
    const source = `
import { spawn } from 'node:child_process';
import fs from 'node:fs';
const children = [false${canScan ? ', true' : ''}].map((detached) => spawn(process.execPath,
  ['-e', 'setInterval(() => {}, 1000)'], { detached, stdio: 'ignore' }));
fs.writeFileSync('../../owned-pids.json', JSON.stringify([process.pid, ...children.map((child) => child.pid)]));
console.error('TREE_READY');
setInterval(() => {}, 1000);
`;
    const fake = await fixture(t, { 'tree.mjs': { source, timeout: 0.5 } });
    let pids = [];
    // This fallback only signals processes whose pids our own fixture reported.
    t.after(async () => {
      try { pids = JSON.parse(await fs.readFile(path.join(fake.root, 'owned-pids.json'), 'utf8')); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      for (const pid of pids) {
        try { process.kill(pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
      }
    });
    for (const interrupted of [false, true]) {
      const result = await run(fake, interrupted ? ['--timeout', '10'] : [], interrupted ? { signalAfter: 400 } : {});
      assert.equal(result.code, 1, result.output);
      assert.match(result.stderr, /TREE_READY/);
      pids = JSON.parse(await fs.readFile(path.join(fake.root, 'owned-pids.json'), 'utf8'));
      // Reaped descendants may briefly remain zombies; those cannot run code.
      for (const pid of pids) {
        const check = spawnSync('ps', ['-p', String(pid), '-o', 'stat='], { encoding: 'utf8' });
        if (canScan) assert.ok(check.status !== 0 || !check.stdout.trim() || check.stdout.trim().startsWith('Z'),
          `owned pid ${pid} still alive: ${check.stdout}`);
        else assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
      }
    }
  });

test('parent-pid sweep stops a real detached child using a controlled ps inventory',
  { skip: process.platform === 'win32' }, async (t) => {
    const source = `
import { spawn } from 'node:child_process';
import fs from 'node:fs';
const child = spawn(process.execPath, ['-e',
  "const fs = require('node:fs'); setInterval(() => fs.appendFileSync('../../heartbeat.txt', 'x'), 10);"],
  { detached: true, stdio: 'ignore' });
fs.writeFileSync('../../owned-tree.json', JSON.stringify([[process.pid, process.ppid], [child.pid, process.pid]]));
console.error('DETACHED_READY');
setInterval(() => {}, 1000);
`;
    const fake = await fixture(t, { 'tree.mjs': { source, timeout: 0.5 } });
    t.after(async () => {
      let tree = [];
      try { tree = JSON.parse(await fs.readFile(path.join(fake.root, 'owned-tree.json'), 'utf8')); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      for (const [pid] of tree) {
        try { process.kill(pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
      }
    });
    await executable(fake, 'ps', `
import fs from 'node:fs';
const tree = JSON.parse(fs.readFileSync(${JSON.stringify(path.join(fake.root, 'owned-tree.json'))}, 'utf8'));
for (const [pid, ppid] of tree) console.log(pid + ' ' + ppid);
`);
    const result = await run(fake);
    assert.equal(result.code, 1, result.output);
    assert.match(result.stderr, /DETACHED_READY/);
    const heartbeat = path.join(fake.root, 'heartbeat.txt');
    const stopped = await fs.readFile(heartbeat, 'utf8');
    assert.ok(stopped.length > 0, 'detached child actually ran');
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.equal(await fs.readFile(heartbeat, 'utf8'), stopped, 'detached child stopped writing after timeout');
  });

test('a suite exiting successfully cannot leave children running in its process group',
  { skip: process.platform === 'win32' }, async (t) => {
    const source = `
import { spawn } from 'node:child_process';
import fs from 'node:fs';
const child = spawn(process.execPath, ['-e',
  "const fs = require('node:fs'); setInterval(() => fs.appendFileSync('../../success-heartbeat.txt', 'x'), 10);"],
  { stdio: 'ignore' });
child.unref();
fs.writeFileSync('../../owned-child.json', JSON.stringify(child.pid));
await new Promise((resolve) => setTimeout(resolve, 100));
`;
    const fake = await fixture(t, { 'parent.mjs': { source } });
    t.after(async () => {
      try {
        const pid = JSON.parse(await fs.readFile(path.join(fake.root, 'owned-child.json'), 'utf8'));
        try { process.kill(pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
    });
    const result = await run(fake);
    assert.equal(result.code, 0, result.output);
    const heartbeat = path.join(fake.root, 'success-heartbeat.txt');
    const stopped = await fs.readFile(heartbeat, 'utf8');
    assert.ok(stopped.length > 0, 'inherited-group child actually ran');
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.equal(await fs.readFile(heartbeat, 'utf8'), stopped);
  });

test('a long-running suite reports the real 30-second status heartbeat', { timeout: 40_000 }, async (t) => {
  const fake = await fixture(t, { 'waiting.mjs': {
    source: 'await new Promise((resolve) => setTimeout(resolve, 30_150));', timeout: 35,
  } });
  const result = await run(fake, [], { deadline: 37_000 });
  assert.equal(result.code, 0, result.output);
  // Elapsed time includes inventory/startup before the 30-second timer starts.
  const heartbeat = /\[0\/1 done; (\d+\.\d+)s elapsed\] running: waiting\.mjs/.exec(result.stderr);
  assert.ok(heartbeat, result.stderr);
  assert.ok(Number(heartbeat[1]) >= 30, result.stderr);
});
