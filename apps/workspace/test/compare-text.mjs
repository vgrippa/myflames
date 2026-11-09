// Compare's words: the time change as a ratio or percentage, matching
// metadata and status badges without raw enums, sentence-case severities.
// Run: node test/compare-text.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
const built = await build({ entryPoints: ['src/compareText.ts'], bundle: true, write: false, format: 'esm', platform: 'node' });
const t = await import('data:text/javascript;base64,' + Buffer.from(built.outputFiles[0].text).toString('base64'));
const n = (value, digits) => new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value);

// A change of 100% or more reads as a ratio; whole numbers with separators from 100×.
assert.equal(t.changeText(74867.34), `${n(750, 0)}× slower`);
assert.equal(t.changeText(149900), `${n(1500, 0)}× slower`, 'thousands separator');
assert.equal(t.changeText(100), '2× slower');
assert.equal(t.changeText(140), '2.4× slower');
assert.equal(t.changeText(-50), '2× faster');
assert.equal(t.changeText(-75), '4× faster');
assert.equal(t.changeText(35.24), '35% slower');
assert.equal(t.changeText(-20.06), '20% faster');
assert.equal(t.changeText(4.25), `${n(4.25, 1)}% slower`);
assert.equal(t.changeText(0), 'No change');
assert.equal(t.changeText(null), '—');
assert.equal(t.changeExact(74867.34), `+${n(74867.34, 2)}%`);

// Matching metadata in words, never snake_case enums.
for (const method of ['unique_subtree', 'unique_operator', 'parent_context', 'unique_table', 'child_context', 'ambiguous_operator', 'unmatched']) {
  const text = t.matchText({ method, confidence: 'high' });
  assert.ok(!/_/.test(text) && /^[A-Z]/.test(text), `${method}: ${text}`);
}
assert.equal(t.matchText({ method: 'unique_operator', confidence: 'high' }), 'Same operator · high confidence');
assert.equal(t.matchText({ method: 'ambiguous_operator', confidence: 'uncertain' }), 'Matched by position · tentative');
assert.equal(t.matchText({ method: 'unmatched', confidence: 'high' }), 'No counterpart');
assert.equal(t.matchText({ method: 'new_rule', confidence: 'medium' }), 'New rule · medium confidence');
assert.deepEqual(['removed', 'added', 'changed', 'unchanged', 'uncertain'].map(t.statusText), ['Removed', 'Added', 'Changed', 'Unchanged', 'Uncertain']);
assert.deepEqual(['critical', 'warn', 'info'].map(t.severityText), ['Critical', 'Warning', 'Note']);

// One title when both sides carry the same label.
assert.equal(t.pairTitle('Sort', 'Sort'), 'Sort');
assert.equal(t.pairTitle('Table scan [t]', 'Index lookup [t]'), 'Table scan [t] → Index lookup [t]');
assert.equal(t.pairTitle('Limit: 20 rows', null), 'Limit: 20 rows');
assert.equal(t.pairTitle(null, 'Sort'), 'Sort');
console.log('compare text: ratios, percentages, matching words and titles passed');
