'use strict';
// Packaging contract: local builds stay unsigned, release builds sign with the
// hardened runtime and only the JIT entitlement, and the release DMG keeps a
// name the in-app update check (myflames/app_update.py) matches.
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const desktop = path.resolve(__dirname, '..');
const config = JSON.parse(fs.readFileSync(path.join(desktop, 'electron-builder.json'), 'utf8'));
const pkg = JSON.parse(fs.readFileSync(path.join(desktop, 'package.json'), 'utf8'));
// Same whole-label rule as app_update.match_asset_for_platform.
const label = (labels) => new RegExp('(?<![a-z0-9])(?:' + labels.join('|') + ')(?![a-z0-9])');
const artifact = (template, arch, ext) => template
  .replace('${productName}', pkg.productName).replace('${version}', pkg.version)
  .replace('${arch}', arch).replace('${ext}', ext).toLowerCase();

test('local builds explicitly disable Developer ID signing and notarization', () => {
  assert.equal(config.mac.identity, null);
  assert.equal(config.mac.notarize, false);
  assert.match(pkg.scripts['package:mac'], /--mac dir --arm64/);
});

test('release signing uses the hardened runtime and grants only JIT', () => {
  assert.equal(config.mac.hardenedRuntime, true);
  assert.equal(config.mac.entitlements, 'entitlements.mac.plist');
  assert.equal(config.mac.entitlementsInherit, 'entitlements.mac.plist');
  const plist = fs.readFileSync(path.join(desktop, config.mac.entitlements), 'utf8');
  assert.deepEqual(plist.match(/<key>[^<]+<\/key>/g), ['<key>com.apple.security.cs.allow-jit</key>']);
});

test('the release DMG name is one the update check matches for Apple Silicon', () => {
  const name = artifact(config.dmg.artifactName, 'arm64', 'dmg');
  assert.equal(name, 'myflames-' + pkg.version + '-macos-arm64.dmg');
  assert.match(name, label(['mac', 'macos', 'darwin', 'apple']));
  assert.match(name, label(['arm64', 'aarch64', 'universal']));
});

test('the app names its publisher as NOTICE does, not Electron\'s default', () => {
  // electron-builder takes the Windows CompanyName, the uninstall entry's
  // publisher and the copyright line from author; without it the exe says GitHub, Inc.
  // A Windows checkout may have CRLF line endings.
  const notice = fs.readFileSync(path.join(desktop, '..', '..', 'NOTICE'), 'utf8').replace(/\r\n/g, '\n');
  assert.ok(notice.includes('Copyright 2026 ' + pkg.author + '\n'), 'author must match the NOTICE copyright holder');
});

test('Windows builds an unsigned x64 NSIS installer the update check matches', () => {
  assert.deepEqual(config.win.target, [{ target: 'nsis', arch: ['x64'] }]);
  assert.equal(config.win.signtoolOptions, undefined);
  assert.equal(config.win.azureSignOptions, undefined);
  assert.match(pkg.scripts['package:win'], /--win --x64 --publish never/);
  const name = artifact(config.nsis.artifactName, 'x64', 'exe');
  assert.equal(name, 'myflames-' + pkg.version + '-windows-x64-setup.exe');
  assert.match(name, label(['win', 'windows']));
  assert.match(name, label(['x86_64', 'amd64', 'x64']));
  assert.doesNotMatch(name, label(['mac', 'macos', 'darwin', 'apple', 'linux']));
});

test('the release script refuses to start without an identity and notary credentials', { skip: process.platform === 'win32' }, () => {
  assert.equal(pkg.scripts['package:mac:release'], 'sh scripts/package-mac-release.sh');
  const script = path.join(desktop, 'scripts', 'package-mac-release.sh');
  const env = { PATH: process.env.PATH, HOME: process.env.HOME };
  const identity = { MYFLAMES_MAC_IDENTITY: 'Example (TEAMID)' };
  const missingKey = path.join(desktop, 'test', 'no-such-AuthKey.p8');
  const onMac = process.platform === 'darwin' && process.arch === 'arm64';
  for (const [extra, reason] of [
    [{}, /set MYFLAMES_MAC_IDENTITY/],
    [{ APPLE_KEYCHAIN_PROFILE: 'profile' }, /set MYFLAMES_MAC_IDENTITY/],
    [{ MYFLAMES_MAC_IDENTITY: 'Developer ID Application: Example (TEAMID)', APPLE_KEYCHAIN_PROFILE: 'profile' }, /leave the "Developer ID Application: " prefix out/],
    [identity, /set APPLE_KEYCHAIN_PROFILE, or APPLE_API_KEY/],
    [{ ...identity, APPLE_API_KEY: missingKey }, /set APPLE_API_KEY_ID and APPLE_API_ISSUER/],
    [{ ...identity, APPLE_API_KEY: missingKey, APPLE_API_KEY_ID: 'KEYID' }, /set APPLE_API_KEY_ID and APPLE_API_ISSUER/],
    [{ ...identity, APPLE_API_KEY: missingKey, APPLE_API_KEY_ID: 'KEYID', APPLE_API_ISSUER: 'issuer' }, /cannot read the API key file/],
  ]) {
    const run = spawnSync('sh', [script], { env: { ...env, ...extra }, encoding: 'utf8' });
    assert.equal(run.status, 2, run.stderr);
    assert.match(run.stderr, onMac ? reason : /build on an Apple Silicon Mac/);
    assert.equal(run.stdout, '');
  }
});

