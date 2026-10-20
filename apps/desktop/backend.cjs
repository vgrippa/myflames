'use strict';
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

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
const EXTERNAL_HOSTS = new Set(['dev.mysql.com', 'mysql.com', 'www.mysql.com', 'mariadb.org', 'mariadb.com', 'github.com', 'tanelpoder.com']);
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
function backendEnvironment(dataDirectory, inherited = process.env, platform = process.platform) {
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
  }
  env.HOME = path.join(dataDirectory, 'backend-home');
  env.USERPROFILE = env.HOME;
  env.MYFLAMES_SECRET_BACKEND = 'vault';
  env.PYTHONUNBUFFERED = '1';
  env.PYTHONNOUSERSITE = '1';
  return env;
}

function startBackend({ command, args, cwd, env, timeout = 20000, grace = 2000 }, dependencies = {}) {
  const spawnChild = dependencies.spawn || spawn;
  const platform = dependencies.platform || process.platform;
  const killGroup = dependencies.killGroup || ((pid, signal) => process.kill(-pid, signal));
  const killTree = dependencies.killTree || (pid => new Promise((resolve, reject) => {
    const helper = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    const timer = setTimeout(() => { helper.kill(); reject(new Error('Timed out stopping the owned backend process tree.')); }, grace);
    helper.once('error', () => { clearTimeout(timer); reject(new Error('Could not run taskkill to stop the owned backend.')); });
    helper.once('exit', code => { clearTimeout(timer); if (code === 0) resolve(); else reject(new Error('Could not stop the owned backend process tree.')); });
  }));
  const child = spawnChild(command, args, { cwd, env, windowsHide: true,
    detached: platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] });
  let settled = false, closed = false, stopping = null, timer, buffer = '', bytes = 0;
  const exited = new Promise(resolve => child.once('exit', () => { closed = true; resolve(); }));
  function signal(value) {
    if (!child.pid) return;
    try { killGroup(child.pid, value); } catch (error) { if (error.code !== 'ESRCH') throw error; }
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
      // or unrelated database process. SIGINT lets Python close its workspace.
      signal('SIGINT');
      let graceTimer;
      await Promise.race([exited, new Promise(resolve => { graceTimer = setTimeout(resolve, grace); })]);
      clearTimeout(graceTimer);
      // Also remove descendants that survived their parent's normal exit.
      signal('SIGKILL');
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
        if (url) { settled = true; clearTimeout(timer); resolve({ child, url, stop }); return; }
      }
    });
    timer = setTimeout(() => fail('The Python backend did not become ready in time.'), timeout);
  });
  ready.stop = stop;
  return ready;
}
module.exports = { parseLaunchLine, sameOrigin, externalLink, backendEnvironment, dataPath, isolatedBackendHome, startBackend };
