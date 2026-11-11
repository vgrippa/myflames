import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { build } from 'esbuild';
import { block, themeRegistry, themeCss } from './theme-contrast-unit.mjs';
import { withThemeWorkspace } from './theme-browser-harness.mjs';
const read = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
const ids = themeRegistry.map(theme => theme.id);
assert.equal(new Set(ids).size, ids.length);
const dark = block(':root[data-theme="dark"] {', read('style.css'));
for (const theme of themeRegistry.slice(3)) {
  const tokens = block(`:root[data-theme="${theme.id}"] {`, themeCss);
  for (const token of Object.keys(dark)) assert.ok(token in tokens, `${theme.id}: missing ${token}`);
  assert.match(themeCss, new RegExp(`:root\\[data-theme="${theme.id}"\\] \\{\\s*color-scheme: ${theme.base};`));
  assert.equal(theme.swatches.length, 4);
  assert.match(theme.id, /^[a-z]+(?:-[a-z]+)*$/);
}
// Discover a backend tuple/list/set allowlist through Python AST, without
// importing the store or introducing another list of allowed ids here.
const backend = spawnSync(process.env.PYTHON || 'python3', ['-c', `import ast,json,sys
root=ast.parse(open(sys.argv[1]).read()); found=[]
for node in ast.walk(root):
 if isinstance(node,(ast.Assign,ast.AnnAssign)):
  try: values=ast.literal_eval(node.value)
  except (ValueError,TypeError): continue
  if isinstance(values,(list,tuple,set)) and all(isinstance(x,str) for x in values) and {'system','light','dark'}.issubset(values): found.append(list(values))
print(json.dumps(found))`, fileURLToPath(new URL('../../../myflames/workspace_store.py',import.meta.url))], {encoding:'utf8'});
assert.equal(backend.status,0,backend.stderr);
const allowlists = JSON.parse(backend.stdout);
for (const allowlist of allowlists) assert.deepEqual([...allowlist].sort(), [...ids].sort(), 'backend theme allowlist matches registry');
if (!allowlists.length) console.log('Backend theme allowlist absent; registry ids ready for preferences lane:', ids.join(', '));
const builtHtml = readFileSync(new URL('../../../myflames/ui_assets/index.html',import.meta.url),'utf8');
assert.ok(!builtHtml.includes('__MYFLAMES_THEME_IDS__'),'boot list is generated');
assert.ok(builtHtml.includes(JSON.stringify(ids)),'boot script uses the registry id list');
assert.ok(!read('charts/core/styles.ts').includes(':root:not([data-theme=light])'), 'late chart styles cannot override named palettes');
const schemaSource = read('SchemaDiagram.tsx');
assert.match(schemaSource,/useState\(\(\) => appliedThemeBase\(\) === "dark"\)/);
assert.match(schemaSource,/new MutationObserver\(update\)/);
assert.match(schemaSource,/attributeFilter: \["data-theme"\]/);
assert.match(schemaSource,/<option value="auto">Follow appearance<\/option>/);
const bundle = await build({stdin:{contents:'export * from "./src/themes"; export * from "./src/charts/core/appearance"; export { CHART_THEME_DEFAULTS } from "./src/charts/core/styles";',resolveDir:fileURLToPath(new URL('../',import.meta.url))},bundle:true,write:false,format:'iife',globalName:'ThemeTest',platform:'browser'});
await withThemeWorkspace(async page => {
  await page.addScriptTag({ content: bundle.outputFiles[0].text });
  // Every palette action applies and persists its registry id. Under a dark
  // OS preference, custom light themes must still resolve to light.
  await page.emulateMediaFeatures([{ name:'prefers-color-scheme',value:'dark' }]);
  for (const theme of themeRegistry) {
    await page.keyboard.down('Meta'); await page.keyboard.press('k'); await page.keyboard.up('Meta');
    await page.waitForSelector('[aria-label="Search commands"]');
    await page.type('[aria-label="Search commands"]', `Theme: ${theme.name}`);
    const option = await page.waitForFunction(label => [...document.querySelectorAll('.command-option')].find(node => node.querySelector('bdi')?.textContent === label), {}, `Theme: ${theme.name}`);
    await option.asElement().click(); await option.dispose();
    // The palette runs the command on the next tick (CommandPalette.tsx).
    await page.waitForFunction(id => (document.documentElement.dataset.theme || 'system') === id, {}, theme.id);
    const state = await page.evaluate(() => ({ id:document.documentElement.dataset.theme || 'system',scheme:getComputedStyle(document.documentElement).colorScheme,base:ThemeTest.appliedThemeBase() }));
    assert.equal(state.id,theme.id);
    await page.waitForFunction(async id => {
      const token=localStorage.getItem('myflames.session-proof');
      const reply=await fetch('/api/settings/ui',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Token':token},body:'{}'}).then(r=>r.json());
      return reply.preferences?.theme===id;
    },{},theme.id);
    const expected = theme.id === 'system' ? 'dark' : theme.base;
    assert.equal(state.scheme,expected); assert.equal(state.base,expected);
    // This is the same helper used by SchemaDiagram's theme observer.
  }
  await page.reload(); await page.waitForSelector('.workspace');
  assert.equal(await page.$eval('html',e=>e.dataset.theme),'one-dark','blocking boot restores custom theme');
  await page.addScriptTag({ content:bundle.outputFiles[0].text });
  await page.evaluate(() => {
    const frame=document.createElement('iframe'); frame.id='theme-test-frame'; frame.sandbox='allow-scripts';
    frame.srcdoc=ThemeTest.themeFrame('<html><head></head><body><div>Chart</div></body></html>');
    document.body.append(frame); window.stopThemeFrame=ThemeTest.bindFrameAppearance(frame);
  });
  const frameElement=await page.$('#theme-test-frame'); const frame=await frameElement.contentFrame();
  for (const theme of [...themeRegistry, ...themeRegistry.slice(0, 3)]) {
    await page.evaluate(id=>ThemeTest.applyTheme(id),theme.id);
    const expected=theme.id==='system'?'dark':theme.base;
    await frame.waitForFunction(base=>document.documentElement.dataset.theme===base,{},expected);
    const wanted=await page.evaluate(()=>{
      const chart=getComputedStyle(document.querySelector('.workspace'));
      return Object.fromEntries(ThemeTest.CHART_SURFACES.map(name=>[name,chart.getPropertyValue(name).trim() || ThemeTest.CHART_THEME_DEFAULTS[ThemeTest.appliedThemeBase()][name]]));
    });
    const accents=await page.evaluate(()=>{
      const root=getComputedStyle(document.documentElement);
      return Object.fromEntries(['--accent',...'text fill hover soft line ink'.split(' ').map(role=>'--accent-'+role),'--focus'].map(name=>[name,root.getPropertyValue(name).trim()]));
    });
    // Inspect every computed token, not just whether CSS.supports accepts it.
    for(const [name,value] of Object.entries({...wanted,...accents})) {
      assert.match(value,/^(#[0-9a-f]{3,8}|rgba?\([0-9.,%/ ()+-]+\))$/i,`${theme.id}: ${name} = ${value}`);
    }
    await frame.waitForFunction(values=>Object.entries(values).every(([key,value])=>document.documentElement.style.getPropertyValue(key)===value),{},accents);
    await frame.waitForFunction(values=>Object.entries(values).every(([key,value])=>document.documentElement.style.getPropertyValue(key)===value),{},wanted);
  }
  // A marker listener runs after the fixed script, so assertions wait for
  // message processing instead of sleeping and potentially missing a write.
  // Stop normal sync so it cannot overwrite an abusive value before a check.
  await page.evaluate(()=>window.stopThemeFrame());
  await frame.evaluate(()=>{window.appearanceMessages=0;window.addEventListener('message',event=>{if(event.data?.type==='myflames-appearance')window.appearanceMessages++;});});
  const baseline=await frame.$eval('html',e=>Object.fromEntries(['--mfc-canvas','--mfc-card','--accent','--accent-text'].map(key=>[key,e.style.getPropertyValue(key)])));
  const beforeCount=await frame.evaluate(()=>window.appearanceMessages);
  await frame.evaluate(()=>window.postMessage({type:'myflames-appearance',theme:'dark',surfaces:{'--mfc-canvas':'#ff0000'},accents:{'--accent':'#ff0000'}},'*'));
  await frame.waitForFunction(count=>window.appearanceMessages>count,{},beforeCount);
  assert.deepEqual(await frame.$eval('html',e=>Object.fromEntries(['--mfc-canvas','--mfc-card','--accent','--accent-text'].map(key=>[key,e.style.getPropertyValue(key)]))),baseline,'non-parent source refused');
  const abuse=[String.raw`v\61r(--mfc-card)`,String.raw`\76 ar(--mfc-card)`,String.raw`rgb(from v\61r(--x) r g b)`,'attr(data-x type(<color>))','env(x, red)','if(media(width > 1px): red; else: blue)','inherit','initial','unset','revert',17,{},[],'','x'.repeat(200000),'var(--evil)','url(https://example.invalid)','rgb(from red r g b)','red; background:url(https://example.invalid)'];
  for(const [index,value] of abuse.entries()) {
    const count=await frame.evaluate(()=>window.appearanceMessages);
    await page.evaluate(value=>document.querySelector('#theme-test-frame').contentWindow.postMessage({type:'myflames-appearance',theme:'dark',surfaces:{'--mfc-canvas':value,'--mfc-card':value,'--evil':'#ff0000'},accents:{'--accent':value,'--accent-text':value,'--evil':'#ff0000'}},'*'),value);
    await frame.waitForFunction(count=>window.appearanceMessages>count,{},count);
    assert.deepEqual(await frame.$eval('html',e=>Object.fromEntries(['--mfc-canvas','--mfc-card','--accent','--accent-text'].map(key=>[key,e.style.getPropertyValue(key)]))),baseline,`abuse payload ${index} refused on both paths`);
    assert.equal(await frame.$eval('html',e=>e.style.getPropertyValue('--evil')),'');
  }
  assert.equal(await frame.evaluate(()=>{try{return !!parent.document}catch{return false}}),false,'sandbox still isolates parent');
  await page.evaluate(()=>document.querySelector('#theme-test-frame').remove());
  // Exercise the mounted React observer too. Importing schema SQL only parses
  // a local document; the harness refuses every database connection/read.
  const clickText = async (text, scope = 'body') => {
    const button = await page.waitForFunction(({text,scope}) => [...document.querySelectorAll(`${scope} button`)].find(node => node.textContent.trim() === text && node.getClientRects().length), {}, {text,scope});
    await button.asElement().click(); await button.dispose();
  };
  await clickText('Database diagram', '.main-nav');
  await clickText('Import a schema', '.diagram-view');
  await page.select('[aria-label="Import format"]', 'sql');
  await page.type('.diagram-paste textarea', 'CREATE TABLE theme_probe (id INT PRIMARY KEY);');
  await clickText('Import diagram', 'dialog[open]');
  await page.waitForSelector('.diagram-table');
  await clickText('Mermaid', '.diagram-view');
  await page.waitForSelector('.diagram-mermaid-preview svg');
  assert.equal(await page.$eval('[aria-label="Color template"]', node => node.value), 'auto');
  for (const theme of themeRegistry) {
    const previous = await page.$eval('.diagram-mermaid-panel', node => ({base:node.dataset.theme, svg:node.querySelector('svg')?.id}));
    await page.evaluate(id => ThemeTest.applyTheme(id), theme.id);
    const expected = theme.id === 'system' || theme.base === 'dark' ? 'dark' : 'default';
    await page.waitForFunction(({base,previous}) => {
      const panel=document.querySelector('.diagram-mermaid-panel'), svg=panel?.querySelector('svg');
      return panel?.dataset.theme===base && svg && (previous.base===base || svg.id!==previous.svg);
    }, {}, {base:expected,previous});
  }

});
console.log('Theme registry, palette, boot, chart frame isolation and applied-base contracts passed.');
