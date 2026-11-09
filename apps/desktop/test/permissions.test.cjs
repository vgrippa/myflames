'use strict';
// Permission contract: the desktop session refuses every permission request
// except the Fullscreen API for the workspace's own origin (Learn's Full
// screen). The rule is read from main.cjs and run as written, so a change to
// it fails here.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { sameOrigin } = require('../backend.cjs');

const main = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8');
const rule = /const fullscreenOnly = (\(permission, url\) => [^;]+);/.exec(main);

test('main.cjs grants only fullscreen, only to the workspace origin', () => {
  assert.ok(rule, 'the fullscreenOnly rule is in main.cjs');
  const origin = 'http://127.0.0.1:41234';
  const fullscreenOnly = new Function('sameOrigin', 'origin', `return ${rule[1]};`)(sameOrigin, origin);
  assert.equal(fullscreenOnly('fullscreen', `${origin}/`), true);
  assert.equal(fullscreenOnly('fullscreen', origin), true);
  for (const url of ['http://127.0.0.1:9/', 'https://example.com/', '', 'not a url', `http://user:pw@127.0.0.1:41234/`])
    assert.equal(fullscreenOnly('fullscreen', url), false, url);
  for (const permission of ['media', 'geolocation', 'notifications', 'clipboard-read', 'openExternal', 'pointerLock'])
    assert.equal(fullscreenOnly(permission, `${origin}/`), false, permission);
  // Both handlers use the rule; the device handler still refuses everything.
  assert.match(main, /setPermissionRequestHandler\(\(_contents, permission, callback, details\) =>\s*callback\(fullscreenOnly\(permission, details && details\.requestingUrl\)\)\)/);
  assert.match(main, /setPermissionCheckHandler\(\(_contents, permission, requestingOrigin\) => fullscreenOnly\(permission, requestingOrigin\)\)/);
  assert.match(main, /setDevicePermissionHandler\(\(\) => false\)/);
});
