// Pure completion heuristics. These tests never connect to a database.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
const built = await build({ entryPoints: [fileURLToPath(new URL('../src/sqlCompletion.ts', import.meta.url))], bundle: true, write: false, format: 'esm', platform: 'node' });
const { completionContext, columnTargets, suggestions } = await import('data:text/javascript;base64,' + Buffer.from(built.outputFiles[0].text).toString('base64'));
const at = marked => { const n = marked.indexOf('|'); return completionContext(marked.replace('|', ''), n); };
let passed = 0;
function test(name, fn) { try { fn(); passed++; } catch (e) { console.error(name); throw e; } }
test('plain prefixes and replacement at the middle of a word', () => {
  assert.deepEqual([at('SELECT nam|e FROM users').start, at('SELECT nam|e FROM users').end, at('SELECT nam|e FROM users').prefix], [7,11,'nam']);
});
test('qualifiers and quoted prefixes', () => {
  assert.deepEqual(at('SELECT `a``b`.`na|me` FROM `odd table` AS `a``b`').qualifiers, ['a`b']);
  assert.equal(at('SELECT `a``b`.`na|me` FROM `odd table` AS `a``b`').prefix, 'na');
});
test('aliases can follow the cursor in a SELECT', () => {
  const c = at('SELECT u.na| FROM app.users AS u JOIN app.roles r ON u.id=r.user_id');
  assert.deepEqual(columnTargets(c,'wrong'), [{schema:'app',table:'users',alias:'u'}]);
});
test('qualifiers resolve default schema and explicit database.table', () => {
  assert.deepEqual(columnTargets(at('SELECT u.| FROM users u'),'app'), [{schema:'app',table:'users',alias:'u'}]);
  assert.deepEqual(columnTargets(at('SELECT app.users.|'),'wrong'), [{schema:'app',table:'users',alias:'users'}]);
});
test('unknown default schema is not guessed', () => assert.deepEqual(columnTargets(at('SELECT u.| FROM users u'),''), []));
test('table and database contexts', () => {
  assert.equal(at('SELECT * FROM app.us|').place,'table');
  assert.equal(at('USE ap|').place,'schema');
});
test('comments strings variables parameters and selected text suppress suggestions', () => {
  for (const sql of ["SELECT 'na|me'", 'SELECT "na|me"', 'SELECT 1 /* na| */', 'SELECT 1 -- na|', 'SELECT 1 # na|', 'SELECT @na|', 'SELECT :na|', "SELECT 'unclosed|", 'SELECT /*! SELECT na| */']) assert.equal(at(sql), null, sql);
  assert.equal(completionContext('SELECT names',7,12),null);
  assert.ok(at('SELECT 1 /* x */ na|'));
});
test('backslash escaped strings remain suppressed', () => assert.equal(at("SELECT 'it\\'s na|'"),null));
test('separate statements do not leak aliases', () => assert.deepEqual(at('SELECT * FROM users u; SELECT u.|').tables,[]));
test('nested SELECT aliases stay in their query', () => {
  const c=at('SELECT * FROM users u WHERE EXISTS (SELECT r.| FROM roles r WHERE r.user_id=u.id)');
  assert.deepEqual(c.tables,[{schema:'',table:'roles',alias:'r'}]);
  assert.deepEqual(at('SELECT u.| FROM users u WHERE EXISTS (SELECT 1 FROM roles r)').tables,[{schema:'',table:'users',alias:'u'}]);
});
test('function arguments can use outer query aliases', () => assert.deepEqual(columnTargets(at('SELECT COUNT(u.|) FROM users u'),'app'),[{schema:'app',table:'users',alias:'u'}]));
test('CTE and derived aliases are not treated as stored tables', () => {
  assert.deepEqual(at('WITH u AS (SELECT * FROM users) SELECT u.| FROM u').tables,[]);
  assert.deepEqual(at('SELECT u.| FROM (SELECT * FROM users) u').tables,[]);
});
test('bounded input and explicit DELIMITER scripts', () => {
  assert.equal(completionContext('a'.repeat(60001),60001),null);
  assert.equal(at('DELIMITER $$\nSELECT na|'),null);
});
test('quotes are escaped and ambiguous columns get their alias', () => {
  const c=at('SELECT na| FROM app.users u JOIN app.roles r ON u.id=r.id');
  const items=suggestions(c,{columns:[{name:'na`me',type:'varchar(20)',table:c.tables[0]},{name:'name',type:'text',table:c.tables[1]}]});
  assert.deepEqual(items.map(s=>s.insert),['u.`na``me`','r.name']);
});
test('qualified completion replaces only the final component', () => {
  const c=at('SELECT u.na| FROM app.users u');
  assert.equal(suggestions(c,{columns:[{name:'name',type:'text',table:c.tables[0]}]})[0].insert,'name');
});
test('schema table and keyword choices have stable types and limit', () => {
  const c=at('SELECT * FROM t|');
  const items=suggestions(c,{tables:Array.from({length:100},(_,i)=>({name:`table${i}`,schema:'app'}))});
  assert.equal(items.length,40);
  assert.equal(items[0].kind,'table');
  assert.equal(suggestions(at('SEL|'),{})[0].insert,'SELECT');
});
test('NO_BACKSLASH_ESCAPES follows the known editor session mode', () => {
  const sql = "SELECT 'x\\' na";
  assert.equal(completionContext(sql,sql.length),null);
  assert.equal(completionContext(sql,sql.length,sql.length,false).prefix,'na');
});
test('unfinished quoted table prefixes preserve following draft SQL', () => {
  const context = at('SELECT * FROM `us| WHERE active=1');
  assert.equal(context.end, 17);
  assert.equal(context.prefix, 'us');
});
console.log(`${passed} SQL completion tests passed`);