test('the app bundles the MySQL client the lock pins, with its license', () => {
  const resources = config.extraResources.map(({ from, to }) => from + ' -> ' + to);
  assert.ok(resources.includes('build/mysql-client -> mysql-client'));
  assert.ok(resources.includes('build/mysql-client/LICENSE -> licenses/MySQL-Client-LICENSE'));
  const lock = JSON.parse(fs.readFileSync(path.join(desktop, 'mysql-client.lock.json'), 'utf8'));
  assert.equal(lock.schema_version, 'mysql-client-lock-1.0');
  assert.match(lock.version, /^8\.4\.\d+$/, 'an 8.4 LTS release');
  assert.deepEqual(lock.programs, ['mysql', 'mysqldump']);
  assert.equal(lock.license, 'GPL-2.0-only WITH Universal-FOSS-exception-1.0');
  assert.equal(lock.source.tag, 'https://github.com/mysql/mysql-server/tree/mysql-' + lock.version);
  assert.match(lock.comment.join(' '), /gpg --verify/);
  const names = { 'darwin-arm64': `mysql-${lock.version}-macos15-arm64.tar.gz`, 'win32-x64': `mysql-${lock.version}-winx64.zip` };
  assert.deepEqual(Object.keys(lock.archives).sort(), Object.keys(names));
  for (const [platform, entry] of Object.entries(lock.archives)) {
    assert.equal(entry.url, 'https://cdn.mysql.com/Downloads/MySQL-8.4/' + names[platform]);
    assert.equal(entry.root + (platform === 'win32-x64' ? '.zip' : '.tar.gz'), names[platform]);
    assert.ok(Number.isSafeInteger(entry.size) && entry.size > 0);
    assert.match(entry.sha256, /^[0-9a-f]{64}$/);
    assert.match(entry.md5, /^[0-9a-f]{32}$/);
  }
  // NOTICE names the same release and where its source is.
  const notice = fs.readFileSync(path.join(desktop, '..', '..', 'NOTICE'), 'utf8');
  assert.ok(notice.includes('MySQL ' + lock.version), 'NOTICE names the bundled MySQL release');
  assert.ok(notice.includes(lock.source.tag), 'NOTICE links the source tag');
});

test('every packaging path fetches the bundled client before electron-builder runs', () => {
  assert.match(pkg.scripts['package:mac'], /^python3 scripts\/fetch-mysql-client\.py --platform darwin-arm64 && electron-builder /);
  const release = fs.readFileSync(path.join(desktop, 'scripts', 'package-mac-release.sh'), 'utf8');
  const fetchAt = release.indexOf('scripts/fetch-mysql-client.py --platform darwin-arm64');
  assert.ok(fetchAt > 0 && fetchAt < release.indexOf('electron-builder --mac dmg'));
  assert.match(release, /"\$client\/bin\/mysql" --version/);
  const workflow = fs.readFileSync(path.join(desktop, '..', '..', '.github', 'workflows', 'windows-desktop.yml'), 'utf8');
  const step = workflow.indexOf('apps/desktop/scripts/fetch-mysql-client.py --platform win32-x64');
  assert.ok(step > 0 && step < workflow.indexOf('npm run package:win'));
  assert.match(workflow, /key: mysql-client-win32-x64-\$\{\{ steps\.mysql-client\.outputs\.sha256 \}\}/);
  assert.match(workflow, /path: apps\/desktop\/build\/mysql-client-cache/);
});
