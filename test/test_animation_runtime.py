"""Execute the lesson clock with a deterministic browser-frame substitute."""
import shutil
import subprocess
import unittest

from myflames.teach._anim import ANIM_JS


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed to execute lesson JavaScript')
class TestAnimationRuntime(unittest.TestCase):
    def run_js(self, body):
        harness = r'''
const assert = require('assert');
let now = 0;
let frames = [];
let timers = [];
global.window = {matchMedia: () => ({matches: false})};
global.requestAnimationFrame = fn => frames.push(fn);
global.setTimeout = (fn, ms) => { const t = {fn, at: now + ms}; timers.push(t); return t; };
global.clearTimeout = t => { timers = timers.filter(v => v !== t); };
function advance(ms) {
  now += ms;
  const ready = timers.filter(t => t.at <= now);
  timers = timers.filter(t => t.at > now);
  ready.forEach(t => t.fn());
  const pending = frames;
  frames = [];
  pending.forEach(fn => fn(now));
}
'''
        result = subprocess.run(
            [shutil.which('node'), '-e', harness + ANIM_JS + body],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pause_freezes_delay_and_preserves_remaining_time(self):
        self.run_js(r'''
let completed = 0;
const tl = anim.timeline().delay(1000).call(() => completed++).play();
advance(0);
advance(400);
anim.setPaused(true);
advance(1000);
assert.equal(completed, 0, 'delay advanced while paused');
assert.equal(tl.getCurrentTime(), 400);
anim.setPaused(false);
advance(599);
assert.equal(completed, 0);
advance(1);
assert.equal(completed, 1);
''')

    def test_live_speed_change_applies_to_delay(self):
        self.run_js(r'''
let completed = false;
anim.timeline().delay(1000).call(() => completed = true).play();
advance(0);
advance(400);
anim.setSpeed(2);
advance(299);
assert.equal(completed, false);
advance(1);
assert.equal(completed, true);
''')

    def test_resume_starts_at_original_eased_position(self):
        self.run_js(r'''
let value = -1;
const tl = anim.timeline().add({from: 0, to: 100, duration: 1000,
  ease: anim.easeInOutCubic, onUpdate: v => value = v});
tl.playFrom(500);
advance(0);
assert.equal(value, 50, 'seek jumped to tween beginning');
assert.equal(tl.getCurrentTime(), 500);
advance(250);
assert.equal(value, 93.75, 'resume changed the easing curve');
advance(250);
assert.equal(value, 100);
assert.equal(tl.isPlaying(), false);
''')

    def test_resume_does_not_extend_later_delays(self):
        self.run_js(r'''
let completed = false;
const tl = anim.timeline().delay(1000).delay(1000).call(() => completed = true);
tl.playFrom(500);
advance(0);
advance(500);
advance(0);
advance(999);
assert.equal(completed, false);
advance(1);
assert.equal(completed, true, 'later delay was lengthened by seek offset');
''')

    def test_stop_cancels_delay(self):
        self.run_js(r'''
let completed = false;
const tl = anim.timeline().delay(1000).call(() => completed = true).play();
advance(0);
advance(100);
tl.stop();
advance(2000);
assert.equal(completed, false);
''')

    def test_reduced_motion_finishes_delay_and_tween(self):
        self.run_js(r'''
window.matchMedia = () => ({matches: true});
let completed = false;
let value = 0;
anim.timeline().delay(1000).add({from: 0, to: 10, duration: 1000,
  onUpdate: v => value = v}).play(() => completed = true);
advance(0);
assert.equal(completed, true);
assert.equal(value, 10);
''')

    def test_zero_duration_step_has_no_default_duration(self):
        self.run_js(r'''
let value = 0;
const tl = anim.timeline().delay(0).add({from: 0, to: 10, duration: 0,
  onUpdate: v => value = v}).play();
advance(0);
advance(0);
assert.equal(value, 10);
assert.equal(tl.isPlaying(), false);
assert.equal(tl.getTotalDuration(), 0);
''')
