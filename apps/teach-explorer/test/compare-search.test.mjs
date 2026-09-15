import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

// Use the build's TypeScript compiler so these tests also run on Node 18.
const source = readFileSync(new URL('../src/data/compare-search.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const context = { exports: {}, URLSearchParams };
vm.runInNewContext(outputText, context);
const parse = search => Array.from(context.exports.readCompareSelection(
  search, ['hash', 'btree'], ['hash'],
));

test('missing selection uses defaults', () => {
  assert.deepEqual(parse('?n=1000'), ['hash']);
});
test('explicitly empty share link stays empty after reload', () => {
  assert.deepEqual(parse('?compare='), []);
});
test('malformed percent text is ignored without throwing', () => {
  assert.deepEqual(parse('?compare=hash,%'), ['hash']);
  assert.deepEqual(parse('?compare=%25'), []);
});
test('URL values are decoded once and duplicates are removed', () => {
  assert.deepEqual(parse('?compare=hash%2Cbtree,hash'), ['hash', 'btree']);
});
