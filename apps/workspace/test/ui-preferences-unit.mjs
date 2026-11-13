// Exercise the real client queue with controlled transport timing, no server.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
const bundled = await build({stdin:{contents:'export * from "./src/uiPreferences"; export * from "./src/themes";',resolveDir:fileURLToPath(new URL('../',import.meta.url))},bundle:true,write:false,format:'iife',globalName:'Preferences',platform:'browser'});
const defaults={theme:'system',sidebar_width:208,server_nav_collapsed:true,health_auto_production:-1,health_auto_development:-1,inspector_width:340,explain_format:'table',files_show_hidden:false,files_transfers_open:true,diagram_zoom_speed:100};
function harness(timers = {setTimeout,clearTimeout}) {
  const requests=[],style={},storage=new Map([['myflames.inspector.width','420'],['myflames.editor.explainFormat','json'],['myflames.files.showHidden','1'],['myflames.files.transfersOpen','0'],['myflames.serverNavCollapsed','false'],['myflames.health.auto.production','30'],['myflames.health.auto.development','0']]);
  const sandbox={console,...timers,URLSearchParams,AbortController,window:{location:{hash:'',pathname:'/',search:''},history:{replaceState(){}},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)}},document:{documentElement:{dataset:{theme:'dark'},style:{setProperty:(k,v)=>style[k]=v}}}};
  sandbox.localStorage=sandbox.window.localStorage;
  sandbox.fetch=async (url,options)=>{
    if (url==='/api/auth/session') return {ok:true,json:async()=>({token:'test-token'})};
    assert.equal(url,'/api/settings/ui');
    return new Promise(resolve=>requests.push({body:JSON.parse(options.body),reply:(preferences,ok=true)=>resolve({ok,status:ok?200:500,json:async()=>ok?{preferences}:{error:'failed'}})}));
  };
  vm.runInNewContext(bundled.outputFiles[0].text,sandbox);
  return {store:sandbox.Preferences,requests,style,storage,root:sandbox.document.documentElement};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
{
  let timeout,delay,cleared,done=false;
  const {store,requests,style,root}=harness({setTimeout:(fn,ms)=>{timeout=fn;delay=ms;return 1;},clearTimeout:id=>{cleared=id;}});
  const wait=store.loadUiPreferencesWithin().then(()=>{done=true;});
  await tick();
  assert.equal(delay,1500,'startup waits at most 1.5 seconds');
  assert.equal(done,false,'startup waits for preferences before the deadline');
  timeout(); await tick();
  assert.equal(done,true,'a stalled read cannot block the first render');
  await wait;
  assert.equal(cleared,1,'the bounded wait clears its timer');
  assert.equal(store.currentUiPreferences().inspector_width,420,'timeout keeps the current values');
  assert.equal(root.dataset.theme,'dark','timeout keeps the injected theme');
  let published;
  const stop=store.subscribeUiPreferences(value=>{published=value;});
  const late=store.loadUiPreferences();
  assert.equal(requests.length,1,'timeout does not cancel or duplicate the pending read');
  const answer={...defaults,theme:'nord',sidebar_width:280,inspector_width:500,explain_format:'tree',files_show_hidden:true,files_transfers_open:false,server_nav_collapsed:false,health_auto_production:60,health_auto_development:10};
  requests.shift().reply(answer); await late;
  assert.deepEqual(JSON.parse(JSON.stringify(published)),answer,'a listener mounted after timeout receives all late preferences');
  assert.equal(style['--sidebar-w'],'280px','late read updates the sidebar width');
  assert.equal(root.dataset.theme,'nord','the existing theme listener paints the late theme');
  stop();
}
{
  for (const ok of [true,false]) {
    let delay,cleared;
    const {store,requests}=harness({setTimeout:(_,ms)=>{delay=ms;return 2;},clearTimeout:id=>{cleared=id;}});
    const wait=store.loadUiPreferencesWithin(25);
    const checked=ok ? wait : assert.rejects(wait);
    await tick(); requests.shift().reply(defaults,ok); await checked;
    assert.equal(delay,25,'callers may choose a shorter deadline');
    assert.equal(cleared,2,'a prompt answer or error clears the timer');
    if (ok) assert.equal(store.currentUiPreferences().theme,'system','prompt reads apply before startup continues');
  }
}
console.log('UI preference bounded startup, late theme and width publication, prompt reads and errors passed.');
{
  const {store,requests,style}=harness();
  assert.equal(store.currentUiPreferences().theme,'dark');
  assert.equal(store.currentUiPreferences().inspector_width,420);
  assert.equal(store.currentUiPreferences().explain_format,'json');
  assert.equal(store.currentUiPreferences().files_show_hidden,true);
  assert.equal(store.currentUiPreferences().files_transfers_open,false);
  assert.equal(store.currentUiPreferences().server_nav_collapsed,false);
  assert.equal(store.currentUiPreferences().health_auto_production,30);
  assert.equal(store.currentUiPreferences().health_auto_development,0,'each legacy type keeps its own choice');
  const load=store.loadUiPreferences(); await tick(); requests.shift().reply(defaults); await load;
  assert.equal(store.currentUiPreferences().theme,'system','backend default wins over legacy theme');
  assert.equal(store.currentUiPreferences().inspector_width,340);
  assert.equal(store.currentUiPreferences().explain_format,'table');
  assert.equal(store.currentUiPreferences().files_show_hidden,false);
  assert.equal(store.currentUiPreferences().files_transfers_open,true);
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

{
  const {store,requests}=harness();
  const write=store.setUiPreference('inspector_width',560);
  let done=false;
  const flush=store.flushUiPreferences().then(()=>{done=true;});
  await tick(); assert.equal(done,false,'flush waits for a delayed write');
  const next=store.setUiPreference('explain_format','tree');
  requests.shift().reply({...defaults,inspector_width:560}); await write; await tick();
  assert.equal(done,false,'flush includes a write queued during the drain');
  requests.shift().reply({...defaults,inspector_width:560,explain_format:'tree'}); await next; await flush;
  assert.equal(done,true); assert.equal(store.currentUiPreferences().explain_format,'tree');
  for (const [key,value] of [['files_show_hidden',true],['files_transfers_open',false]]) {
    const saved=store.setUiPreference(key,value); await tick();
    assert.deepEqual(requests[0].body.set,{[key]:value});
    requests.shift().reply({...store.currentUiPreferences(),[key]:value}); await saved;
  }
}
{
  const {store,requests}=harness();
  const write=store.setUiPreference('explain_format','json'); await tick();
  const started=Date.now(); await store.flushUiPreferences(25);
  assert.ok(Date.now()-started>=20,'hung write waits for the timeout');
  assert.ok(Date.now()-started<1000,'hung write cannot block shutdown indefinitely');
  requests.shift().reply({...defaults,explain_format:'json'}); await write;
}
console.log('UI preference delayed drains, concurrent queued writes, hung-write timeout and additional stored choices passed.');

{
  const {store,storage}=harness();
  storage.set('myflames.inspector.width','560'); storage.set('myflames.editor.explainFormat','tree');
  assert.equal(store.currentUiPreferences().inspector_width,420,'legacy width is read only once');
  assert.equal(store.currentUiPreferences().explain_format,'json','legacy Explain format is read only once');
}

{
  const {store,requests}=harness();
  assert.equal(store.currentUiPreferences().diagram_zoom_speed,100,'Normal is the fallback');
  const load=store.loadUiPreferences(); await tick();
  requests.shift().reply({...defaults,diagram_zoom_speed:50}); await load;
  assert.equal(store.currentUiPreferences().diagram_zoom_speed,50,'stored speed is restored');
  const published=[];
  const stop=store.subscribeUiPreferences(value=>published.push(value.diagram_zoom_speed));
  const first=store.setUiPreference('diagram_zoom_speed',25);
  const last=store.setUiPreference('diagram_zoom_speed',200);
  assert.equal(store.currentUiPreferences().diagram_zoom_speed,200,'speed changes publish immediately');
  await tick(); assert.equal(requests.length,1);
  assert.deepEqual(requests[0].body.set,{diagram_zoom_speed:25});
  requests.shift().reply({...defaults,diagram_zoom_speed:25}); await first; await tick();
  assert.equal(store.currentUiPreferences().diagram_zoom_speed,200,'stale speed answer cannot undo the latest change');
  assert.deepEqual(requests[0].body.set,{diagram_zoom_speed:200});
  requests.shift().reply({...defaults,diagram_zoom_speed:200}); await last;
  assert.deepEqual(published,[25,200,200]);
  for (const speed of [25,50,100,150,200]) {
    const saved=store.setUiPreference('diagram_zoom_speed',speed); await tick();
    assert.deepEqual(requests[0].body.set,{diagram_zoom_speed:speed});
    requests.shift().reply({...defaults,diagram_zoom_speed:speed}); await saved;
    assert.equal(store.currentUiPreferences().diagram_zoom_speed,speed);
  }
  stop();
}
console.log('Diagram zoom speed: Normal fallback, stored restore, live subscribers and serialized persistence passed.');

// Execute the real wheel hook's effects against controlled DOM/RAF probes.
// Browser geometry is exercised separately by schema-diagram.mjs.
{
  const wheelBundle=await build({
    stdin:{contents:'export * from "./src/wheelZoom";',resolveDir:fileURLToPath(new URL('../',import.meta.url))},
    bundle:true,write:false,format:'iife',globalName:'WheelZoom',platform:'browser',
    plugins:[{name:'controlled-hooks',setup(builder){
      builder.onResolve({filter:/^react$/},()=>({path:'react',namespace:'controlled-hooks'}));
      builder.onLoad({filter:/.*/,namespace:'controlled-hooks'},()=>({contents:'export const {useRef,useEffect}=globalThis.__hooks;'}));
    }}],
  });
  const refs=[],effects=[],pending=[],frames=new Map();
  let refIndex=0,effectIndex=0,nextFrame=0,canceledFrames=0;
  const sandbox={
    __hooks:{
      useRef:initial=>refs[refIndex++] ||= {current:initial},
      useEffect:(callback,deps)=>{
        const index=effectIndex++,old=effects[index];
        if (!old || !deps || deps.some((value,i)=>value!==old.deps?.[i])) pending.push(()=>{
          old?.cleanup?.(); effects[index]={deps,cleanup:callback()};
        });
      },
    },
    requestAnimationFrame:callback=>{const id=++nextFrame;frames.set(id,callback);return id;},
    cancelAnimationFrame:id=>{canceledFrames++;frames.delete(id);},
  };
  vm.runInNewContext(wheelBundle.outputFiles[0].text,sandbox);
  const {wheelZoomStep,useWheelZoom}=sandbox.WheelZoom;
  const near=(value,expected)=>assert.ok(Math.abs(value-expected)<1e-12,`${value} vs ${expected}`);
  for (const [event,expected] of [
    [{deltaX:0,deltaY:20,deltaMode:0},-.02],
    [{deltaX:0,deltaY:-2,deltaMode:1},.032],
    [{deltaX:0,deltaY:.1,deltaMode:2},-.06],
    [{deltaX:0,deltaY:10000,deltaMode:0},-.1],
    [{deltaX:0,deltaY:-10000,deltaMode:0},.1],
  ]) for (const speed of [25,50,100,150,200]) near(wheelZoomStep(event,speed,600),expected*speed/100);
  for (const event of [{deltaX:0,deltaY:0},{deltaX:20,deltaY:0},{deltaX:30,deltaY:20},
    {deltaX:0,deltaY:.001},{deltaX:0,deltaY:NaN},{deltaX:0,deltaY:Infinity}]) {
    assert.equal(wheelZoomStep({...event,deltaMode:0},100,600),null);
  }
  const target=()=>({clientHeight:600,adds:0,removes:0,listener:null,
    addEventListener(type,listener,options){assert.equal(type,'wheel');assert.equal(options.passive,false);this.listener=listener;this.adds++;},
    removeEventListener(type,listener){assert.equal(type,'wheel');assert.equal(listener,this.listener);this.listener=null;this.removes++;},
    fire(event){let prevented=false;this.listener({...event,deltaX:event.deltaX??0,deltaY:event.deltaY??0,deltaMode:event.deltaMode??0,
      clientX:event.clientX??40,clientY:event.clientY??60,preventDefault(){prevented=true;}});return prevented;},
  });
  const ref={current:null},applied=[],element=target();
  const render=(node,speed,apply=(...args)=>applied.push(args))=>{
    ref.current=node;refIndex=effectIndex=0;useWheelZoom(ref,apply,speed);pending.splice(0).forEach(effect=>effect());
  };
  const flush=()=>{const callbacks=[...frames.values()];frames.clear();callbacks.forEach(callback=>callback());};
  render(null,100);render(element,100);
  assert.equal(element.adds,1,'conditional canvas attaches after mounting');
  for (let i=0;i<40;i++) assert.equal(element.fire({deltaY:.5,ctrlKey:true,clientX:i,clientY:i+1}),true);
  assert.equal(frames.size,1,'forty trackpad events share one frame');
  assert.equal(applied.length,0,'zoom waits for the frame');flush();
  near(applied[0][0],Math.exp(-.02));assert.deepEqual(applied[0].slice(1),[39,40],'last pointer is used');
  for (const event of [{deltaX:20},{deltaX:30,deltaY:20},{deltaY:0}]) assert.equal(element.fire(event),false);
  assert.equal(frames.size,0,'ignored events do not schedule work');
  const latest=[];render(element,200,(...args)=>latest.push(args));
  assert.equal(element.adds,1,'ordinary renders retain the listener');
  element.fire({deltaY:20});flush();near(latest[0][0],Math.exp(-.04));
  assert.equal(applied.length,1,'the callback is current');
  element.fire({deltaY:50});render(null,200);
  assert.equal(element.removes,1);assert.equal(frames.size,0,'detach cancels pending work');
  const replacement=target();render(replacement,50);
  replacement.fire({deltaY:-2,deltaMode:1});flush();near(applied.at(-1)[0],Math.exp(.016));
  replacement.fire({deltaY:20});effects.forEach(effect=>effect.cleanup?.());
  assert.equal(replacement.removes,1);assert.equal(frames.size,0,'unmount cancels the pending frame');
  assert.equal(canceledFrames,2);
}
console.log('Wheel normalization, caps, speed, trackpad/pinch batching, current callbacks, conditional mounts and unmount cancellation passed.');
