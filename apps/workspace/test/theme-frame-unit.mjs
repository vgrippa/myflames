// Runtime checks of the fixed frame script when Chromium cannot launch.
// Native CSS.supports behavior is covered separately by themes.mjs in Chromium.
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { build } from 'esbuild';
const bundle = await build({stdin:{contents:'export * from "./src/themes"; export * from "./src/charts/core/appearance";',resolveDir:fileURLToPath(new URL('../',import.meta.url))},bundle:true,write:false,format:'iife',globalName:'ThemeTest',platform:'browser'});
const context = vm.createContext({});
vm.runInContext(bundle.outputFiles[0].text,context);
const { ThemeTest }=context;
const html=ThemeTest.themeFrame('<html><head></head><body>Chart</body></html>');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script); // Catch escaping errors in the emitted script itself.
let handler;
const parent={};
const values=new Map(); const attributes=new Map();
const root={style:{setProperty:(key,value)=>values.set(key,value),removeProperty:key=>values.delete(key)},setAttribute:(key,value)=>attributes.set(key,value)};
vm.runInNewContext(script,{window:{addEventListener:(type,callback)=>{assert.equal(type,'message');handler=callback;}},parent,document:{documentElement:root},CSS:{supports:(property,value)=>property==='color' && /^(#[\da-f]{6}|var\(--evil\))$/i.test(value)}});
const send=(data,source=parent)=>handler({data,source});
const message={type:'myflames-appearance',theme:'dark',accents:{'--accent':'#123456'},surfaces:Object.fromEntries(ThemeTest.CHART_SURFACES.map(name=>[name,'#123456']))};
send(message,{});assert.equal(values.size,0,'non-parent messages refused');
send({...message,theme:'fire'});assert.equal(values.size,0,'frames accept only resolved bases');
send(message);assert.equal(attributes.get('data-theme'),'dark');assert.equal(root.style.colorScheme,'dark');
for(const name of ThemeTest.CHART_SURFACES)assert.equal(values.get(name),'#123456',name);
send({...message,surfaces:{'--mfc-canvas':'red; background:url(https://example.invalid)','--mfc-card':'var(--evil)','--evil':'#ffffff'}});
assert.equal(values.get('--mfc-canvas'),'#123456');assert.equal(values.get('--mfc-card'),'#123456');assert.equal(values.has('--evil'),false);
send({...message,surfaces:{'--mfc-card':''}});assert.equal(values.has('--mfc-card'),false,'missing host override restores frame base default');
for (const theme of ThemeTest.THEME_CHOICES) {
  for (const mediaDark of [true,false]) {
    const element={dataset:{theme:theme.id},ownerDocument:{defaultView:{matchMedia:()=>({matches:mediaDark})}}};
    assert.equal(ThemeTest.appliedThemeBase(element),theme.id==='system'?(mediaDark?'dark':'light'):theme.base);
  }
}
for(const id of [undefined,'unknown'])assert.equal(ThemeTest.appliedThemeBase({dataset:{theme:id},ownerDocument:{defaultView:{matchMedia:()=>({matches:true})}}}),'dark');
const stored=new Map();
context.localStorage={getItem:key=>stored.get(key)||null,setItem:(key,value)=>stored.set(key,value)};
context.document={documentElement:{dataset:{}}};
for(const id of ThemeTest.THEME_IDS){ThemeTest.applyTheme(id);assert.equal(ThemeTest.savedTheme(),id);assert.equal(context.document.documentElement.dataset.theme,id==='system'?undefined:id);}
context.localStorage.setItem=()=>{throw Error('Storage unavailable')};
ThemeTest.applyTheme('fire');assert.equal(ThemeTest.appliedThemeId(),'fire','root choice survives unavailable storage');
ThemeTest.applyTheme('unknown');assert.equal(ThemeTest.savedTheme(),'one-dark','invalid id cannot replace saved choice');
console.log('Fixed frame script, source isolation, invalid-color refusal, applied bases and storage passed.');
