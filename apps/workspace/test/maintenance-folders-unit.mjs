// Exercise the exported folder helpers with the real path functions and an
// in-memory API transport. No browser, filesystem listing or server writes.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const compile = async name => ts.transpileModule(await fs.readFile(new URL(`../src/${name}`, import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const requests = [];
let listing;
globalThis.__maintenanceFolderApi = (route, body, signal) => {
  requests.push({ route, body, signal });
  return Promise.resolve(listing);
};
const apiUrl = moduleUrl('export const api = (...args) => globalThis.__maintenanceFolderApi(...args); export class ApiError extends Error {}');
const filesUrl = moduleUrl((await compile('filesApi.ts')).replace('"./api"', JSON.stringify(apiUrl)));
const kitUrl = moduleUrl('export const ActionButton = null, ActionStatus = null, Fields = null, useAction = null;');
const perfUrl = moduleUrl('export const Duration = null;');
const opsUrl = moduleUrl('export const chooseBackupDirectory = null, factText = null;');
const source = (await compile('OpsShared.tsx'))
  .replace('"react/jsx-runtime"', JSON.stringify(pathToFileURL(require.resolve('react/jsx-runtime')).href))
  .replace('"react"', JSON.stringify(pathToFileURL(require.resolve('react')).href))
  .replace('"./kit"', JSON.stringify(kitUrl)).replace('"./PerfShared"', JSON.stringify(perfUrl))
  .replace('"./opsApi"', JSON.stringify(opsUrl)).replace('"./filesApi"', JSON.stringify(filesUrl));
