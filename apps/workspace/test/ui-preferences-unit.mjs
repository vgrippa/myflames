// Exercise the real client queue with controlled transport timing, no server.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
const bundled = await build({entryPoints:[fileURLToPath(new URL('../src/uiPreferences.ts',import.meta.url))],bundle:true,write:false,format:'iife',globalName:'Preferences',platform:'browser'});
const defaults={theme:'system',sidebar_width:208,server_nav_collapsed:true,health_auto_production:-1,health_auto_development:-1};
function harness() {
  const requests=[],style={},storage=new Map([['myflames.serverNavCollapsed','false'],['myflames.health.auto.production','30'],['myflames.health.auto.development','0']]);
  const sandbox={console,URLSearchParams,AbortController,window:{location:{hash:'',pathname:'/',search:''},history:{replaceState(){}},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)}},document:{documentElement:{dataset:{theme:'dark'},style:{setProperty:(k,v)=>style[k]=v}}}};
  sandbox.localStorage=sandbox.window.localStorage;
  sandbox.fetch=async (url,options)=>{
    if (url==='/api/auth/session') return {ok:true,json:async()=>({token:'test-token'})};
    assert.equal(url,'/api/settings/ui');
    return new Promise(resolve=>requests.push({body:JSON.parse(options.body),reply:(preferences,ok=true)=>resolve({ok,status:ok?200:500,json:async()=>ok?{preferences}:{error:'failed'}})}));
  };
  vm.runInNewContext(bundled.outputFiles[0].text,sandbox);
  return {store:sandbox.Preferences,requests,style};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
{
  const {store,requests,style}=harness();
  assert.equal(store.currentUiPreferences().theme,'dark');
  assert.equal(store.currentUiPreferences().server_nav_collapsed,false);
  assert.equal(store.currentUiPreferences().health_auto_production,30);
  assert.equal(store.currentUiPreferences().health_auto_development,0,'each legacy type keeps its own choice');
  const load=store.loadUiPreferences(); await tick(); requests.shift().reply(defaults); await load;
  assert.equal(store.currentUiPreferences().theme,'system','backend default wins over legacy theme');
  assert.equal(store.currentUiPreferences().health_auto_production,-1);
  assert.equal(store.currentUiPreferences().health_auto_development,-1);
  assert.equal(style['--sidebar-w'],'208px');
  const first=store.setUiPreference('theme','dark');
  const last=store.setUiPreference('theme','nord');
  assert.equal(store.currentUiPreferences().theme,'nord','latest choice paints immediately');
  await tick(); assert.equal(requests.length,1,'one write at a time');
  assert.equal(requests[0].body.set.theme,'dark');
  requests.shift().reply({...defaults,theme:'dark'}); await first; await tick();
  assert.equal(store.currentUiPreferences().theme,'nord','older answer cannot replace latest choice');
  requests.shift().reply({...defaults,theme:'nord'}); await last;
  assert.equal(store.currentUiPreferences().theme,'nord');
  const failed=store.setUiPreference('sidebar_width',280); const caught=assert.rejects(failed);
  await tick(); requests.shift().reply(null,false); await caught; await tick();
  assert.deepEqual(requests[0].body,{},'failed latest write re-reads');
  requests.shift().reply({...defaults,theme:'nord',sidebar_width:256}); await tick();
  assert.equal(store.currentUiPreferences().sidebar_width,256);
  assert.equal(style['--sidebar-w'],'256px','readback restores authoritative width');
  assert.equal(store.clampSidebarWidth(-100),208); assert.equal(store.clampSidebarWidth(9999),320);
}
{
  const {store,requests}=harness();
  const load=store.loadUiPreferences(); await tick();
  const changed=store.setUiPreference('health_auto_production',60); await tick();
  requests.shift().reply(defaults); await load;
  assert.equal(store.currentUiPreferences().health_auto_production,60,'late initial read cannot undo an interaction');
  requests.shift().reply({...defaults,health_auto_production:60}); await changed;
}
{
  const {store,requests,style}=harness();
  const load=store.loadUiPreferences(); const caught=assert.rejects(load);
  await tick(); requests.shift().reply(null,false); await caught;
  assert.equal(style['--sidebar-w'],'208px','failed startup read still reserves fallback width');
}
console.log('UI preferences: backend precedence, optimistic choices, serial latest writes, stale reads and failed-write readback passed.');
