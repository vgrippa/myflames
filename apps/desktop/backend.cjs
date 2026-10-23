'use strict';
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const crypto = require('node:crypto');

// Accept only the CLI's exact loopback launch line. URL normalization must not
// turn alternate hosts, credentials or extra parameters into accepted input.
function parseLaunchLine(line) {
  const match = /^myflames UI: (http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})\/#launch=[A-Za-z0-9_-]{32,128})$/.exec(line);
  if (!match || Number(match[2]) > 65535) return null;
  return match[1];
}
function sameOrigin(url, origin) {
  try { const parsed = new URL(url); return parsed.origin === origin && !parsed.username && !parsed.password; }
  catch { return false; }
}
// Official sites the workspace links to. blogs.oracle.com: the MySQL team's
// posts that Home news lists (myflames/news.py keeps only /mysql/ links).
// docs.percona.com: Percona's manuals for the variables Parameters links to
// (myflames/sysvar_docs.py).
const EXTERNAL_HOSTS = new Set(['dev.mysql.com', 'mysql.com', 'www.mysql.com', 'mariadb.org', 'mariadb.com', 'github.com', 'tanelpoder.com',
  'blogs.oracle.com', 'docs.percona.com']);
function externalLink(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' && !parsed.username && !parsed.password &&
      !parsed.port && EXTERNAL_HOSTS.has(parsed.hostname) ? parsed.href : null;
  } catch { return null; }
}
// Resolve existing ancestors as well as the final path, so a symlink cannot
// redirect the isolated desktop store into the command-line workspace.
function canonicalPath(value) {
  const resolved = path.resolve(value);
  try { return fs.realpathSync.native(resolved); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    const parent = path.dirname(resolved);
    if (parent === resolved) throw error;
    return path.join(canonicalPath(parent), path.basename(resolved));
  }
}
function dataPath(value, home, platform = process.platform) {
  const candidate = canonicalPath(value);
  const protectedPath = canonicalPath(path.join(home, '.myflames'));
  const normalized = platform === 'win32' ? candidate.toLowerCase() : candidate;
  const protectedNormalized = platform === 'win32' ? protectedPath.toLowerCase() : protectedPath;
  const relative = path.relative(protectedNormalized, normalized);
  if (!relative || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative))) {
    throw new Error('Desktop data must be separate from the command-line workspace.');
  }
  return candidate;
}
function isolatedBackendHome(directory, home) {
  const actualHome = dataPath(path.join(directory, 'backend-home'), home);
  const actualStore = dataPath(path.join(actualHome, '.myflames'), home);
  for (const [parent, child] of [[directory, actualHome], [actualHome, actualStore]]) {
    const relative = path.relative(parent, child);
    if (!relative || relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) {
      throw new Error('Desktop backend data must remain inside its isolated application directory.');
    }
  }
  return actualHome;
}
// The MySQL and MariaDB installers for Windows put their clients in versioned
// folders under Program Files and do not add them to PATH. Searched in this
// order (MySQL first, as on macOS), newest version first within each.
const WINDOWS_CLIENT_FOLDERS = [
  ['MySQL', /^MySQL Server (\d+(?:\.\d+)*)$/i],
  ['', /^MariaDB (\d+(?:\.\d+)*)$/i],
  ['MySQL', /^MySQL Shell (\d+(?:\.\d+)*)$/i],
];
function newerFirst(a, b) {
  const x = a.split('.').map(Number), y = b.split('.').map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    if ((x[i] || 0) !== (y[i] || 0)) return (y[i] || 0) - (x[i] || 0);
  }
  return 0;
}
// Windows paths compare without case; keep the first spelling of each.
function uniqueWindowsPaths(paths) {
  const seen = new Set();
  return paths.filter(value => {
    const key = value.toLowerCase().replace(/[\\/]+$/, '');
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
function windowsClientDirectories(inherited, fileSystem = fs) {
  const variable = name => {
    const key = Object.keys(inherited).find(candidate => candidate.toLowerCase() === name.toLowerCase());
    return key ? inherited[key] : undefined;
  };
  const roots = uniqueWindowsPaths(['ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'].map(variable).filter(Boolean));
  const found = [];
  for (const [parent, pattern] of WINDOWS_CLIENT_FOLDERS) {
    const group = [];
    for (const root of roots) {
      const base = parent ? path.win32.join(root, parent) : root;
      let names;
      try { names = fileSystem.readdirSync(base); } catch { continue; }
      for (const name of names) {
        const match = pattern.exec(name);
        if (!match) continue;
        const bin = path.win32.join(base, name, 'bin');
        try { if (fileSystem.statSync(bin).isDirectory()) group.push({ bin, version: match[1] }); } catch { /* not installed */ }
      }
    }
    group.sort((a, b) => newerFirst(a.version, b.version));
    found.push(...group.map(item => item.bin));
  }
  return found;
}
function backendEnvironment(dataDirectory, inherited = process.env, platform = process.platform, fileSystem = fs) {
  const env = {};
  // Preserve tool discovery and OS necessities, never inherited MySQL defaults,
  // Python injection paths, profile overrides or vault secrets.
  for (const key of ['PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL']) {
    if (inherited[key] != null) env[key] = inherited[key];
  }
  // Finder does not load shell startup files. Append fixed client installation
  // locations, preserving an explicitly configured PATH's search precedence.
  if (platform === 'darwin') {
    env.PATH = [...new Set((env.PATH || '/usr/bin:/bin:/usr/sbin:/sbin').split(':').filter(Boolean).concat([
      '/opt/homebrew/bin', '/usr/local/bin', '/usr/local/mysql/bin',
      '/opt/homebrew/opt/mysql-client/bin', '/usr/local/opt/mysql-client/bin',
      '/opt/homebrew/opt/mysql-shell/bin', '/usr/local/opt/mysql-shell/bin',
      '/opt/homebrew/opt/mariadb/bin', '/usr/local/opt/mariadb/bin', '/opt/local/bin',
    ]))].join(':');
  } else if (platform === 'win32') {
    env.PATH = uniqueWindowsPaths((env.PATH || '').split(';').filter(Boolean)
      .concat(windowsClientDirectories(inherited, fileSystem))).join(';');
  }
  env.HOME = path.join(dataDirectory, 'backend-home');
  env.USERPROFILE = env.HOME;
  env.MYFLAMES_SECRET_BACKEND = 'vault';
  env.PYTHONUNBUFFERED = '1';
  env.PYTHONNOUSERSITE = '1';
  return env;
}

// Dump, restore and clone tools run in their own process groups, which a
// signal to the backend's group never reaches. The backend lists the groups it
// owns in a fresh file in this private directory (tool-groups-1.0, written by
// myflames.lifecycle.ToolGroups) so stop() can end them if the backend must be
// killed. Files from earlier launches are removed, never trusted.
const TOOL_GROUPS_NAME = /^\.?tool-groups-[A-Za-z0-9_-]*(\.json)?$/;
function toolGroupsFile(dataDirectory, id = crypto.randomUUID()) {
  const directory = path.join(dataDirectory, 'runtime');
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const info = fs.lstatSync(directory);
  if (!info.isDirectory() || (typeof process.getuid === 'function' && info.uid !== process.getuid())) {
    throw new Error('The desktop runtime directory must be a private folder owned by this user.');
  }
  fs.chmodSync(directory, 0o700);
  for (const name of fs.readdirSync(directory)) {
    if (TOOL_GROUPS_NAME.test(name)) fs.rmSync(path.join(directory, name), { force: true });
  }
  return path.join(directory, `tool-groups-${id}.json`);
}
// Read the backend's list. The backend writes it before its launch line and
// keeps it until it exits, removing it only when it could not record a change;
// so once it has launched (*required*) a missing file is unreadable. Anything
// unexpected is unreadable, and shutdown then never claims the tools stopped.
function readToolGroups(file, exclude = [], required = false) {
  const none = { groups: [], readable: true }, unreadable = { groups: [], readable: false };
  if (!file) return none;
  let fd;
  // O_NONBLOCK: a FIFO in place of the file must not block the main process.
  try { fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0)); }
  catch (error) { return error.code === 'ENOENT' && !required ? none : unreadable; }
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.size > 4096 || (typeof process.getuid === 'function' && info.uid !== process.getuid())) return unreadable;
    const buffer = Buffer.alloc(4097), length = fs.readSync(fd, buffer, 0, buffer.length, 0);
    if (length > 4096) return unreadable;
    const value = JSON.parse(buffer.toString('utf8', 0, length));
    if (!value || value.schema_version !== 'tool-groups-1.0' || !Array.isArray(value.groups) || value.groups.length > 16) return unreadable;
    const groups = [];
    for (const id of value.groups) {
      if (!Number.isSafeInteger(id) || id <= 1 || id > 0x7fffffff || exclude.includes(id)) return unreadable;
      if (!groups.includes(id)) groups.push(id);
    }
    return { groups, readable: true };
  } catch { return unreadable; }
  finally { fs.closeSync(fd); }
}