const { dumpFolderStem, suggestDumpFolder, dumpOutputName } = await import(moduleUrl(source));
try {
  const date = new Date(2026, 0, 2, 0, 5);
  assert.equal(dumpFolderStem('sales', date), 'sales-2026-01-02', 'date uses local calendar fields');
  for (const name of ['../../schema', '\\..\\bad:/name?*<>|"', '.hidden', '--database', '\0\n\r', 'a'.repeat(200), '🔥'.repeat(80)]) {
    const stem = dumpFolderStem(name, date);
    assert.ok(!/[/\\<>:"|?*\u0000-\u001f]/.test(stem), stem);
    assert.ok(!stem.includes('..') && !/^[.\s-]/.test(stem), stem);
    assert.ok(Array.from(stem).length <= 91, stem);
    assert.ok(new TextEncoder().encode(stem).length <= 191, 'fits a portable filename byte limit');
    assert.match(stem, /-2026-01-02$/);
  }
  assert.equal(dumpFolderStem('', date), 'dump-2026-01-02');
  for (const value of ['', '/', '\\', '.', '..', '/backups/..', 'C:\\', '/backups/CON', '/backups/invalid?'])
    assert.equal(dumpOutputName(value, 'generated'), 'generated', 'Browse cannot escape its chosen parent: '+value);
  assert.equal(dumpOutputName('/backup/café', 'generated'), 'café');
  listing = { path: '/Users/dba', home: '/Users/dba', transfers: false, entries: [
    { name: 'sales-2026-01-02' }, { name: 'SALES-2026-01-02-2' },
  ] };
  const signal = new AbortController().signal;
  assert.equal(await suggestDumpFolder('', 'sales-2026-01-02', signal), '/Users/dba/sales-2026-01-02-3');
  assert.deepEqual(requests.at(-1), { route: 'files/local/list', body: { path: '' }, signal });
  assert.equal(await suggestDumpFolder('', 'sales-2026-01-02', signal,
    ['/Users/dba/sales-2026-01-02-3', '/other/sales-2026-01-02-4']), '/Users/dba/sales-2026-01-02-4');
  listing = { path: 'C:\\Users\\dba', home: 'C:\\Users\\dba', entries: [] };
  assert.equal(await suggestDumpFolder('', 'sales-2026-01-02'), 'C:\\Users\\dba\\sales-2026-01-02');
  listing = { path: '/', home: '/', entries: [] };
  assert.equal(await suggestDumpFolder('/', 'sales-2026-01-02'), '/sales-2026-01-02');
  assert.ok(requests.every(r => r.route === 'files/local/list'), 'suggestions never create a directory or start a job');
  const bindingUrl = moduleUrl('export const connectionKey = null;');
  const opsSource = (await compile('opsApi.ts'))
    .replace('"react"', JSON.stringify(pathToFileURL(require.resolve('react')).href))
    .replace('"./api"', JSON.stringify(apiUrl)).replace('"./binding"', JSON.stringify(bindingUrl));
  const { startLifecycle } = await import(moduleUrl(opsSource));
  const plan = Object.freeze({ operation: 'restore', review_token: 'opaque-token', commands: ['exact command'], binding: 'opaque-binding',
    required_acknowledgement: 'REPLACE mysql SYSTEM TABLES ON target' });
  const connection = { host: 'test.invalid' };
  await startLifecycle(connection, plan, 'target');
  assert.equal('typed_acknowledgement' in requests.at(-1).body, false, 'unchecked sends no acknowledgement field');
  await startLifecycle(connection, plan, 'target', undefined, plan.required_acknowledgement);
  const sent = requests.at(-1);
  assert.equal(sent.route, 'lifecycle/start');
  assert.equal(sent.body.typed_acknowledgement, plan.required_acknowledgement);
  assert.equal(sent.body.plan, plan, 'reviewed plan is forwarded without alteration');
  assert.equal(sent.body.typed_target, 'target');
  assert.equal(sent.body.confirmed, true);
  // Compile only the exported pure helpers and the real byte formatter. This
  // avoids mounting the view or replacing the calculations with test doubles.
  const declarations = async (name, names) => {
    const text = await fs.readFile(new URL(`../src/${name}`, import.meta.url), 'utf8');
    const ast = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const found = ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
    assert.equal(found.length, names.length);
    return found.map(node => node.getText(ast)).join('\n');
  };
  const helpers = await declarations('Maintenance.tsx', ['lifecycleProgress', 'lifecycleRate', 'lifecycleElapsed', 'autoRevealDump']);
  const formatter = await declarations('PerfShared.tsx', ['bytes']);
  const { lifecycleProgress, lifecycleRate, lifecycleElapsed, autoRevealDump } = await import(moduleUrl(ts.transpileModule(
    formatter + '\n' + helpers, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 } }).outputText));
  const { joinPath } = await import(filesUrl);
  assert.equal(joinPath('/backup/', 'myflames-manifest.json'), '/backup/myflames-manifest.json');
  assert.equal(joinPath('C:\\backup', 'myflames-manifest.json'), 'C:\\backup\\myflames-manifest.json');
  const mib = 2 ** 20;
  const sql = { progress: null, output: { written_bytes: 120 * mib, estimated_bytes: 262 * mib } };
  assert.equal(lifecycleProgress(sql).percent, '≈46%');
  assert.equal(lifecycleProgress(sql).output, '120.0 MiB written of about 262.0 MiB');
  assert.equal(lifecycleProgress({ ...sql, progress: 45 }).percent, '45%', 'tool percent wins over table-size estimate');
  assert.equal(lifecycleProgress({ ...sql, progress: 0 }).value, 0, 'reported zero stays determinate');
  assert.equal(lifecycleProgress({ ...sql, progress: 100 }).percent, '100%');
  assert.equal(lifecycleProgress({ progress: null }).value, null, 'older snapshots are indeterminate');
  assert.equal(lifecycleProgress({ progress: null }).output, '');
  for (const estimated_bytes of [null, 0, -1, NaN, Infinity]) {
    const result = lifecycleProgress({ progress: null, output: { written_bytes: mib, estimated_bytes } });
    assert.equal(result.value, null);
    assert.equal(result.output, '1.0 MiB written');
  }
  const over = lifecycleProgress({ progress: null, output: { written_bytes: 1.2 * 2 ** 30, estimated_bytes: 2 ** 30 } });
  assert.equal(over.value, null, 'exceeding an estimate never implies completion');
  assert.equal(over.percent, '');
  assert.equal(over.output, '1.2 GiB written, more than the 1.0 GiB estimate');
  assert.equal(lifecycleProgress({ progress: null, output: { written_bytes: mib, estimated_bytes: mib } }).value, null, 'exact estimate is also indeterminate');
  assert.equal(lifecycleProgress({ progress: null, output: { written_bytes: 9999, estimated_bytes: 10000 } }).percent, '≈99%', 'rounding cannot claim estimated completion');
  assert.equal(lifecycleProgress({ progress: NaN }).value, null);
  assert.equal(lifecycleRate(null, { written: mib, at: 1000 }), null, 'no rate from a single snapshot');
  assert.equal(lifecycleRate({ written: mib, at: 1000 }, { written: 25 * mib, at: 3000 }), 12 * mib);
  assert.equal(lifecycleRate({ written: mib, at: 1000 }, { written: mib, at: 2000 }), 0, 'stalled output shows zero rate');
  assert.equal(lifecycleRate({ written: mib, at: 1000 }, { written: 0, at: 2000 }), null, 'counter reset discards a rate');
  assert.equal(lifecycleRate({ written: 0, at: 1000 }, { written: mib, at: 1000 }), null, 'same-time samples cannot divide by zero');
  assert.equal(lifecycleElapsed(83.9), '1:23');
  assert.equal(lifecycleElapsed(3600), '1:00:00');
  assert.equal(lifecycleElapsed(0), '0:00');
  assert.equal(lifecycleElapsed(-1), '0:00');
  for (const unknown of [null, undefined, NaN, Infinity]) assert.equal(lifecycleElapsed(unknown), '');
  // Abuse case (open lead 17): a schema named "a,calc.exe,b" gives a dump
  // folder with commas, which File Explorer's /select, splits. Windows never
  // opens a folder without a click; macOS opens one this view started, once.
  const comma = { id: 'j1', directory: 'C:\\Users\\John Smith\\a,calc.exe,b-2026-11-06' };
  assert.equal(autoRevealDump(comma, true, new Set(), true), false, 'Windows never auto-reveals');
  assert.equal(autoRevealDump({ id: 'j1', directory: '/Users/dba/app-2026-11-06' }, true, new Set(), false), true);
  assert.equal(autoRevealDump({ id: 'j1', directory: '/Users/dba/app-2026-11-06' }, false, new Set(), false), false, 'adopted jobs never auto-reveal');
  assert.equal(autoRevealDump({ id: 'j1', directory: '/Users/dba/app-2026-11-06' }, true, new Set(['j1']), false), false, 'once per job');
  assert.equal(autoRevealDump({ id: null, directory: '/Users/dba/x' }, true, new Set(), false), false);
  assert.equal(autoRevealDump({ id: 'j2' }, true, new Set(), false), false, 'no folder for failed dumps, restore or clone');
  console.log('PASS maintenance reported and estimated progress, over-estimate and unknown output, snapshot rates and elapsed clocks');
  console.log('PASS maintenance folder names, local date, collisions, reserved paths, Windows roots and read-only suggestions');
  console.log('PASS maintenance acknowledgement request omission and exact reviewed-plan round trip');
} finally {
  delete globalThis.__maintenanceFolderApi;
}
