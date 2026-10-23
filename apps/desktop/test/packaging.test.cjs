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
