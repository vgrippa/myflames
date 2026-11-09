// Socket-free checks of real rendering, persisted settings and escaping.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const here = path.dirname(fileURLToPath(import.meta.url));
const directory = await fs.mkdtemp(path.join(here, '.memory-unit-'));
const errors = [];
const originalError = console.error;
try {
  const output = await build({ stdin: { contents: `export {MemoryView, memoryNotes} from './Memory';
    export {InspectorContext, NotesButton} from './kit'; export {InstrumentsLinkContext} from './Instruments'; export {connectionKey} from './binding';
    export {normalizeLab, TABLE_IDS} from './session'; export {WORKLOAD_VIEWS} from './serverViews';`,
    resolveDir: path.join(here, '../src'), loader: 'tsx' }, bundle: true, platform: 'node', format: 'cjs',
    write: false, jsx: 'automatic', loader: {'.css':'empty'}, external:['react','react-dom'] });
  const filename = path.join(directory, 'memory.cjs');
  await fs.writeFile(filename, output.outputFiles[0].text);
  const {MemoryView, memoryNotes, NotesButton, InspectorContext, InstrumentsLinkContext, connectionKey, normalizeLab, TABLE_IDS, WORKLOAD_VIEWS} = createRequire(import.meta.url)(filename);
  console.error = (...args) => {
    if (String(args[0]).includes('useLayoutEffect does nothing on the server')) return;
    // Node 25's built-in Web Storage warns when code reads localStorage without
    // --localstorage-file; the view guards storage access, so this is not an error.
    if (String(args[0]).includes('--localstorage-file')) return;
    errors.push(args.map(String).join(' '));
  };
  const connection = {host:'localhost',port:3306,user:'app'};
  const render = (report, selected = '', other = false) => renderToStaticMarkup(React.createElement(InstrumentsLinkContext.Provider, {value() {}}, React.createElement(InspectorContext.Provider,
    {value:{open:true,setOpen(){},width:340,setWidth(){},sheet:false}}, React.createElement(MemoryView,
      {connection,disabled:false,selection:{key:selected,select(){}},interval:5,onIntervalChange(){},
       report:{key:other ? 'other' : connectionKey(connection),label:'fixture',report},onReport(){}}))));
  // Popovers mount on click, which SSR cannot trigger. Render the actual
  // NotesButton's children with the notes supplied by the production helper.
  const renderNotes = report => renderToStaticMarkup(NotesButton({notes: memoryNotes(report)}).props.children);
  const inspector = html => html.split('class="inspector-scroll"')[1] || '';
  const rowHtml = (html, key) => html.match(new RegExp(`<tr data-row-key="${key}"[^>]*>(.*?)</tr>`, 's'))?.[1] || '';
  const fixture = async name => JSON.parse(await fs.readFile(path.join(here,'fixtures',`memory-${name}.json`),'utf8'));
  const ok = await fixture('ok');
  const html = render(ok, 'category:buffer_pool');
  assert.match(html, /Memory by category/);
  assert.match(html, /<dt>Current<\/dt><dd><span class="memory-value">1\.0 GiB<\/span> <span class="cell-muted">1,073,741,824 B<\/span><\/dd>/);
  assert.match(html, /<dt>Buffer pool setting<\/dt><dd><span class="memory-value">1\.0 GiB<\/span> <span class="cell-muted">1,073,741,824 B<\/span><\/dd>/);
  assert.match(html, /<dt>Tracked total<\/dt><dd><span class="memory-value">1\.4 GiB<\/span> <span class="cell-muted">1,476,395,008 B<\/span><\/dd>/);
  assert.match(html, /<dt>Sum of event highs<\/dt><dd><span class="memory-value">1\.5 GiB<\/span> <span class="cell-muted">1,572,864,000 B<\/span><\/dd>/);
  assert.match(html, /400 of 400/);
  assert.match(html, /Performance Schema tracked share/);
  const notes = renderNotes(ok);
  assert.match(notes, /above both 1%.*16 MiB/);
  assert.match(notes, /25% rule judges only temporary tables, sort, join and hash buffers, and network/);
  assert.doesNotMatch(inspector(html), /Growth triggers|25% rule|1 GiB session rule/,
    'general rules belong only to Notes');
  for (const rule of [/Growth triggers/g, /25% rule/g, /1 GiB session rule/g])
    assert.equal((renderNotes(ok).match(rule) || []).length, 1, 'each general rule is stated once');
  const watch = await fixture('watch');
  const sort = render(watch, 'category:sort');
  assert.match(rowHtml(sort, 'category:sort'), /status-pill is-watch.*Watch/s, 'a category finding identifies its row');
  assert.doesNotMatch(rowHtml(sort, 'category:buffer_pool'), /status-pill/, 'unflagged rows have no warning');
  assert.match(inspector(sort), /Sort exceeds this threshold/);
  assert.doesNotMatch(inspector(sort), /Inspect this session|short allocation burst|Rule of thumb:/);
  assert.doesNotMatch(inspector(render(watch, 'category:buffer_pool')), /Rules of thumb|Sort exceeds|Inspect this session/);
  const session = inspector(render(watch, 'session:42'));
  assert.match(session, /Inspect this session/);
  assert.doesNotMatch(session, /Sort exceeds|short allocation burst/);
  const account = inspector(render(watch, `account:${JSON.stringify(['app','localhost'])}`));
  assert.doesNotMatch(account, /Rules of thumb|Sort exceeds|Inspect this session/);
  // Exercise session-table pills using a report whose default is By session.
  const sessionOnly = render({...watch, categories: [], collection: {...watch.collection, events: false}});
  assert.match(rowHtml(sessionOnly, 'session:42'), /status-pill is-watch.*Watch/s, 'the default session table exposes a session finding');
  assert.match(sort, /<col data-col="name" style="width:180px"/);
  // Highs are upper bounds (current plus summed headroom), never recorded peaks.
  assert.match(html, /Each event high is an upper bound/);
  assert.match(html, /TRUNCATE of a memory summary table resets highs to current bytes/);
  assert.doesNotMatch(html, /independent peaks|can occur at different times/);
  // The 1 GiB session rule names its cross-thread free caveat.
  assert.match(notes, /1 GiB session rule can be overstated: memory another thread freed/);
  // Join and hash buffers: hashed_operation is not only joins.
  const joinRow = {...ok.categories[0], key: 'join', label: 'Join and hash buffers', current_bytes: 1048576, high_bytes: 2097152, share_pct: 0.1};
  const join = render({...ok, categories: [...ok.categories, joinRow]}, 'category:join');
  assert.match(join, /hashed_operation also serves other hashed operations, such as INTERSECT and EXCEPT/);
  assert.doesNotMatch(html, /hashed_operation/, 'only the join category explains hashed_operation');
  // MariaDB never instruments the sort buffer or NET::buff: said, not hidden.
  const mariadb = await fixture('mariadb-on');
  assert.match(render(mariadb, 'category:sort'), /MariaDB does not instrument the sort buffer \(Filesort_buffer::sort_keys\)/);
  const network = render(mariadb, 'category:network');
  assert.match(network, /MariaDB does not instrument the network buffer \(NET::buff\)/);
  assert.match(network, /<dt>Current<\/dt><dd>Not collected<\/dd>/);
  assert.match(render(mariadb), /data-row-key="category:network"/);
  assert.doesNotMatch(render(mariadb, 'category:buffer_pool'), /does not instrument the sort buffer/, 'the note belongs to Sort only');
  // A session below 0 freed another thread's memory: no negative byte count.
  const below = render({...ok, sessions: [{...ok.sessions[0], current_bytes: -5242880}]}, 'session:42');
  assert.match(below, /<dt>Current<\/dt><dd>Below 0 \(freed by another thread\)<\/dd>/);
  assert.doesNotMatch(below, /-5,242,880|-5\.0 MiB/);
  assert.match(below, /memory another thread freed can still count here/);
  // Foreground threads with INSTRUMENTED = NO: context, and a gate to setup_actors.
  assert.match(render({...ok, thread_coverage: {uninstrumented: 1, foreground: 10}}, 'category:buffer_pool'),
    /<dt>Foreground threads instrumented<\/dt><dd>9 of 10<\/dd>/);
  assert.match(render(ok, 'category:buffer_pool'), /<dt>Foreground threads instrumented<\/dt><dd>4 of 4<\/dd>/);
  const gated = {...ok, status: 'not_judged', gate: '2 of 10 foreground threads are not instrumented, so their memory is recorded nowhere; open Server > Instruments to review setup_actors.', gate_row: 'actors'};
  assert.match(render(gated), /data-instruments-row="actors"/);
  const uncollected = await fixture('not-collected');
  for (const selected of ['session:42', `account:${JSON.stringify(['app','localhost'])}`]) {
    const missing = render(uncollected, selected);
    assert.match(missing, /instruments are disabled; open Server &gt; Instruments/);
    assert.match(missing, /Current<.*?Not collected/s);
    assert.match(missing, /<dt>Current<\/dt><dd>Not collected<\/dd>/);
    assert.match(missing, /<dt>Sum of event highs<\/dt><dd>Not collected<\/dd>/);
  }
  assert.match(html, /memory_summary_global_by_event_name/);
  assert.equal((html.match(/data-action="refresh"/g)||[]).length, 1);
  assert.match(html, /sum of highs/);
  const fallback = render(await fixture('mariadb-fallback'), 'session:42');
  assert.match(fallback, /sum of highs not collected/);
  assert.doesNotMatch(fallback, /Not collected sum of highs/);
  assert.match(fallback, /data-value="sessions">By session/);
  assert.match(fallback, /aria-label="Memory by session"/);
  assert.doesNotMatch(fallback, /aria-label="Memory by category"/);
  assert.match(fallback, /information_schema.PROCESSLIST/);
  assert.match(fallback, /Session high/);
  assert.match(fallback, /Not judged/);
  assert.match(fallback, /data-instruments-row="ps"/);
  const hostile = await fixture('hostile');
  const row = hostile.accounts[0];
  const escaped = render(hostile, `account:${JSON.stringify([row.user,row.host])}`);
  assert.match(escaped, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(escaped, /<img src=x/);
  assert.match(escaped, /<bdi>&lt;img/);
  assert.match(render(await fixture('denied')), /Not collected tracked/);
  assert.match(render(ok, '', true), /data-other-connection="true"/);
  assert.doesNotMatch(render(await fixture('not-collected'), '', true), /data-instruments-row=/,
    'a stale report cannot act on the current server');
  const normalized = normalizeLab({slow:{kind:'memory',memory_interval:30,columns_v1:{'memory.categories':{hidden:['high']}}}});
  assert.equal(normalized.slow.kind,'memory');
  assert.equal(normalized.slow.memory_interval,30);
  assert.deepEqual(normalized.slow.columns_v1['memory.categories'].hidden,['high']);
  for (const interval of [undefined,1,61,true,'5']) assert.equal(normalizeLab({slow:{memory_interval:interval}}).slow.memory_interval,5);
  assert.ok(TABLE_IDS.includes('memory.sessions'));
  assert.deepEqual(WORKLOAD_VIEWS.filter(v => v.group==='Resources').map(v=>v.id),['memory','waits','io']);
  assert.deepEqual(errors,[]);
  console.log('Memory unit checks passed: real SSR, exact numbers, fallback, escaping, stale actions, persisted intervals and columns, navigation.');
} finally {
  console.error = originalError;
  await fs.rm(directory,{recursive:true,force:true});
}
