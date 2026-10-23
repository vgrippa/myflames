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
  const name = config.dmg.artifactName
    .replace('${productName}', pkg.productName).replace('${version}', pkg.version)
    .replace('${arch}', 'arm64').replace('${ext}', 'dmg').toLowerCase();
  assert.equal(name, 'myflames-' + pkg.version + '-macos-arm64.dmg');
  // Same whole-label rule as app_update.match_asset_for_platform.
  const label = (labels) => new RegExp('(?<![a-z0-9])(?:' + labels.join('|') + ')(?![a-z0-9])');
  assert.match(name, label(['mac', 'macos', 'darwin', 'apple']));
  assert.match(name, label(['arm64', 'aarch64', 'universal']));
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
