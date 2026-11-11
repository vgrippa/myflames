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
const bundle = await build({stdin:{contents:'export * from "./src/themes"; export * from "./src/charts/core/appearance";',resolveDir:fileURLToPath(new URL('../',import.meta.url))},bundle:true,write:false,format:'iife',globalName:'ThemeTest',platform:'browser'});
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
    const wanted=await page.evaluate(()=>Object.fromEntries(ThemeTest.CHART_SURFACES.map(name=>[name,getComputedStyle(document.querySelector('.workspace')).getPropertyValue(name).trim()])));
    await frame.waitForFunction(values=>Object.entries(values).every(([key,value])=>document.documentElement.style.getPropertyValue(key)===value),{},wanted);
  }
  // Abuse cases: non-parent source, invalid colors, var chains and unknown
  // properties never enter the frame style; valid color values use setProperty.
  const before=await frame.$eval('html',e=>e.style.getPropertyValue('--mfc-canvas'));
  await frame.evaluate(()=>window.postMessage({type:'myflames-appearance',theme:'dark',surfaces:{'--mfc-canvas':'#ff0000'}},'*'));
  await page.evaluate(()=>document.querySelector('#theme-test-frame').contentWindow.postMessage({type:'myflames-appearance',theme:'dark',surfaces:{'--mfc-canvas':'red; background:url(https://example.invalid)','--mfc-card':'var(--evil)','--evil':'#ff0000'}},'*'));
  await new Promise(resolve=>setTimeout(resolve,100));
  assert.equal(await frame.$eval('html',e=>e.style.getPropertyValue('--mfc-canvas')),before);
  assert.equal(await frame.$eval('html',e=>e.style.getPropertyValue('--evil')),'');
  assert.equal(await frame.evaluate(()=>{try{return !!parent.document}catch{return false}}),false,'sandbox still isolates parent');
  await page.evaluate(()=>{window.stopThemeFrame();document.querySelector('#theme-test-frame').remove()});
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
