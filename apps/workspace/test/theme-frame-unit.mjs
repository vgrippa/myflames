// Runtime checks of the fixed frame script when Chromium cannot launch.
// Native CSS.supports behavior is covered separately by themes.mjs in Chromium.
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { build } from 'esbuild';
const bundle = await build({stdin:{contents:'export * from "./src/themes"; export * from "./src/charts/core/appearance";',resolveDir:fileURLToPath(new URL('../',import.meta.url))},bundle:true,write:false,format:'iife',globalName:'ThemeTest',platform:'browser'});
const stored=new Map(), requests=[];
const defaults={theme:'system',sidebar_width:208,server_nav_collapsed:true,health_auto_production:-1,health_auto_development:-1};
let backend={...defaults};
const storage={getItem:key=>stored.get(key)||null,setItem:(key,value)=>stored.set(key,value)};
const context = vm.createContext({
  URLSearchParams, localStorage:storage,
  window:{location:{hash:''},localStorage:storage},
  document:{documentElement:{dataset:{},style:{setProperty(){}}}},
  fetch:async (url,options)=>{
    if(url==='/api/auth/session')return {ok:true,json:async()=>({token:'unit-token'})};
    assert.equal(url,'/api/settings/ui');
    const body=JSON.parse(options.body); requests.push(body);
    backend={...backend,...body.set};
    return {ok:true,json:async()=>({preferences:{...backend}})};
  },
});
vm.runInContext(bundle.outputFiles[0].text,context);
const { ThemeTest }=context;
const html=ThemeTest.themeFrame('<html><head></head><body>Chart</body></html>');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script); // Catch escaping errors in the emitted script itself.
let handler;
const parent={};
const values=new Map(); const attributes=new Map();
const root={style:{setProperty:(key,value)=>values.set(key,value),removeProperty:key=>values.delete(key)},setAttribute:(key,value)=>attributes.set(key,value)};
vm.runInNewContext(script,{window:{addEventListener:(type,callback)=>{assert.equal(type,'message');handler=callback;}},parent,document:{documentElement:root},CSS:{supports:(property,value)=>property==='color' && value!=='rgb(invalid)'}});
const send=(data,source=parent)=>handler({data,source});
const message={type:'myflames-appearance',theme:'dark',accents:{'--accent':'#123456'},surfaces:Object.fromEntries(ThemeTest.CHART_SURFACES.map(name=>[name,'#123456']))};
send(message,{});assert.equal(values.size,0,'non-parent messages refused');
send({...message,theme:'fire'});assert.equal(values.size,0,'frames accept only resolved bases');
send(message);assert.equal(attributes.get('data-theme'),'dark');assert.equal(root.style.colorScheme,'dark');
for(const name of ThemeTest.CHART_SURFACES)assert.equal(values.get(name),'#123456',name);
send({...message,surfaces:{'--mfc-canvas':'red; background:url(https://example.invalid)','--mfc-card':'var(--evil)','--evil':'#ffffff'}});
assert.equal(values.get('--mfc-canvas'),'#123456');assert.equal(values.get('--mfc-card'),'#123456');assert.equal(values.has('--evil'),false);
const abuse=[String.raw`v\61r(--mfc-card)`,String.raw`\76 ar(--mfc-card)`,String.raw`rgb(from v\61r(--x) r g b)`,'attr(data-x type(<color>))','env(x, red)','if(media(width > 1px): red; else: blue)','inherit','initial','unset','revert',17,{},[],'','x'.repeat(200000),'rgb(from red r g b)','rgb(var(--x))','rgb(invalid)'];
for(const value of abuse){
  send({...message,accents:{'--accent':value},surfaces:{'--mfc-card':value}});
  assert.equal(values.get('--accent'),'#123456');assert.equal(values.get('--mfc-card'),'#123456');
}
// All allowed outer functions and hex lengths reach setProperty. The fake
// CSS.supports is deliberately permissive so it cannot hide grammar holes.
for(const value of ['#123','#1234','#123456','#12345678','rgb(1 2 3)','rgba(1,2,3,.5)','hsl(10 20% 30%)','hsla(10,20%,30%,.5)','hwb(10 20% 30%)','lab(50% 1 2)','lch(50% 1 2)','oklab(.5 .1 .2)','oklch(.5 .1 20)','color(display-p3 1 0 0)','color-mix(in srgb, rgb(1 2 3), blue)']) {
  send({...message,accents:{'--focus':value},surfaces:{'--mfc-focus':value}});
  assert.equal(values.get('--focus'),value);assert.equal(values.get('--mfc-focus'),value);
}
for (const theme of ThemeTest.THEME_CHOICES) {
  for (const mediaDark of [true,false]) {
    const element={dataset:{theme:theme.id},ownerDocument:{defaultView:{matchMedia:()=>({matches:mediaDark})}}};
    assert.equal(ThemeTest.appliedThemeBase(element),theme.id==='system'?(mediaDark?'dark':'light'):theme.base);
  }
}
for(const id of [undefined,'unknown'])assert.equal(ThemeTest.appliedThemeBase({dataset:{theme:id},ownerDocument:{defaultView:{matchMedia:()=>({matches:true})}}}),'dark');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
for(const id of ThemeTest.THEME_IDS){
  ThemeTest.applyTheme(id);await tick();
  assert.equal(ThemeTest.savedTheme(),id);assert.equal(backend.theme,id);
  assert.equal(context.document.documentElement.dataset.theme,id==='system'?undefined:id);
  assert.equal(requests.at(-1).set.theme,id,'theme persists through the backend');
}
assert.equal(stored.has('myflames.theme'),false,'theme writes never use localStorage');
context.localStorage.setItem=()=>{throw Error('Storage unavailable')};
ThemeTest.applyTheme('fire');await tick();
assert.equal(ThemeTest.appliedThemeId(),'fire','backend choice survives unavailable browser storage');
const writes=requests.length;
ThemeTest.applyTheme('unknown');await tick();
assert.equal(ThemeTest.savedTheme(),'fire','invalid id cannot replace saved choice');
assert.equal(requests.length,writes,'invalid id cannot reach the backend');
// Import the real Vite plugin's exported validator, without starting Vite.
const config=await build({entryPoints:[fileURLToPath(new URL('../vite.config.ts',import.meta.url))],bundle:true,write:false,format:'cjs',platform:'node',packages:'external'});
const module={exports:{}};
vm.runInNewContext(config.outputFiles[0].text,{module,exports:module.exports,require:createRequire(new URL('../vite.config.ts',import.meta.url))});
const {validateThemeIds}=module.exports;
validateThemeIds([...ThemeTest.THEME_IDS]);
for(const bad of ['Dark','dark_','dark--blue','dark2','dark\n',String.raw`dark\x`,'$&',"dark</script>"]){
  assert.throws(()=>validateThemeIds(['dark',bad]),/Theme ids/);
}
console.log('Fixed frame grammar, source isolation, applied bases, backend persistence and build id validation passed.');
