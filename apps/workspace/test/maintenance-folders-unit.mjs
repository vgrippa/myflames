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
  console.log('PASS maintenance folder names, local date, collisions, reserved paths, Windows roots and read-only suggestions');
  console.log('PASS maintenance acknowledgement request omission and exact reviewed-plan round trip');
} finally {
  delete globalThis.__maintenanceFolderApi;
}
