#!/usr/bin/env node
/** Run the tracked workspace suites without npm rebuilding between workers. */
import { spawn, spawnSync } from 'node:child_process';
import { createReadStream, readlinkSync } from 'node:fs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { performance } from 'node:perf_hooks';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const DEFAULT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const KINDS = new Set(['suite', 'live', 'helper', 'generator', 'fixture']);
const DEFAULT_WEIGHT = 60;
const STATUS_INTERVAL = 30_000;
const SUITE_ENV = { 'action-feedback.mjs': ['ACTION_FEEDBACK_ONLY', 'ACTION_FEEDBACK_WORKERS', 'ACTION_FEEDBACK_SHOTS'] };
const PRIVATE_ENV = new Set(['HOME', 'USERPROFILE', 'TMPDIR', 'TMP', 'TEMP', 'PATH', 'MYFLAMES_SECRET_BACKEND',
  'MYFLAMES_VAULT_PASSPHRASE', 'MYSQL_PWD', 'MYSQL_HOME', 'MYSQL_TEST_LOGIN_FILE']);

class UsageError extends Error {}

const help = `Usage: node scripts/run_workspace_suites.mjs [options]
  --jobs N              Parallel suites (default: half the CPUs, capped at 4)
  --timeout SECONDS     Override every suite timeout (also bounds the build)
  --durations PATH      Recorded seconds (default: scripts/workspace_suite_durations.json)
  --durations-out PATH  Write measured seconds; never written implicitly
  --no-build            Use the existing workspace build
  --log-dir PATH        Parent for a fresh run directory (default: temporary directory)
  --only a.mjs,b.mjs    Select suite entries by file name
  --kind suite|live     Run this kind (default: suite; live requires explicit selection)
  --shard I/N           Run one duration-balanced shard, numbered from 1
  --list                Print selected files without building or running them
  --receipt PATH        Write source provenance, timings, counts and exit status
  --root PATH           Repository root (default: this script's parent)
  --help                Show this help
`;

function positive(value, flag, integer = false) {
  const number = Number(value);
  if (!value?.trim() || !Number.isFinite(number) || number <= 0
      || (integer && !Number.isSafeInteger(number))) {
    throw new UsageError(`${flag} must be a positive ${integer ? 'integer' : 'finite number'}`);
  }
  return number;
}

function parseOptions(argv) {
  const options = { root: DEFAULT_ROOT, kind: 'suite', jobs: Math.max(1, Math.min(4, Math.floor(os.cpus().length / 2))) };
  const values = new Set(['--jobs', '--timeout', '--durations', '--durations-out', '--log-dir',
    '--only', '--kind', '--shard', '--receipt', '--root']);
  const seen = new Set();
  for (let index = 0; index < argv.length; index += 1) {
    const [flag, ...suffix] = argv[index].split('=');
    if (seen.has(flag)) throw new UsageError(`${flag} was supplied more than once`);
    seen.add(flag);
    if (['--help', '--no-build', '--list'].includes(flag) && suffix.length === 0) {
      options[flag.slice(2)] = true;
      continue;
    }
    if (!values.has(flag)) throw new UsageError(`Unknown option: ${argv[index]}`);
    const value = suffix.length ? suffix.join('=') : argv[++index];
    if (!value || value.startsWith('--')) throw new UsageError(`${flag} requires a value`);
    options[flag.slice(2)] = value;
  }
  if (typeof options.jobs === 'string') options.jobs = positive(options.jobs, '--jobs', true);
  if (options.timeout !== undefined) options.timeout = positive(options.timeout, '--timeout');
  if (!['suite', 'live'].includes(options.kind)) throw new UsageError('--kind must be suite or live');
  if (options.only !== undefined) {
    options.only = options.only.split(',').map((name) => name.trim());
    if (options.only.some((name) => !name) || new Set(options.only).size !== options.only.length) {
      throw new UsageError('--only needs distinct, nonempty file names');
    }
    if (options.kind !== 'suite') throw new UsageError('--only selects suite entries; it cannot be combined with --kind live');
  }
  if (options.shard !== undefined) {
    const match = /^(\d+)\/(\d+)$/.exec(options.shard);
    if (!match) throw new UsageError('--shard must be I/N, numbered from 1');
    options.shard = { index: positive(match[1], '--shard', true), count: positive(match[2], '--shard', true) };
    if (options.shard.index > options.shard.count) throw new UsageError('--shard requires 1 <= I <= N');
  }
  options.root = path.resolve(options.root);
  options.workspace = path.join(options.root, 'apps', 'workspace');
  options.durations = path.resolve(options.durations || path.join(options.root, 'scripts', 'workspace_suite_durations.json'));
  for (const flag of ['durations-out', 'log-dir', 'receipt']) {
    if (options[flag]) options[flag] = path.resolve(options[flag]);
  }
  return options;
}

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

