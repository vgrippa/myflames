// Static and unit checks for the editor/data-editing fixes (no browser).
import assert from 'node:assert/strict';
import fs from 'node:fs';
const src = f => fs.readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8');

// QueryDigest exports execution statistics, not extrema/percentiles of digest
// averages or per-digest row totals. Execute the actual text generator with
// formatters stubbed to expose its numeric inputs (no browser needed).
const digest = src('QueryDigest.tsx');
const generator = /const generatePtReportText = \(\) => \{([\s\S]*?)\n  \};/.exec(digest);
assert.ok(generator, 'digest text generator exists');
const body = generator[1]
  .replace('const lines: string[]', 'const lines')
  .replace('(str: string, len: number)', '(str, len)')
  .replace('(value: number | null)', '(value)')
  .replace('(total: number)', '(total)');
const generate = new Function('statements', 'aggregates', 'serverMeta', 'bound', 'filter',
  'time', 'number', 'shortDigest', 'formatRatio', 'digestQuery', body);
function exported(rows) {
  const sum = key => rows.reduce((n, row) => n + (row[key] || 0), 0);
  return generate(rows, { totalWorkloadMs: sum('total_ms'), totalCalls: sum('count'),
    totalExamined: sum('rows_examined'), totalSent: sum('rows_sent'), totalLockMs: sum('lock_time_ms') },
    { engine: 'mysql', version: '8.4', filter: 'time' }, { label: 'Alice@db:3306' }, 'time',
    x => `${x}ms`, String, String, () => ({ text: '1:1' }), row => ({ text: row.digest_text }));
}
const row = { digest_text: 'SELECT 1', count: 100, total_ms: 100, avg_ms: 1, max_ms: 1,
  rows_sent: 100, rows_examined: 200, lock_time_ms: 50, tmp_disk_tables: 0 };
const text = exported([row, { ...row, count: 1, avg_ms: 100, max_ms: 100 }]);
const attributes = label => text.split('\n').find(line => line.startsWith(`# ${label}`)).trim().split(/\s+/).slice(3);
assert.deepEqual(attributes('Exec time').slice(0, 3), ['200ms', 'unknown', '100ms']);
assert.equal(attributes('Exec time')[4], 'unknown', 'execution p95 cannot be recovered from averages');
assert.deepEqual(attributes('Rows sent').slice(0, 3), ['200', 'unknown', 'unknown']);
assert.deepEqual(attributes('Rows examine').slice(0, 3), ['400', 'unknown', 'unknown']);
assert.deepEqual(attributes('Lock time').slice(0, 3), ['100ms', 'unknown', 'unknown']);
assert.match(exported([{ ...row, max_ms: 0 }]), /# Exec time\s+100ms\s+unknown\s+0ms/);
assert.match(exported([{ ...row, max_ms: undefined, lock_time_ms: undefined }]), /# Exec time\s+100ms\s+unknown\s+unknown/);
assert.match(exported([{ ...row, lock_time_ms: undefined }]), /# Lock time\s+unknown/);
assert.match(exported([]), /# Exec time\s+0ms\s+unknown\s+unknown\s+unknown\s+unknown/);
assert.ok(/const escape = csvCell/.test(digest) && digest.includes("from \"./reportTableExport\""), 'digest CSV uses the formula guard');

// TableData: CSV guard, JSON null, NULL is empty in TSV, Set to Default only where real.
const td = src('TableData.tsx');
assert.ok(td.includes('csvCell(v)'), 'row CSV uses csvCell');
assert.ok(td.includes('Record<string, string | null>') && td.includes('?? null'), 'row JSON emits null for NULL');
assert.ok(/v\?\.kind === 'null' \? null/.test(td) && !/\? 'NULL' :/.test(td), 'NULL is not copied as the text NULL');
assert.ok(td.includes('"Revert change"') && td.includes('contextMenu.inserted ? "Set to Default"'), 'existing rows say Revert change');

// ResultsPane: Copy value is the raw value.
const rp = src('ResultsPane.tsx');
assert.ok(rp.includes('copy(result.rows[menu.row][menu.col] ?? ""'), 'Copy value copies the raw value');

// TableAlterDialog: identifiers are backtick-escaped.
const alter = src('TableAlterDialog.tsx');
assert.ok(/bt\(schema\)\}\.\$\{bt\(table\)/.test(alter) && !alter.includes('`${schema}`'), 'ALTER TABLE quotes identifiers');

// MassKillDialog (Kill sessions…): statements are reviewed before anything is sent; cancel is recorded.
const mk = src('MassKillDialog.tsx');
const handle = mk.slice(mk.indexOf('async function handleConfirm'), mk.indexOf('function declineBulk'));
assert.ok(handle.includes('prepareBulkKill') && !handle.includes('killBulkSessions'), 'prepare does not send');
const send = mk.slice(mk.indexOf('async function sendBulk'), mk.indexOf('return (\n    <dialog'));
assert.ok(send.includes('killBulkSessions(') && send.includes('policy.check()'), 'send re-checks policy then kills');
assert.ok(mk.includes('<ConfirmSheet') && mk.includes('pending.review.statements') && mk.includes('onCancel={declineBulk}'), 'confirmation sheet lists the statements');
assert.ok(/recordDeclined\(/.test(mk), 'cancel is recorded as declined');
console.log('Editor fixes: digest execution statistics and CSV guard, table copy, Copy value, ALTER quoting and Kill sessions review passed.');
