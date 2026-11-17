#!/usr/bin/env node
/** Certify the aggregate workspace gate from receipts, not matrix job state. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SHARDS = 6;
const COUNT_KEYS = { passed: 'passed', flaky: 'flaky', failed: 'failed', timed_out: 'timed_out',
  'quarantined-failed': 'quarantined_failed', 'quarantined-passed': 'quarantined_passed' };
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

export function checkReceipts(manifest, receipts, commit) {
  if (!object(manifest) || manifest.version !== 1 || !object(manifest.entries)) {
    throw new Error('Manifest requires version 1 and an entries object');
  }
  if (!Array.isArray(receipts) || receipts.length !== SHARDS) {
    throw new Error(`Expected exactly ${SHARDS} receipts; found ${receipts?.length ?? 0}`);
  }
  const expected = new Map(Object.entries(manifest.entries).filter(([, entry]) => entry.kind === 'suite'));
  if (!expected.size) throw new Error('Manifest contains no suite entries');
  const seen = new Set();
  for (const [index, receipt] of receipts.entries()) {
    const label = `Receipt ${index + 1}`;
    if (!object(receipt) || receipt.exit_status !== 0) throw new Error(`${label}: exit_status must be 0`);
    if (commit && receipt.commit !== commit) throw new Error(`${label}: commit does not match ${commit}`);
    if (!Array.isArray(receipt.suites) || !receipt.suites.length) throw new Error(`${label}: suites must be nonempty`);
    const counts = Object.fromEntries(Object.values(COUNT_KEYS).map((key) => [key, 0]));
    for (const suite of receipt.suites) {
      if (!object(suite) || !expected.has(suite.file)) throw new Error(`${label}: unexpected suite ${suite?.file}`);
      if (seen.has(suite.file)) throw new Error(`${label}: duplicate suite ${suite.file}`);
      seen.add(suite.file);
      const entry = expected.get(suite.file);
      // The manifest is authoritative even if a receipt omits its safety flag.
      // A receipt that declares safety must also never soften a failure.
      const safety = Boolean(entry.safety) || suite.safety === true;
      const allowed = safety ? ['passed']
        : entry.quarantine ? ['quarantined-failed', 'quarantined-passed'] : ['passed', 'flaky'];
      if (!allowed.includes(suite.status)) {
        const requirement = safety ? 'have passed without retries (safety suite)'
          : entry.quarantine ? 'report its quarantine outcome' : 'have passed or be flaky';
        throw new Error(`${label}: ${suite.file} must ${requirement}; got ${suite.status}`);
      }
      counts[COUNT_KEYS[suite.status]] += 1;
    }
    // Statuses are authoritative. Counts must agree, so a superficially green
    // summary cannot conceal a failed, missing, or duplicated suite.
    if (!object(receipt.counts) || Object.entries(counts).some(([key, count]) => receipt.counts[key] !== count)) {
      throw new Error(`${label}: counts do not match its suite results`);
    }
  }
  const missing = [...expected.keys()].filter((file) => !seen.has(file));
  if (missing.length) throw new Error(`Missing suites: ${missing.sort().join(', ')}`);
  return seen.size;
}

async function receiptFiles(directory) {
  const files = [];
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await receiptFiles(file));
    else if (entry.isFile() && entry.name.endsWith('.json')) files.push(file);
    else throw new Error(`Unexpected receipt artifact entry: ${file}`);
  }
  return files.sort();
}

async function reportFlaky(files, receipts) {
  const flaky = receipts.flatMap((receipt, index) => (Array.isArray(receipt?.suites) ? receipt.suites : [])
    .filter((suite) => suite?.status === 'flaky')
    .map((suite) => ({ file: suite.file,
      shard: /^shard-(\d+)\.json$/.exec(path.basename(files[index]))?.[1] || String(index + 1) })));
  console.log('\nFlaky suites:');
  for (const suite of flaky) console.log(`  ${suite.file} (shard ${suite.shard}/${SHARDS})`);
  if (!flaky.length) console.log('  (none)');
  if (process.env.GITHUB_STEP_SUMMARY) {
    // Escape manifest names before placing them into the Markdown table.
    const cell = (value) => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/\|/g, '&#124;').replace(/[\r\n]/g, ' ');
    const lines = ['### Flaky workspace suites', ''];
    if (flaky.length) {
      lines.push('| Suite | Shard |', '| --- | --- |',
        ...flaky.map((suite) => `| ${cell(suite.file)} | ${suite.shard}/${SHARDS} |`));
    } else lines.push('No flaky suites.');
    await fs.appendFile(process.env.GITHUB_STEP_SUMMARY, `${lines.join('\n')}\n`);
  }
}

async function main() {
  const [directory, manifestPath = path.join(ROOT, 'scripts', 'workspace-suites.json'), ...extra] = process.argv.slice(2);
  if (!directory || extra.length) {
    console.error('Usage: node scripts/check_workspace_receipts.mjs RECEIPT_DIR [MANIFEST]');
    return 2;
  }
  try {
    const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
    const files = await receiptFiles(directory);
    const receipts = await Promise.all(files.map(async (file) => {
      try { return JSON.parse(await fs.readFile(file, 'utf8')); }
      catch (error) { throw new Error(`${file}: ${error.message}`); }
    }));
    try {
      const count = checkReceipts(manifest, receipts, process.env.GITHUB_SHA);
      console.log(`Workspace receipts: ${SHARDS} successful shards cover all ${count} suites exactly once.`);
    } finally {
      // Preserve the flaky evidence even when another shard or suite failed.
      await reportFlaky(files, receipts);
    }
    return 0;
  } catch (error) {
    console.error(`Workspace receipt check failed: ${error.message}`);
    return 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) process.exitCode = await main();