async function readJson(file) {
  try {
    return JSON.parse(await fs.readFile(file, 'utf8'));
  } catch (error) {
    throw new UsageError(`Cannot read ${file}: ${error.message}`);
  }
}

function gitIgnored(directory, names) {
  if (!names.length) return new Set();
  const result = spawnSync('git', ['check-ignore', '--stdin'], { cwd: directory, input: names.join('\n') + '\n',
    encoding: 'utf8' });
  // Status 0: some names are ignored; 1: none; anything else (no git, not a repository): none.
  if (result.status !== 0) return new Set();
  return new Set(result.stdout.split('\n').filter(Boolean).map((name) => name.replace(/\/$/, '')));
}

async function inventory(options) {
  const manifest = await readJson(path.join(options.root, 'scripts', 'workspace-suites.json'));
  if (!object(manifest) || manifest.version !== 1 || !object(manifest.entries)) {
    throw new UsageError('workspace-suites.json requires version 1 and an entries object');
  }
  let entries;
  const testDirectory = path.join(options.workspace, 'test');
  try {
    // Dot entries are scratch folders some suites create beside themselves
    // (report.mjs, perf-budgets-regressions.mjs), never suites.
    entries = (await fs.readdir(testDirectory, { withFileTypes: true }))
      .filter((entry) => !entry.name.startsWith('.'));
  } catch (error) {
    throw new UsageError(`Cannot inventory workspace/test: ${error.message}`);
  }
  // Output git ignores (the Files suites' shots-* screenshot folders) is not
  // part of the inventory. Without git the check covers every entry.
  const ignored = gitIgnored(testDirectory, entries.map((entry) => entry.name + (entry.isDirectory() ? '/' : '')));
  entries = entries.filter((entry) => !ignored.has(entry.name));
  const names = new Set(entries.map((entry) => entry.name));
  const unlisted = [...names].filter((name) => !Object.hasOwn(manifest.entries, name)).sort();
  const missing = Object.keys(manifest.entries).filter((name) => !names.has(name)).sort();
  if (unlisted.length || missing.length) {
    throw new UsageError(`INVENTORY MISMATCH\nUnlisted test entries:\n  ${unlisted.join('\n  ') || '(none)'}\n`
      + `Missing test entries:\n  ${missing.join('\n  ') || '(none)'}`);
  }
  const files = new Map(entries.map((entry) => [entry.name, entry]));
  for (const [file, entry] of Object.entries(manifest.entries)) {
    if (!file || file === '.' || file === '..' || /[\\/\x00-\x1f\x7f]/.test(file)
        || !object(entry) || !KINDS.has(entry.kind)
        || !(entry.script === null || (typeof entry.script === 'string' && entry.script.trim()))
        || typeof entry.reason !== 'string' || !entry.reason.trim() || /[\r\n]/.test(entry.reason)) {
      throw new UsageError(`Invalid manifest entry: ${file}`);
    }
    if (entry.kind === 'suite' || entry.kind === 'live') {
      if (!file.endsWith('.mjs') || !files.get(file).isFile()
          || typeof entry.timeout !== 'number' || !Number.isFinite(entry.timeout) || entry.timeout <= 0) {
        throw new UsageError(`${file}: suite/live entries require a regular .mjs file and a positive timeout`);
      }
    } else if (Object.hasOwn(entry, 'timeout')) {
      throw new UsageError(`${file}: only suite/live entries have a timeout`);
    }
    if (Object.hasOwn(entry, 'quarantine')) {
      if (entry.kind !== 'suite') throw new UsageError(`${file}: only suite entries may have quarantine`);
      if (typeof entry.quarantine !== 'string' || !entry.quarantine.trim()
          || /[\r\n\u0085\u2028\u2029]/.test(entry.quarantine)) {
        throw new UsageError(`${file}: quarantine must be a nonempty, one-line string`);
      }
    }
    for (const key of ['needs', 'env']) {
      if ((key === 'needs' && entry.kind === 'live') || Object.hasOwn(entry, key)) {
        if (entry.kind !== 'live' || !Array.isArray(entry[key])
            || new Set(entry[key]).size !== entry[key].length
            || entry[key].some((name) => typeof name !== 'string' || !/^[A-Z_][A-Z0-9_]*$/.test(name)
              || PRIVATE_ENV.has(name) || /^(NODE_|PYTHON|PUPPETEER_)/.test(name))) {
          throw new UsageError(`${file}: ${key} must be a live-only list of distinct environment names, without runtime or private overrides`);
        }
      }
    }
    if (entry.needs?.some((name) => entry.env?.includes(name))) {
      throw new UsageError(`${file}: needs and env must not overlap`);
    }
  }
  return manifest.entries;
}

