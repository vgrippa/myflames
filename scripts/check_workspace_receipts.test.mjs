import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { checkReceipts } from './check_workspace_receipts.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SHA = '0123456789abcdef0123456789abcdef01234567';
const SCRIPT = path.join(ROOT, 'scripts', 'check_workspace_receipts.mjs');
const clone = (value) => JSON.parse(JSON.stringify(value));
function fixture() {
  const entries = Object.fromEntries(Array.from({ length: 12 }, (_, i) => [`suite-${i}.mjs`, { kind: 'suite' }]));
  entries['suite-11.mjs'].quarantine = 'Known non-safety failure, tracked in QA.';
  entries['live.mjs'] = { kind: 'live' };
  entries['helper.mjs'] = { kind: 'helper' };
  const receipts = Array.from({ length: 6 }, (_, i) => ({ exit_status: 0, commit: SHA,
    counts: { passed: i === 5 ? 1 : 2, failed: 0, timed_out: 0, quarantined_failed: i === 5 ? 1 : 0, quarantined_passed: 0 },
    suites: [i * 2, i * 2 + 1].map((n) => ({ file: `suite-${n}.mjs`, seconds: 1,
      status: n === 11 ? 'quarantined-failed' : 'passed' })) }));
  return { manifest: { version: 1, entries }, receipts };
}

test('six receipts cover only suite entries, exactly once, with manifest-authorized quarantines', () => {
  const { manifest, receipts } = fixture();
  assert.equal(checkReceipts(manifest, receipts, SHA), 12);
  receipts[5].suites[1].status = 'quarantined-passed';
  receipts[5].counts.quarantined_failed = 0;
  receipts[5].counts.quarantined_passed = 1;
  assert.equal(checkReceipts(manifest, receipts), 12);
});

test('missing/extra receipts, unsuccessful exits, omissions and duplicates fail independently of job status', () => {
  const { manifest, receipts } = fixture();
  for (const count of [0, 1, 5, 7]) {
    assert.throws(() => checkReceipts(manifest, Array.from({ length: count }, (_, i) => receipts[i % 6])), /exactly 6 receipts/);
  }
  for (const status of [undefined, null, '0', 1, 2]) {
    const changed = clone(receipts); changed[0].exit_status = status;
    assert.throws(() => checkReceipts(manifest, changed), /exit_status must be 0/);
  }
  const omitted = clone(receipts); omitted[0].suites.pop(); omitted[0].counts.passed--;
  assert.throws(() => checkReceipts(manifest, omitted), /Missing suites: suite-1\.mjs/);
  for (const sameReceipt of [true, false]) {
    const duplicated = clone(receipts);
    duplicated[sameReceipt ? 0 : 1].suites[1] = clone(duplicated[0].suites[0]);
    assert.throws(() => checkReceipts(manifest, duplicated), /duplicate suite suite-0\.mjs/);
  }
});

test('a receipt cannot quarantine an ordinary failure, timeout, unknown status, or non-suite', () => {
  const { manifest, receipts } = fixture();
  for (const status of ['failed', 'timed_out', 'quarantined-failed', 'quarantined-passed', 'skipped', undefined]) {
    const changed = clone(receipts); changed[0].suites[0].status = status;
    assert.throws(() => checkReceipts(manifest, changed), /suite-0\.mjs must have passed/);
  }
  for (const file of ['unlisted.mjs', 'live.mjs', 'helper.mjs']) {
    const changed = clone(receipts); changed[0].suites[0].file = file;
    assert.throws(() => checkReceipts(manifest, changed), /unexpected suite/);
  }
  for (const status of ['passed', 'failed', 'timed_out']) {
    const changed = clone(receipts); changed[5].suites[1].status = status;
    assert.throws(() => checkReceipts(manifest, changed), /report its quarantine outcome/);
  }
});

test('malformed receipts, mismatched counts and a different source commit cannot pass', () => {
  const { manifest, receipts } = fixture();
  for (const invalid of [null, {}, { exit_status: 0 }, { exit_status: 0, suites: [] }, { exit_status: 0, suites: [null] }]) {
    const changed = clone(receipts); changed[0] = invalid;
    assert.throws(() => checkReceipts(manifest, changed));
  }
  for (const counts of [null, {}, { ...receipts[0].counts, passed: 3 }, { ...receipts[0].counts, failed: 1 }]) {
    const changed = clone(receipts); changed[0].counts = counts;
    assert.throws(() => checkReceipts(manifest, changed), /counts do not match/);
  }
  assert.throws(() => checkReceipts(manifest, receipts, 'f'.repeat(40)), /commit does not match/);
  assert.throws(() => checkReceipts({ version: 1, entries: {} }, receipts), /no suite entries/);
  assert.throws(() => checkReceipts({}, receipts), /Manifest requires/);
});

test('CLI reads the nested download-artifact layout and fails for missing, extra or invalid artifacts', async (t) => {
  const directory = await fs.mkdtemp(path.join(ROOT, '.workspace-receipts-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const { manifest, receipts } = fixture();
  const manifestPath = path.join(directory, 'manifest.json');
  const downloads = path.join(directory, 'downloads');
  await fs.writeFile(manifestPath, JSON.stringify(manifest));
  for (let i = 0; i < 6; i++) {
    const folder = path.join(downloads, `workspace-suites-receipt-${i + 1}`);
    await fs.mkdir(folder, { recursive: true });
    await fs.writeFile(path.join(folder, `shard-${i + 1}.json`), JSON.stringify(receipts[i]));
  }
  const run = (...args) => spawnSync(process.execPath, [SCRIPT, ...args], {
    encoding: 'utf8', env: { ...process.env, GITHUB_SHA: SHA }, timeout: 5000,
  });
  assert.equal(run().status, 2);
  const success = run(downloads, manifestPath);
  assert.equal(success.status, 0, success.stderr);
  assert.match(success.stdout, /all 12 suites exactly once/);
  const shard = path.join(downloads, 'workspace-suites-receipt-1', 'shard-1.json');
  await fs.rm(shard);
  assert.match(run(downloads, manifestPath).stderr, /exactly 6 receipts; found 5/);
  await fs.writeFile(shard, '{');
  assert.equal(run(downloads, manifestPath).status, 1);
  await fs.writeFile(shard, JSON.stringify(receipts[0]));
  await fs.writeFile(path.join(downloads, 'extra.json'), JSON.stringify(receipts[0]));
  assert.match(run(downloads, manifestPath).stderr, /exactly 6 receipts; found 7/);
  await fs.rm(path.join(downloads, 'extra.json'));
  await fs.writeFile(path.join(downloads, 'unexpected.txt'), 'not a receipt');
  assert.match(run(downloads, manifestPath).stderr, /Unexpected receipt artifact entry/);
});