function startBackend({ command, args, cwd, env, timeout = 20000, grace = 5000, killWait = 2000, groupsFile = null }, dependencies = {}) {
  const spawnChild = dependencies.spawn || spawn;
  const platform = dependencies.platform || process.platform;
  const killGroup = dependencies.killGroup || ((pid, signal) => process.kill(-pid, signal));
  const killTree = dependencies.killTree || (pid => new Promise((resolve, reject) => {
    const helper = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    const timer = setTimeout(() => { helper.kill(); reject(new Error('Timed out stopping the owned backend process tree.')); }, grace);
    helper.once('error', () => { clearTimeout(timer); reject(new Error('Could not run taskkill to stop the owned backend.')); });
    helper.once('exit', code => { clearTimeout(timer); if (code === 0) resolve(); else reject(new Error('Could not stop the owned backend process tree.')); });
  }));
  // Process groups are POSIX; on Windows taskkill /T covers the whole tree.
  if (platform === 'win32') groupsFile = null;
  const childEnv = groupsFile ? { ...env, MYFLAMES_TOOL_GROUPS_FILE: groupsFile } : env;
  const child = spawnChild(command, args, { cwd, env: childEnv, windowsHide: true,
    detached: platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] });
  let settled = false, launched = false, closed = false, stopping = null, timer, buffer = '', bytes = 0;
  const exited = new Promise(resolve => child.once('exit', () => { closed = true; resolve(); }));
  const pause = ms => new Promise(resolve => setTimeout(resolve, Math.max(0, ms)));
  function signal(value) {
    if (!child.pid) return;
    try { killGroup(child.pid, value); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
  // EPERM: macOS reports a group holding only an unreaped zombie this way, and
  // a group of another user is not ours. Neither runs our tool any more.
  function groupAlive(id) {
    try { killGroup(id, 0); return true; }
    catch (error) { return !(error.code === 'ESRCH' || error.code === 'EPERM'); }
  }
  // SIGKILL each recorded tool group and wait, until the deadline, for all of
  // them to be gone. Returns the groups still running. An id seen gone during
  // this stop is never signalled again: a stale list could name it after the
  // system reused it for another program's group.
  const gone = new Set();
  async function stopGroups(ids, deadline) {
    const check = id => { if (!gone.has(id) && groupAlive(id)) return true; gone.add(id); return false; };
    let alive = ids.filter(check);
    for (const id of alive) {
      try { killGroup(id, 'SIGKILL'); }
      catch (error) { if (error.code === 'ESRCH' || error.code === 'EPERM') gone.add(id); }
    }
    alive = alive.filter(id => !gone.has(id));
    while (alive.length && Date.now() < deadline) {
      await pause(Math.min(25, deadline - Date.now()));
      alive = alive.filter(check);
    }
    return alive;
  }
  async function stop() {
    if (stopping) return stopping;
    stopping = (async () => {
      clearTimeout(timer);
      if (!child.pid) return;
      if (platform === 'win32') {
        if (closed) return; // Do not target a PID that could have been reused.
        let failure;
        try { await killTree(child.pid); } catch (error) { failure = error; }
        if (!closed) child.kill(); // Bounded fallback for the owned direct child.
        let waitTimer;
        await Promise.race([exited, new Promise(resolve => { waitTimer = setTimeout(resolve, grace); })]);
        clearTimeout(waitTimer);
        if (!closed) throw new Error('The owned backend did not stop.');
        if (failure) throw failure; // Tree cleanup may be incomplete, even if parent stopped.
        return;
      }
      // The child owns a fresh process group. This never signals an existing UI
      // or unrelated database process. SIGINT lets Python close its workspace:
      // it stops its dump, restore and clone tools first, then the rest.
      signal('SIGINT');
      let graceTimer;
      await Promise.race([exited, new Promise(resolve => { graceTimer = setTimeout(resolve, grace); })]);
      clearTimeout(graceTimer);
      const deadline = Date.now() + killWait, exclude = [process.pid, child.pid];
      let remaining = [], readable = true;
      if (!closed) {
        // The backend did not finish in time. While it runs, its list names
        // exactly the tool groups that exist: stop them before killing it.
        const record = readToolGroups(groupsFile, exclude, launched);
        readable = record.readable;
        remaining = await stopGroups(record.groups, deadline);
      }
      // Also remove descendants that survived their parent's normal exit.
      signal('SIGKILL');
      let killTimer;
      await Promise.race([exited, new Promise(resolve => { killTimer = setTimeout(resolve, Math.max(0, deadline - Date.now())); })]);
      clearTimeout(killTimer);
      // The backend's last list also covers a tool it started just before it
      // was killed, or one its own cleanup could not stop.
      const last = readToolGroups(groupsFile, exclude, launched);
      readable = readable && last.readable;
      remaining = await stopGroups([...new Set([...remaining, ...last.groups])], deadline);
      if (!closed) throw new Error('The owned backend did not stop.');
      if (remaining.length || !readable) throw new Error('Could not confirm that every owned dump, restore or clone tool stopped.');
      if (groupsFile) { try { fs.rmSync(groupsFile, { force: true }); } catch { /* the next launch removes it */ } }
    })();
    return stopping;
  }
  const ready = new Promise((resolve, reject) => {
    function fail(message) {
      if (settled) return;
      settled = true; clearTimeout(timer);
      void stop().then(() => reject(new Error(message)), () => reject(new Error(message)));
    }
    child.once('error', () => fail('Could not start the Python backend. Check the configured Python executable.'));
    child.once('exit', code => fail(`The Python backend stopped before startup (exit ${code ?? 'signal'}). Build the workspace assets and check the Python installation.`));
    child.stdout.on('data', () => {}); // Drain, but never mirror private output.
    child.stderr.on('data', chunk => {
      if (settled) return;
      bytes += chunk.length;
      if (bytes > 65536) return fail('The Python backend produced too much startup output.');
      buffer += chunk.toString('utf8');
      if (buffer.length > 8192 && !buffer.includes('\n')) return fail('Invalid backend startup response.');
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, end).replace(/\r$/, ''); buffer = buffer.slice(end + 1);
        const url = parseLaunchLine(line);
        if (url) { settled = launched = true; clearTimeout(timer); resolve({ child, url, stop }); return; }
      }
    });
    timer = setTimeout(() => fail('The Python backend did not become ready in time.'), timeout);
  });
  ready.stop = stop;
  return ready;
}
module.exports = { parseLaunchLine, sameOrigin, externalLink, backendEnvironment, dataPath, isolatedBackendHome,
  toolGroupsFile, readToolGroups, startBackend };