async function selectSuites(options, entries) {
  let durations = {};
  try {
    durations = JSON.parse(await fs.readFile(options.durations, 'utf8'));
  } catch (error) {
    if (error.code !== 'ENOENT') throw new UsageError(`Cannot read durations: ${error.message}`);
  }
  if (!object(durations) || Object.values(durations).some((seconds) =>
    typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0)) {
    throw new UsageError('Durations must be an object mapping file names to finite, nonnegative seconds');
  }
  for (const file of options.only || []) {
    if (!Object.hasOwn(entries, file) || entries[file].kind !== 'suite') {
      throw new UsageError(`--only requires suite entries: ${file}`);
    }
  }
  const suites = Object.entries(entries)
    .filter(([file, entry]) => entry.kind === options.kind && (!options.only || options.only.includes(file)))
    .map(([file, entry]) => ({ file, timeout: options.timeout ?? entry.timeout, quarantine: entry.quarantine,
      needs: entry.needs || [], env: entry.env || [],
      weight: Object.hasOwn(durations, file) ? durations[file] : DEFAULT_WEIGHT }))
    .sort((a, b) => b.weight - a.weight || (a.file < b.file ? -1 : a.file > b.file ? 1 : 0));
  if (!options.shard) return suites;
  // Greedy longest-first assignment is deterministic, including equal/zero weights.
  // Allocate only as many bins as can be occupied, even for an enormous N.
  const bins = Array.from({ length: Math.min(options.shard.count, suites.length) }, () => ({ weight: 0, suites: [] }));
  for (const suite of suites) {
    let lightest = bins[0];
    for (const bin of bins) {
      if (bin.weight < lightest.weight
          || (bin.weight === lightest.weight && bin.suites.length < lightest.suites.length)) lightest = bin;
    }
    lightest.suites.push(suite);
    lightest.weight += suite.weight;
  }
  return bins[options.shard.index - 1]?.suites || [];
}

function isolatedEnvironment(home, temporary, forwarded = []) {
  // Do not inherit database credentials, client options, NODE_OPTIONS or a real HOME.
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    const name = process.platform === 'win32' ? key.toUpperCase() : key;
    // PYTHON picks the interpreter suites start the backend and seed scripts with.
    if (['PATH', 'CI', 'PYTHON', 'MYFLAMES_NO_SANDBOX', 'MYFLAMES_BROWSER_ARGS'].includes(name)
        || name.startsWith('PUPPETEER_')
        || (process.platform === 'win32' && ['SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT'].includes(name))) {
      env[key] = value;
    }
  }
  // Each live suite receives only its own declared prerequisites/companions.
  // The build and ordinary suites never inherit another suite's lab settings.
  for (const name of forwarded) if (process.env[name] !== undefined) env[name] = process.env[name];
  // Puppeteer finds its browser under the home folder by default; keep pointing
  // at the real download, since each suite gets a fresh HOME.
  const browsers = process.env.PUPPETEER_CACHE_DIR || path.join(os.homedir(), '.cache', 'puppeteer');
  return { ...env, PUPPETEER_CACHE_DIR: browsers, HOME: home, USERPROFILE: home, TMPDIR: temporary,
    TMP: temporary, TEMP: temporary, MYFLAMES_SECRET_BACKEND: 'vault' };
}

