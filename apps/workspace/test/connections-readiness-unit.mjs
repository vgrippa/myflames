// Pure contracts run in the sandbox. Browser interactions have their own suite.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-readiness-unit-'));
const memory = new Map();
globalThis.localStorage = { getItem: key => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value) };
const STORAGE = 'myflames.connectionReadiness.v1';
const calls = [];
globalThis.__readinessApi = c => new Promise((resolve, reject) => calls.push({ c, resolve, reject }));
const profile = { profile_id: 'saved', host: 'db.example', port: 3306, user: 'reader', ssl_mode: 'PREFERRED' };
const facts = { product: 'MySQL', version: '8.4.3', checked_at: '2026-11-08T12:00:00+00:00',
  read_only: false, super_read_only: false, replica: true, group_member: false, galera_member: false, performance_schema: true };
const report = { ...facts, engine: 'mysql', flavor: 'mysql', notes: [],
  privileges: { PROCESS: false }, actions: { kill: { allowed: false, need: 'CONNECTION_ADMIN or SUPER' } }, password: 'RESPONSE_SECRET' };
let counter = 0;
async function load(entry) {
  const result = await build({ entryPoints: [new URL(`../src/${entry}`, import.meta.url).pathname], bundle: true, write: false,
    format: 'esm', platform: 'node', plugins: [{ name: 'dependencies', setup(b) {
      b.onResolve({ filter: /^react(?:\/jsx-runtime)?$/ }, args => ({ path: args.path, namespace: 'stub' }));
      b.onResolve({ filter: /^\.\/api$/ }, () => ({ path: 'api', namespace: 'stub' }));
      b.onResolve({ filter: /^\.\/kit$/ }, () => ({ path: 'kit', namespace: 'stub' }));
      b.onResolve({ filter: /^\.\/session$/ }, () => ({ path: 'session', namespace: 'stub' }));
      b.onLoad({ filter: /.*/, namespace: 'stub' }, args => ({ loader: 'js', contents: args.path === 'react'
        ? 'export const useSyncExternalStore = (sub, snapshot) => snapshot(); export const useCallback = x => x; export const useEffect = () => {}; export const useRef = x => ({current:x}); export const useState = x => [x,()=>{}];'
        : args.path === 'react/jsx-runtime' ? 'export const jsx = () => null, jsxs = () => null, Fragment = () => null;'
        : args.path === 'api' ? 'export const readReadiness = c => globalThis.__readinessApi(c); export const api = () => {}; export const previewConnections = () => {};'
        : args.path === 'session' ? 'export const relativeTime = () => "2 h ago";'
        : 'export const InfoPopover = () => null, ActionButton = () => null, Checkbox = () => null, ConfirmSheet = () => null, DataTable = () => null, Select = () => null;' }));
      b.onLoad({ filter: /\.css$/ }, () => ({ contents: '', loader: 'text' }));
    } }],
  });
  const file = path.join(temporary, `${++counter}.mjs`);
  await fs.writeFile(file, result.outputFiles[0].text);
  return import(pathToFileURL(file).href);
}
try {
  const store = await load('readiness.tsx');
  assert.equal(calls.length, 0, 'importing the store cannot connect to Production');
  assert.deepEqual(store.useReadiness(profile), {});
  const first = store.checkReadiness({ ...profile, password: 'REQUEST_SECRET' });
  assert.strictEqual(store.checkReadiness({ ...profile, password: 'REQUEST_SECRET' }), first, 'concurrent tests join one read');
  assert.equal(calls.length, 1);
  assert.equal(store.useReadiness(profile).pending, true);
  calls[0].resolve(report);
  await first;
  assert.equal(store.useReadiness(profile).facts.replica, true);
  assert.equal(store.readinessLine(facts), 'MySQL 8.4.3 · replica · Performance Schema on');
  assert.equal(store.readinessHint(report), 'Kill needs CONNECTION_ADMIN or SUPER.');
  assert.equal(store.readinessHint({ ...report, performance_schema: false }), 'Performance Schema is off; performance views are limited. See Server > Instruments.');
  const persisted = memory.get(STORAGE);
  assert.ok(persisted.includes('MySQL'));
  assert.ok(!persisted.includes('SECRET'), 'neither request nor response secrets enter localStorage');
  assert.ok(!persisted.includes('privileges'), 'grant-derived data is not persisted');
  assert.equal(store.useReadiness({ ...profile, host: 'other.example' }).facts, undefined, 'a changed destination never inherits old facts');
  store.copyReadiness(profile, { ...profile, profile_id: 'saved-copy' });
  assert.equal(store.useReadiness({ ...profile, profile_id: 'saved-copy' }).facts.product, 'MySQL');
  const nativeTimeout = globalThis.setTimeout;
  let clearError;
  globalThis.setTimeout = (callback, milliseconds) => { assert.equal(milliseconds, 6000); clearError = callback; return 42; };
  const failure = store.checkReadiness(profile);
  calls[1].reject(Error('Offline'));
  await assert.rejects(failure, /Offline/);
  globalThis.setTimeout = nativeTimeout;
  assert.equal(store.useReadiness(profile).stale, true);
  assert.equal(store.useReadiness(profile).facts.product, 'MySQL', 'a failed check keeps good facts');
  assert.equal(store.useReadiness(profile).error, 'Offline');
  clearError();
  assert.equal(store.useReadiness(profile).error, '', 'only the transient error clears after six seconds');
  assert.equal(store.useReadiness(profile).facts.product, 'MySQL');
  const recovered = store.checkReadiness(profile);
  calls[2].resolve(report); await recovered; // clears the transient error timer
  assert.equal(store.useReadiness(profile).stale, false);
  // Editing credentials while a readiness request is in flight replaces the
  // obsolete request and drops its response without storing any password.
  const wrongAttempt = store.checkReadiness({ ...profile, password: 'WRONG_SECRET' });
  const correctedAttempt = store.checkReadiness({ ...profile, password: 'RIGHT_SECRET' });
  assert.notStrictEqual(correctedAttempt, wrongAttempt, 'changed credentials start a new request generation');
  assert.equal(calls.length, 5);
  calls[3].reject(Error('Access denied for wrong password'));
  await assert.rejects(wrongAttempt, /Access denied/);
  assert.equal(store.useReadiness(profile).pending, true, 'obsolete failure is dropped while the replacement is in flight');
  assert.equal(store.useReadiness(profile).error, '');
  calls[4].resolve({ ...report, version: '8.4.4' });
  await correctedAttempt;
  assert.equal(store.useReadiness(profile).facts.version, '8.4.4');
  assert.equal(store.useReadiness(profile).stale, false);
  assert.equal(store.useReadiness(profile).error, undefined);
  assert.ok(!memory.get(STORAGE).includes('WRONG_SECRET') && !memory.get(STORAGE).includes('RIGHT_SECRET'));
  const reopened = await load('readiness.tsx');
  assert.equal(reopened.useReadiness(profile).facts.product, 'MySQL');
  assert.equal(reopened.useReadiness(profile).stale, true, 'offline facts are labelled stale after reload');
  assert.equal(calls.length, 5, 'restoring cache never sends a readiness request');
  memory.set(STORAGE, 'corrupt');
  const corrupt = await load('readiness.tsx');
  assert.deepEqual(corrupt.useReadiness(profile), {});
  globalThis.localStorage.setItem = () => { throw Error('quota'); };
  const unavailable = corrupt.checkReadiness(profile);
  calls[5].resolve(report); await unavailable;
  assert.equal(corrupt.useReadiness(profile).facts.product, 'MySQL', 'quota cannot break a successful read');
  const importer = await load('ImportConnections.tsx');
  const c = { name: 'Migrated', host: 'DB.example', port: 3306, user: 'reader', database: '' };
  assert.equal(importer.importDuplicate(c, [{ ...c, name: ' MIGRATED ' }]), 'Name already exists');
  assert.equal(importer.importDuplicate(c, [{ ...c, name: 'Other', ssl_mode: 'REQUIRED' }]), 'Endpoint already exists');
  assert.equal(importer.importDuplicate(c, [{ ...c, name: 'Other', ssh_host: 'bastion' }]), '');
  assert.equal(importer.importDuplicate(c, [{ ...c, name: 'Other', port: 3307 }]), '');
  assert.match(importer.importProblem({ ...c, ssh_host: '2001:db8::1', ssh_port: 2223 }), /SSH host/);
  assert.match(importer.importProblem({ ...c, name: 'x'.repeat(201) }), /200 characters/);
  // Mixed duplicate: a row rejected because its name is already taken must not
  // reserve its new endpoint and block a subsequent valid row for that endpoint.
  const existing = [{ name: 'Taken', host: 'old.example', port: 3306, user: 'reader' }];
  const mixed = [
    { name: 'Taken', host: 'new.example', port: 3306, user: 'reader', database: '' },
    { name: 'Available', host: 'new.example', port: 3306, user: 'reader', database: '' },
  ];
  const initialMixed = importer.planImportRows(mixed, existing);
  assert.deepEqual(initialMixed.rows.map(r => r.reason), ['Name already exists', '']);
  assert.deepEqual(initialMixed.selected, ['1']);
  // Rejected unsupported rows also do not reserve endpoints, and eligible file
  // duplicates recompute conflicts when selection changes.
  const withUnsupportedAndDup = [
    { name: 'Bad SSH', host: 'shared.example', port: 3306, user: 'reader', database: '', ssh_host: '2001:db8::1', ssh_port: 2223 },
    { name: 'First valid', host: 'shared.example', port: 3306, user: 'reader', database: '' },
    { name: 'Second valid', host: 'shared.example', port: 3306, user: 'reader', database: '' },
  ];
  const initialDup = importer.planImportRows(withUnsupportedAndDup, existing);
  assert.match(initialDup.rows[0].reason, /SSH host/);
  assert.equal(initialDup.rows[1].reason, '');
  assert.equal(initialDup.rows[2].reason, 'Endpoint already exists');
  assert.deepEqual(initialDup.selected, ['1']);
  const uncheckedDup = importer.planImportRows(withUnsupportedAndDup, existing, []);
  assert.equal(uncheckedDup.rows[1].reason, '');
  assert.equal(uncheckedDup.rows[2].reason, '');
  const switchedDup = importer.planImportRows(withUnsupportedAndDup, existing, ['2']);
  assert.equal(switchedDup.rows[1].reason, 'Endpoint already exists');
  assert.equal(switchedDup.rows[2].reason, '');
  assert.deepEqual(switchedDup.selected, ['2']);
  console.log('Readiness cache, requests, secrets and import duplicate contracts passed.');
} finally {
  await fs.rm(temporary, { recursive: true, force: true });
}
