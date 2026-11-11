'use strict';
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
(async () => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-desktop-smoke-'));
  // A packaged app is a macOS .app bundle or the Windows win-unpacked folder.
  const appPath = process.argv[2] ? path.resolve(process.argv[2]) : null;
  const windows = process.platform === 'win32';
  const executable = appPath ? path.join(appPath, windows ? 'myflames.exe' : 'Contents/MacOS/myflames') : require('electron');
  const bundledBackend = appPath && (windows ? path.join(appPath, 'resources', 'backend', 'myflames-backend.exe')
    : path.join(appPath, 'Contents/Resources/backend/myflames-backend'));
  const env = { ...process.env, MYFLAMES_DESKTOP_DATA: home };
  if (appPath) {
    // Prove that the delivered bundle does not depend on terminal PATH/Python.
    // Windows spells it Path; drop every spelling so only this one is passed.
    for (const key of Object.keys(env)) if (key.toUpperCase() === 'PATH') delete env[key];
    const systemRoot = process.env.SystemRoot || 'C:\\Windows';
    env.PATH = windows ? [path.join(systemRoot, 'System32'), systemRoot].join(path.delimiter) : '/usr/bin:/bin:/usr/sbin:/sbin';
    env.MYFLAMES_DESKTOP_PYTHON = '/missing/python-must-not-be-used';
    env.MYFLAMES_DESKTOP_SMOKE = '1';
  }
  delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(executable, [...(appPath ? [] : [path.resolve(__dirname, '..')]), '--smoke'], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '', diagnostics = '';
  child.stdout.on('data', chunk => { output += chunk; });
  // Chromium diagnostics can be noisy. Keep them private unless startup fails.
  child.stderr.on('data', chunk => { diagnostics = (diagnostics + chunk.toString()).slice(-4000); });
  const timeout = setTimeout(() => child.kill('SIGTERM'), 45000);
  try {
    const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', resolve); });
    if (code !== 0) throw new Error('Desktop smoke did not complete successfully. ' + diagnostics.replace(/http:\/\/\S+/g, '[private URL]'));
    const report = JSON.parse(await fs.readFile(path.join(home, 'smoke.json'), 'utf8'));
    if (!report.ready || !report.shellNavigation || !report.completion || !report.undoRedo || !report.authenticated || !report.reloadAuthenticated || !report.preload || !report.sandbox || report.nodeIntegration) throw new Error('Desktop isolation checks failed.');
    const samePath = (a, b) => windows ? String(a).toLowerCase() === b.toLowerCase() : a === b;
    if (appPath && (!report.packaged || !samePath(report.backendExecutable, bundledBackend)))
      throw new Error('The packaged app did not use its bundled backend.');
    let clientLine = '';
    if (appPath) {
      // The bundled MySQL client starts with only the system folders on PATH,
      // and the backend found a usable mysql (the bundled one unless one is installed).
      const bin = windows ? path.join(appPath, 'resources', 'mysql-client', 'bin') : path.join(appPath, 'Contents/Resources/mysql-client/bin');
      const version = require('../mysql-client.lock.json').version;
      const run = spawnSync(path.join(bin, windows ? 'mysql.exe' : 'mysql'), ['--version'], { env: { PATH: env.PATH, ...(windows ? { SystemRoot: process.env.SystemRoot || 'C:\\Windows' } : {}) }, encoding: 'utf8', timeout: 20000 });
      if (run.status !== 0 || !run.stdout.includes('Ver ' + version + ' '))
        throw new Error('The bundled MySQL client did not start: ' + (run.error ? run.error.message : (run.stdout + run.stderr).trim()));
      const client = report.mysqlClient || {};
      if (!client.available) throw new Error('The packaged backend found no usable mysql client.');
      const inside = String(client.path || '').toLowerCase().startsWith((bin + path.sep).toLowerCase());
      clientLine = `Backend mysql client: ${client.path} (${inside ? 'bundled' : 'installed'}). Bundled: ${run.stdout.trim()}\n`;
    }
    try { process.kill(report.backendPid, 0); throw new Error('Owned backend remained alive after desktop exit.'); }
    catch (error) { if (error.code !== 'ESRCH') throw error; }
    const outputDirectory = path.resolve(__dirname, '../smoke-results');
    await fs.mkdir(outputDirectory, { recursive: true });
    await fs.copyFile(path.join(home, 'smoke.png'), path.join(outputDirectory, 'desktop.png'));
    process.stdout.write(output + clientLine + 'Owned backend stopped. Screenshot: apps/desktop/smoke-results/desktop.png\n');
  } finally { clearTimeout(timeout); await fs.rm(home, { recursive: true, force: true }); }
})().catch(error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