function stopWorker(child) {
  if (!child.pid) return;
  if (process.platform === 'win32') {
    // Windows retains taskkill's existing tree behavior. The private-path ps
    // sweep below is POSIX-only and cannot discover reparented Windows children.
    const result = spawnSync('taskkill', ['/F', '/T', '/PID', String(child.pid)], { stdio: 'ignore', timeout: 5000 });
    if ((result.error || result.status !== 0) && child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
    return;
  }
  // The ChildProcess handle still owns this live worker. Never signal its old
  // pid/group merely because it used to lead a suite that has already exited.
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
}

export function referencesPrivatePath(text, roots) {
  return roots.some((root) => {
    let offset = text.indexOf(root);
    while (offset !== -1) {
      const next = text[offset + root.length];
      if (next === undefined || /[\\/\s"'\x00]/.test(next)) return true;
      offset = text.indexOf(root, offset + root.length);
    }
    return false;
  });
}

function processTable(pid) {
  // -ww prevents long Chrome/Python command lines being cut before the path.
  const result = spawnSync('ps', [...(pid === undefined ? ['-A'] : ['-p', String(pid)]),
    '-ww', '-o', 'pid=,pgid=,stat=,command='], { encoding: 'utf8', timeout: 2000, maxBuffer: 16 << 20 });
  if (!result.error && result.status === 1 && !result.stdout.trim() && !result.stderr.trim()) return [];
  if (result.error || result.status !== 0) throw new Error('Cannot inspect suite processes with ps');
  return result.stdout.split('\n').flatMap((line) => {
    const match = /^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$/.exec(line);
    return match && !match[3].startsWith('Z') && Number(match[1]) > 1 && Number(match[1]) !== process.pid
      ? [{ pid: Number(match[1]), group: Number(match[2]), command: match[4] }] : [];
  });
}

function workingDirectories(processes) {
  const directories = new Map();
  if (!processes.length) return directories;
  if (process.platform === 'linux') {
    for (const { pid } of processes) {
      try { directories.set(pid, readlinkSync(`/proc/${pid}/cwd`)); }
      catch (error) { if (!['ENOENT', 'ESRCH', 'EACCES', 'EPERM'].includes(error.code)) throw error; }
    }
  } else if (process.platform === 'darwin') {
    // Darwin ps exposes arguments but not cwd. Its system lsof supplies that
    // field; Linux supplies it through procfs. No environment/secrets are read.
    const result = spawnSync('lsof', ['-nP', '-a', '-p', processes.map(({ pid }) => pid).join(','),
      '-d', 'cwd', '-Fpn'], { encoding: 'utf8', timeout: 2000, maxBuffer: 16 << 20 });
    if (result.error || ![0, 1].includes(result.status)) throw new Error('Cannot inspect suite working folders with lsof');
    let pid;
    for (const line of result.stdout.split('\n')) {
      if (/^p\d+$/.test(line)) pid = Number(line.slice(1));
      else if (pid && line.startsWith('n')) directories.set(pid, line.slice(1));
    }
  }
  return directories;
}

function ownedProcesses(processes, roots) {
  const directories = workingDirectories(processes.filter(({ command }) => !referencesPrivatePath(command, roots)));
  return processes.filter(({ pid, command }) => referencesPrivatePath(command, roots)
    || referencesPrivatePath(directories.get(pid) || '', roots));
}

function signalProcess(pid) {
  // macOS answers EPERM for a group that holds only an unreaped zombie; like
  // ESRCH it means there is nothing left to stop (run_tests_parallel.py does the same).
  try { process.kill(pid, 'SIGKILL'); } catch (error) { if (!['ESRCH', 'EPERM'].includes(error.code)) throw error; }
}

async function cleanupProcesses(child, home, name) {
  if (process.platform === 'win32') return; // stopWorker already ran taskkill.
  const roots = [...new Set([home, await fs.realpath(home)].flatMap((root) => {
    // Darwin's /tmp and /var aliases may be canonicalized by a child, lsof,
    // or the suite itself. Both spellings denote this same private directory.
    const alias = process.platform === 'darwin' ? root.replace(/^\/private(?=\/(?:tmp|var)\/)/, '') : root;
    return [root, alias, path.join(root, 'tmp'), path.join(alias, 'tmp')];
  }))];
  let warned = false;
  for (let attempt = 0; attempt < 10; attempt += 1) {
    const table = processTable();
    const owned = ownedProcesses(table, roots);
    if (!owned.length) return;
    if (!warned) {
      console.error(`Warning: ${name} left ${owned.length} running process(es) behind; cleaning up (suite bug).`);
      warned = true;
    }
    const members = table.filter(({ group }) => group === child.pid);
    // A reused group leader must not turn cleanup into a kill of another job.
    // Keep group cleanup only when *every* current member still proves ownership.
    if (members.length && members.every(({ pid }) => owned.some((entry) => entry.pid === pid))) {
      const current = processTable().filter(({ group }) => group === child.pid);
      if (current.length && current.every(({ pid }) => {
        const fresh = processTable(pid).filter(({ group }) => group === child.pid);
        return fresh.length === 1 && ownedProcesses(fresh, roots).length === 1;
      })) signalProcess(-child.pid);
    }
    for (const { pid } of owned) {
      // Re-read the full command immediately before each signal. A pid from
      // the inventory may have exited and been reused by an unrelated process.
      if (ownedProcesses(processTable(pid), roots).length) signalProcess(pid);
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`${name}: private-path processes remain after cleanup`);
}

async function runProcess(command, args, name, timeout, options, state, forwarded = []) {
  const started = performance.now();
  let home;
  let log;
  let child;
  let timer;
  let failure;
  let timedOut = false;
  let code = null;
  let signal = null;
  const logPath = path.join(state.logDir, `${name}.log`);
  try {
    home = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-workspace-home-'));
    const temporary = path.join(home, 'tmp');
    await fs.mkdir(temporary);
    log = await fs.open(logPath, 'wx', 0o600);
    child = spawn(command, args, { cwd: options.workspace, detached: true,
      env: isolatedEnvironment(home, temporary, forwarded), stdio: ['ignore', log.fd, log.fd] });
    state.active.set(child, name);
    const ended = new Promise((resolve) => {
      child.once('error', (error) => { failure = error; resolve(); });
      child.once('exit', (exitCode, exitSignal) => { code = exitCode; signal = exitSignal; resolve(); });
    });
    // Polling avoids Node's maximum setTimeout delay for large timeout values.
    timer = setInterval(() => {
      if (performance.now() - started < timeout * 1000 || timedOut) return;
      timedOut = true;
      try { stopWorker(child); } catch (error) { failure = error; }
    }, 50);
    if (state.interrupted) stopWorker(child);
    await ended;
  } catch (error) {
    failure = error;
  } finally {
    clearInterval(timer);
    if (child) {
      try { stopWorker(child); await cleanupProcesses(child, home, name); } catch (error) { failure ||= error; }
      state.active.delete(child);
    }
    if (log) {
      if (failure) await log.write(`\nRunner error: ${failure.message}\n`).catch(() => {});
      try { await log.close(); } catch (error) { failure ||= error; }
    }
    if (home) {
      try { await fs.rm(home, { recursive: true, force: true, maxRetries: 3 }); }
      catch (error) { failure ||= error; }
    }
  }
  return { file: name, seconds: Number(((performance.now() - started) / 1000).toFixed(6)),
    status: timedOut ? 'timed_out' : code === 0 && !failure && !state.interrupted ? 'passed' : 'failed',
    code, signal, error: failure?.message, logPath };
}

async function printFailure(result) {
  const outcome = result.outcome ?? result.status;
  console.error(`\n=== ${result.file} (${outcome === 'timed_out' ? 'TIMED OUT' : 'FAILED'}; exit ${result.code ?? result.signal ?? 'unknown'}) ===`);
  if (result.quarantine) console.error(`Quarantine: ${result.quarantine}`);
  // Keep memory bounded even when a suite produces a very large log.
  const lines = [];
  try {
    const input = createReadStream(result.logPath, { encoding: 'utf8' });
    for await (const line of createInterface({ input, crlfDelay: Infinity })) {
      lines.push(line);
      if (lines.length > 60) lines.shift();
    }
    console.error(lines.join('\n'));
  } catch (error) {
    console.error(`Cannot read log: ${error.message}`);
  }
  if (result.error) console.error(`Runner error: ${result.error}`);
}

function sourceProvenance(root) {
  const git = (...args) => {
    const result = spawnSync('git', ['--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', root, ...args],
      { encoding: 'utf8', timeout: 10_000, env: isolatedEnvironment(os.tmpdir(), os.tmpdir()) });
    if (result.error || result.status !== 0) throw new Error('Git provenance unavailable');
    return result.stdout.trim();
  };
  try {
    // A fake workspace nested in another checkout must not borrow its SHA.
    if (path.resolve(git('rev-parse', '--show-toplevel')) !== root) throw new Error('Root is not a Git checkout');
    const commit = git('rev-parse', '--verify', 'HEAD');
    if (!/^[a-f0-9]{40}$/.test(commit)) throw new Error('Full Git SHA unavailable');
    return { commit, clean_tree: git('status', '--porcelain=v1', '--untracked-files=all') === '' };
  } catch {
    return { commit: null, clean_tree: false };
  }
}

async function writeJson(destination, value) {
  await fs.mkdir(path.dirname(destination), { recursive: true });
  const temporary = await fs.mkdtemp(path.join(path.dirname(destination), '.workspace-json-'));
  try {
    const file = path.join(temporary, 'result.json');
    await fs.writeFile(file, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
    await fs.rename(file, destination);
  } finally {
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

function counts(results) {
  return { passed: results.filter((result) => result.status === 'passed').length,
    failed: results.filter((result) => result.status === 'failed').length,
    timed_out: results.filter((result) => result.status === 'timed_out').length,
    quarantined_failed: results.filter((result) => result.status === 'quarantined-failed').length,
    quarantined_passed: results.filter((result) => result.status === 'quarantined-passed').length };
}

async function execute(options, state) {
  const entries = await inventory(options);
  const suites = await selectSuites(options, entries);
  if (options.list) {
    for (const suite of suites) console.log(suite.file);
    return 0;
  }
  if (!suites.length) throw new UsageError('No suites selected; refusing an empty run');
  const missing = suites.flatMap((suite) => {
    const names = suite.needs.filter((name) => !process.env[name]?.trim());
    return names.length ? [`${suite.file}: ${names.join(', ')}`] : [];
  });
  if (missing.length) throw new UsageError(`Missing live suite environment variables:\n  ${missing.join('\n  ')}`);
  // Capture source state before this run creates untracked logs in the checkout.
  if (options.receipt) state.provenance = sourceProvenance(options.root);
  if (options['log-dir']) await fs.mkdir(options['log-dir'], { recursive: true });
  state.logDir = await fs.mkdtemp(path.join(options['log-dir'] || os.tmpdir(), 'myflames-workspace-suites-'));
  console.log(`Workspace suite logs: ${state.logDir}`);
  const interrupt = (signal) => {
    state.interrupted = signal;
    for (const child of state.active.keys()) {
      try { stopWorker(child); } catch (error) { console.error(`Cleanup error: ${error.message}`); }
    }
  };
  const onInt = () => interrupt('SIGINT');
  const onTerm = () => interrupt('SIGTERM');
  process.on('SIGINT', onInt);
  process.on('SIGTERM', onTerm);
  const status = setInterval(() => {
    console.error(`[${state.results.length}/${suites.length} done; ${((performance.now() - state.clock) / 1000).toFixed(1)}s elapsed] running: ${[...state.active.values()].join(', ') || 'none'}`);
  }, STATUS_INTERVAL);
  try {
    if (!options['no-build']) {
      const command = process.platform === 'win32' ? process.env.ComSpec || 'cmd.exe' : 'npm';
      const args = process.platform === 'win32' ? ['/d', '/s', '/c', 'npm run build'] : ['run', 'build'];
      const build = await runProcess(command, args, 'build', options.timeout ?? 900, options, state);
      if (build.status !== 'passed') { await printFailure(build); return 1; }
    }
    let index = 0;
    const worker = async () => {
      while (index < suites.length && !state.interrupted) {
        const suite = suites[index++];
        const result = await runProcess(process.execPath, [`test/${suite.file}`], suite.file, suite.timeout, options, state,
          [...suite.needs, ...suite.env, ...(SUITE_ENV[suite.file] || [])]);
        if (suite.quarantine && !result.error) {
          // Retain the process outcome so quarantined timeouts keep their diagnostic.
          result.outcome = result.status;
          result.quarantine = suite.quarantine;
          result.status = result.status === 'passed' ? 'quarantined-passed' : 'quarantined-failed';
        }
        state.results.push(result);
        console.log(`${result.status.toUpperCase()} ${suite.file} (${result.seconds.toFixed(3)}s)`);
      }
    };
    await Promise.all(Array.from({ length: Math.min(options.jobs, suites.length) }, worker));
    for (const result of state.results) {
      if (result.status === 'failed' || result.status === 'timed_out') await printFailure(result);
    }
    const quarantinedFailures = state.results.filter((result) => result.status === 'quarantined-failed');
    if (quarantinedFailures.length) {
      console.error('\nQuarantined failures:');
      for (const result of quarantinedFailures) await printFailure(result);
    }
    const quarantinedPasses = state.results.filter((result) => result.status === 'quarantined-passed');
    if (quarantinedPasses.length) {
      console.log('\nPassed while quarantined:');
      for (const result of quarantinedPasses) {
        console.log(`  ${result.file}: passed while quarantined; remove the quarantine (${result.quarantine})`);
      }
    }
    const totals = counts(state.results);
    console.log(`\nSuites: ${totals.passed} passed, ${totals.failed} failed, ${totals.timed_out} timed out, `
      + `${totals.quarantined_failed} quarantined failed, ${totals.quarantined_passed} quarantined passed; ${state.results.length}/${suites.length} ran`);
    if (options['durations-out']) {
      await writeJson(options['durations-out'], Object.fromEntries(state.results.slice()
        .sort((a, b) => a.file < b.file ? -1 : 1).map((result) => [result.file, result.seconds])));
    }
    return !state.interrupted && state.results.length > 0 && state.results.length === suites.length
      && totals.failed === 0 && totals.timed_out === 0 ? 0 : 1;
  } finally {
    clearInterval(status);
    process.off('SIGINT', onInt);
    process.off('SIGTERM', onTerm);
  }
}

async function main() {
  let options;
  try {
    options = parseOptions(process.argv.slice(2));
    if (options.help) { console.log(help); return 0; }
  } catch (error) {
    console.error(`Workspace runner error: ${error.message}`);
    return 2;
  }
  const state = { active: new Map(), results: [], clock: performance.now(), started: new Date().toISOString(),
    provenance: { commit: null, clean_tree: false } };
  let exitStatus;
  try {
    exitStatus = await execute(options, state);
  } catch (error) {
    console.error(`Workspace runner error: ${error.message}`);
    exitStatus = error instanceof UsageError ? 2 : 1;
  }
  if (options.receipt && !options.list) {
    if (!state.provenance.commit) {
      console.error('Receipt has no verifiable Git SHA; commit is null and clean_tree is false.');
      if (exitStatus === 0) exitStatus = 1;
    }
    try {
      await writeJson(options.receipt, { ...state.provenance,
        command: process.argv.map((arg) => JSON.stringify(arg)).join(' '),
        started: state.started, finished: new Date().toISOString(), node: process.version,
        counts: counts(state.results), suites: state.results.map(({ file, seconds, status }) => ({ file, seconds, status })),
        exit_status: exitStatus });
    } catch (error) {
      console.error(`Cannot write receipt: ${error.message}`);
      exitStatus = 1;
    }
  }
  return exitStatus;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) process.exitCode = await main();
