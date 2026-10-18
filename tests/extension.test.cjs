const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');
function setup(response, collect = {ok:true,tweets:[{tweet_id:'99'}]}) {
  const removed=[], updated=[]; let created=0;
  const stored = {};
  const context = vm.createContext({setInterval(){}, URL, AbortSignal, console, setTimeout, clearTimeout,
    fetch: async()=>({ok:true,json:async()=>({ok:true,account:'x:fixture'})}),
    chrome: { storage: {sync: {get:async d=>d}, local: {get:async d=>({...d,...structuredClone(stored)}),set:async d=>Object.assign(stored,structuredClone(d))}},
      tabs: {query:async()=>[{id:1,active:true,url:response.url}],create:async()=>({id:++created}),sendMessage:async(id,m)=>m.type==='ping'?response:{account:'x:fixture',...collect},
        remove:async id=>removed.push(id),update:async id=>updated.push(id)},
      alarms:{onAlarm:{addListener(){}},create(){}},
      runtime:{sendMessage:async()=>({ok:true}),onMessage:{addListener(){}},onInstalled:{addListener(){}},onStartup:{addListener(){}}}}
  });
  vm.runInContext(source,context);
  return {context,removed,updated,stored,get created(){return created}};
}
test('login redirect stays open and requests login',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/flow/login'});
  await assert.rejects(vm.runInContext('runSync({})',h.context),/登录/);
  assert.deepEqual(h.removed,[]); assert.deepEqual(h.updated,[1]);
});
test('successful collection preserves the existing tab',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'});
  await vm.runInContext('runSync({})',h.context);
  assert.deepEqual(h.removed,[]);
});
test('collection errors keep tab open',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'},{ok:false,error:'login needed'});
  await assert.rejects(vm.runInContext('runSync({})',h.context),/login needed/);
  assert.deepEqual(h.removed,[]);
});
test('overlapping manual and alarm sync share a single tab',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'});
  await vm.runInContext('Promise.all([runSync({}),runSync({})])',h.context);
  assert.equal(h.created,0);
});
test('content script answers readiness ping on login page',()=>{
  let listener;
  const c=vm.createContext({setInterval(){},window:{addEventListener(){}},document:{addEventListener(){},readyState:'complete',querySelector:()=>null},location:{href:'https://x.com/i/flow/login'},chrome:{runtime:{sendMessage:async()=>({ok:true}),onMessage:{addListener(f){listener=f}}}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension/content.js'),'utf8'),c);
  let result;listener({type:'ping'},{},r=>result=r);
  assert.equal(result.ok,true);assert.equal(result.ready,true);
});
test('queue flush cannot erase captures arriving during upload',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'});
  let stored=[{tweet_id:'1'}], release;
  h.context.chrome.storage.local.get=async()=>({xbm_pending_queue:structuredClone(stored)});
  h.context.chrome.storage.local.set=async d=>{stored=structuredClone(d.xbm_pending_queue)};
  h.context.fetch=async()=>{await new Promise(r=>{release=r});return {ok:true,json:async()=>({ok:true})}};
  const flushing=vm.runInContext("flushQueue('http://nas')",h.context);
  await new Promise(setImmediate);
  const capturing=vm.runInContext("enqueue([{tweet_id:'2'}])",h.context);
  release();await flushing;await capturing;
  assert.equal(stored.length,1);assert.equal(stored[0].tweet_id,'2');
});

test('NAS outage keeps captures durable and preserves existing tab', async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'}, {ok:true,tweets:[{tweet_id:'10'}]});
  h.context.fetch=async()=>{throw new Error('offline')};
  const result=await vm.runInContext('runSync({})',h.context);
  assert.equal(result.pendingDelivery,true);
  assert.equal(h.stored.xbm_pending_queue[0].tweet_id,'10');
  assert.deepEqual(h.removed,[]);
});
test('login failure pauses subsequent automatic collection',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/flow/login'});
  await assert.rejects(vm.runInContext('runSync({automatic:true})',h.context));
  await vm.runInContext('runSync({automatic:true})',h.context);
  assert.equal(h.created,0);
  assert.equal(h.stored.xbm_sync_status.state,'needs_login');
});
test('partial upload acknowledges batches and preserves unconfirmed data',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'});
  h.stored.xbm_pending_queue=Array.from({length:150},(_,i)=>({tweet_id:String(i+1)}));
  let sent=0;
  h.context.fetch=async()=>{if(++sent===2)throw new Error('offline');return {ok:true,json:async()=>({ok:true})}};
  await assert.rejects(vm.runInContext("flushQueue('http://nas')",h.context));
  assert.equal(h.stored.xbm_pending_queue.length,50);
  assert.equal(h.stored.xbm_pending_queue[0].tweet_id,'101');
});
test('HTTP 200 without receipt cannot erase browser outbox',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'});
  h.stored.xbm_pending_queue=[{tweet_id:'20'}];
  h.context.fetch=async()=>({ok:true,json:async()=>({error:'not committed'})});
  await assert.rejects(vm.runInContext("flushQueue('http://nas')",h.context));
  assert.equal(h.stored.xbm_pending_queue.length,1);
});
test('storage failure leaves collection tab available for retry',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'}, {ok:true,tweets:[{tweet_id:'1'}]});
  const original=h.context.chrome.storage.local.set;
  h.context.chrome.storage.local.set=async d=>{if(d.xbm_pending_queue)throw new Error('quota');return original(d)};
  await assert.rejects(vm.runInContext('runSync({})',h.context),/quota/);
  assert.deepEqual(h.removed,[]);
});
test('worker restart can deliver persisted outbox without opening X',async()=>{
  const before=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'});
  await vm.runInContext("enqueue([{tweet_id:'9'}])",before.context);
  const after=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'});
  Object.assign(after.stored,structuredClone(before.stored));
  await vm.runInContext("flushQueue('http://nas')",after.context);
  assert.equal(after.stored.xbm_pending_queue.length,0);
  assert.equal(after.created,0);
});
test('outbox keeps more than 2000 bookmarks instead of truncating',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'});
  h.context.captured=Array.from({length:2001},(_,i)=>({tweet_id:String(i+1)}));
  await vm.runInContext('enqueue(captured)',h.context);
  assert.equal(h.stored.xbm_pending_queue.length,2001);
  assert.equal(h.stored.xbm_pending_queue[0].tweet_id,'1');
});
function pageProbe(pathname, selectedTab) {
  let listener;
  const c=vm.createContext({setInterval(){},window:{addEventListener(){}},
    document:{addEventListener(){},readyState:'complete',querySelector:selector=>selector.includes('Profile_Link')?null:(selectedTab===null?null:{textContent:selectedTab})},
    location:{pathname,href:'https://x.com'+pathname},
    chrome:{runtime:{sendMessage:async()=>({ok:true}),onMessage:{addListener(f){listener=f}}}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension/content.js'),'utf8'),c);
  let result;listener({type:'ping'},{},r=>result=r);return result;
}
test('history bookmarks tab is a collection page',()=>{
  assert.equal(pageProbe('/i/history','书签').pageType,'bookmarks');
  assert.equal(pageProbe('/i/history','Bookmarks').pageType,'bookmarks');
});
test('history likes tab is never a collection page',()=>{
  assert.equal(pageProbe('/i/history','喜欢').pageType,'history_other');
});
test('history waits for selected tab to render',()=>{
  assert.equal(pageProbe('/i/history',null).ready,false);
});
test('background accepts observed history bookmarks route',async()=>{
  const h=setup({ok:true,ready:true,pageType:'bookmarks',url:'https://x.com/i/history'});
  await vm.runInContext('runSync({})',h.context);assert.deepEqual(h.removed,[]);
});
test('background rejects history likes without marking login expired',async()=>{
  const h=setup({ok:true,ready:true,pageType:'history_other',url:'https://x.com/i/history'});
  await assert.rejects(vm.runInContext('runSync({})',h.context),/书签/);
  assert.equal(h.stored.xbm_sync_status.state,'error');assert.deepEqual(h.removed,[]);
});
test('navigation channel failure reconnects and records recovery',async()=>{
  const ready={ok:true,ready:true,pageType:'bookmarks',url:'https://x.com/i/history'};
  const h=setup(ready);let attempts=0;
  h.context.setTimeout=(fn)=>setImmediate(fn);
  h.context.chrome.tabs.sendMessage=async(id,msg)=>{
    if(msg.type==='ping')return ready;
    if(++attempts===1)throw new Error('message channel closed');
    return {ok:true,account:'x:fixture',tweets:[{tweet_id:'99'}]};
  };
  await vm.runInContext('runSync({})',h.context);
  assert.equal(attempts,2);
  assert.ok(h.stored.xbm_task_history.some(e=>(e.error||'').includes('重新连接')));
});
test('task history survives status changes and keeps latest 100 records',async()=>{
  const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'});
  await vm.runInContext("Promise.all(Array.from({length:105}, (_,i)=>saveStatus({state:'idle',count:i})))",h.context);
  assert.equal(h.stored.xbm_task_history.length,100);
  assert.equal(h.stored.xbm_task_history[99].count,104);
});

test('zero capture is not a successful NAS submission and preserves page',async()=>{
 const h=setup({ok:true,ready:true,pageType:'bookmarks',url:'https://x.com/i/history'},{ok:true,tweets:[],diagnostics:{responses:1,parsed:0}});
 const result=await vm.runInContext('runSync({})',h.context);
 assert.equal(result.noCapture,true);assert.equal(h.stored.xbm_sync_status.state,'no_capture');
 assert.deepEqual(h.removed,[]);
});
async function interceptFixture(data,operation='Bookmarks') {
 const messages=[],listeners={};
 function XHR(){};XHR.prototype.open=function(){};XHR.prototype.send=function(){};
 const win={fetch:async()=>({clone:()=>({json:async()=>data})}),addEventListener:(type,fn)=>{listeners[type]=fn},postMessage:msg=>messages.push(msg)};
 const c=vm.createContext({setInterval(){},document:{querySelector:()=>({getAttribute:()=>'/fixture'})},window:win,XMLHttpRequest:XHR});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension/injected.js'),'utf8'),c);
 await win.fetch('https://x.com/i/api/graphql/fake/'+operation);await new Promise(setImmediate);
 listeners.message({source:win,data:{__xbm:true,type:'get-buffer'}});
 return messages.find(m=>m.type==='buffer');
}
test('parser handles nested timeline modules and rest_id',async()=>{
 const result=await interceptFixture({data:{user:{result:{timeline_v2:{timeline:{instructions:[{moduleItems:[{item:{itemContent:{tweet_results:{result:{rest_id:'123',legacy:{full_text:'Synthetic fixture'}}}}}}]}]}}}}}});
 assert.equal(result.tweets.length,1);assert.equal(result.tweets[0].tweet_id,'123');
 assert.equal(result.diagnostics.parsed,1);
});
test('parser diagnoses changed schema without claiming success',async()=>{
 const result=await interceptFixture({data:{new_shape:{}}});
 assert.equal(result.tweets.length,0);assert.equal(result.diagnostics.responses,1);
 assert.equal(result.diagnostics.dataKeys[0],'new_shape');
});
test('unrelated timeline operations are counted but never archived',async()=>{
 const result=await interceptFixture({data:{timeline:{instructions:[{tweet_results:{result:{legacy:{id_str:'123'}}}}]}}},'Likes');
 assert.equal(result.tweets.length,0);assert.equal(result.diagnostics.graphqlResponses,1);
 assert.equal(result.diagnostics.responses,0);assert.equal(result.diagnostics.operations[0],'Likes');
});

test('manual sync without bookmarks requests a page without creating or navigating tabs',async()=>{
 const h=setup({});h.context.chrome.tabs.query=async()=>[];
 await assert.rejects(vm.runInContext('runSync({})',h.context),/请先打开 X 书签页/);
 assert.equal(h.created,0);assert.deepEqual(h.updated,[]);assert.deepEqual(h.removed,[]);
});
test('automatic sync without bookmarks waits while delivering persisted outbox',async()=>{
 const h=setup({});h.context.chrome.tabs.query=async()=>[];
 h.stored.xbm_pending_queue=[{tweet_id:'pending'}];
 const r=await vm.runInContext('runSync({automatic:true})',h.context);
 assert.equal(r.waitingBrowser,true);assert.equal(h.stored.xbm_sync_status.state,'waiting_browser');
 assert.equal(h.stored.xbm_pending_queue.length,0);assert.equal(h.created,0);
});
test('prefers active bookmark tab and skips active likes',async()=>{
 for(const activeType of ['bookmarks','history_other']){
  const h=setup({});const captures=[];
  h.context.chrome.tabs.query=async()=>[{id:2,active:false,lastAccessed:200},{id:1,active:true,lastAccessed:100}];
  h.context.chrome.tabs.sendMessage=async(id,m)=>{
   if(m.type==='ping')return {ok:true,ready:true,pageType:id===1?activeType:'bookmarks',url:'https://x.com/i/history'};
   captures.push(id);return {ok:true,account:'x:fixture',tweets:[{tweet_id:'99'}]};
  };
  await vm.runInContext('runSync({})',h.context);
  assert.deepEqual(captures,[activeType==='bookmarks'?1:2]);
  assert.equal(h.created,0);assert.deepEqual(h.removed,[]);
 }
});
test('stale content script asks for refresh without opening replacement tabs',async()=>{
 const h=setup({url:'https://x.com/i/bookmarks'});
 h.context.chrome.tabs.sendMessage=async()=>{throw new Error('Receiving end does not exist');};
 await assert.rejects(vm.runInContext('runSync({})',h.context),/刷新/);
 assert.equal(h.created,0);assert.deepEqual(h.updated,[]);
});

test('outbox separates identical tweet IDs and preserves capture account after switching',async()=>{
 const h=setup({});const deliveries=[];
 h.context.fetch=async(url,options)=>{const payload=JSON.parse(options.body);deliveries.push(payload);return {ok:true,json:async()=>({ok:true,account:payload.account})};};
 await vm.runInContext("enqueue([{tweet_id:'1'}], 'x:alpha')",h.context);
 await vm.runInContext("enqueue([{tweet_id:'1'}], 'x:beta')",h.context);
 assert.equal(h.stored.xbm_pending_queue.length,2);
 await vm.runInContext("flushQueue('http://nas')",h.context);
 assert.deepEqual(deliveries.map(p=>p.account),['x:alpha','x:beta']);
 assert.ok(deliveries.every(p=>p.tweets[0].tweet_id==='1'&&!('_xbm_account' in p.tweets[0])));
});
test('legacy queued captures remain unassigned instead of inheriting current login',async()=>{
 const h=setup({});h.stored.xbm_pending_queue=[{tweet_id:'legacy'}];const accounts=[];
 h.context.fetch=async(url,options)=>{const payload=JSON.parse(options.body);accounts.push(payload.account);return {ok:true,json:async()=>({ok:true,account:payload.account})};};
 await vm.runInContext("enqueue([{tweet_id:'new'}], 'x:alpha')",h.context);
 await vm.runInContext("flushQueue('http://nas')",h.context);
 assert.deepEqual(accounts,['','x:alpha']);
});
test('missing capture identity stops manual sync without assigning data to legacy',async()=>{
 const h=setup({ok:true,ready:true,url:'https://x.com/i/bookmarks'},{ok:true,account:'',tweets:[{tweet_id:'99'}]});
 await assert.rejects(vm.runInContext('runSync({})',h.context),/无法识别/);
 assert.equal((h.stored.xbm_pending_queue||[]).length,0);
});
test('account-aware delivery rejects an old NAS receipt and keeps pending data',async()=>{
 const h=setup({});h.context.fetch=async()=>({ok:true,json:async()=>({ok:true})});
 await vm.runInContext("enqueue([{tweet_id:'1'}], 'x:alpha')",h.context);
 await assert.rejects(vm.runInContext("flushQueue('http://nas')",h.context),/账户确认不匹配/);
 assert.equal(h.stored.xbm_pending_queue.length,1);
});
test('in-flight responses from previous login are discarded',async()=>{
 let handle='alpha',resolveResponse;const messages=[],listeners={};
 const payload={data:{timeline:{instructions:[{tweet_results:{result:{legacy:{id_str:'123'}}}}]}}};
 function XHR(){};XHR.prototype.open=function(){};XHR.prototype.send=function(){};
 const win={fetch:()=>new Promise(resolve=>resolveResponse=resolve),addEventListener:(t,fn)=>listeners[t]=fn,postMessage:m=>messages.push(m)};
 const c=vm.createContext({setInterval(){},window:win,XMLHttpRequest:XHR,document:{querySelector:()=>({getAttribute:()=>'/'+handle})}});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension/injected.js'),'utf8'),c);
 const request=win.fetch('https://x.com/i/api/graphql/fake/Bookmarks');handle='beta';
 resolveResponse({clone:()=>({json:async()=>payload})});await request;await new Promise(setImmediate);
 listeners.message({source:win,data:{__xbm:true,type:'get-buffer'}});
 assert.equal(messages.find(m=>m.type==='buffer').tweets.length,0);
 assert.ok(!messages.some(m=>m.type==='tweets'));
});
test('capture owner comes from logged-in profile entry rather than tweet author',async()=>{
 const result=await interceptFixture({data:{timeline:{instructions:[{tweet_results:{result:{legacy:{id_str:'123'},core:{user_results:{result:{legacy:{name:'Other author',screen_name:'other_author'}}}}}}}]}}});
 assert.equal(result.account,'x:fixture');assert.equal(result.tweets[0].screen_name,'other_author');
});
test('content readiness reports normalized logged-in profile handle',()=>{
 let listener;const c=vm.createContext({setInterval(){},window:{addEventListener(){}},document:{addEventListener(){},readyState:'complete',querySelector:selector=>selector.includes('Profile_Link')?{getAttribute:()=>'/Login_Account'}:{textContent:'Bookmarks'}},location:{href:'https://x.com/i/history',pathname:'/i/history'},chrome:{runtime:{sendMessage:async()=>({ok:true}),onMessage:{addListener:f=>listener=f}}}});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension/content.js'),'utf8'),c);
 let result;listener({type:'ping'},{},r=>result=r);assert.equal(result.account,'x:login_account');
});

test('account observation reports identity without collecting bookmarks and persists before network failure',async()=>{
 const h=setup({});h.context.fetch=async()=>{throw new Error('offline');};
 await assert.rejects(vm.runInContext("reportBrowserAccount('x:fixture', 123000)",h.context),/offline/);
 assert.equal(h.stored.xbm_current_account,'x:fixture');assert.equal(h.stored.xbm_account_observed_at,123000);
});
test('settings page reports login identity and detects an account switch without opening bookmarks',async()=>{
 let handle='alpha',tick;const messages=[];
 const c=vm.createContext({Date,setInterval:fn=>tick=fn,window:{addEventListener(){}},document:{addEventListener(){},querySelector:()=>({getAttribute:()=>'/'+handle})},location:{pathname:'/settings/account'},chrome:{runtime:{sendMessage:async msg=>{messages.push(msg);return {ok:true};},onMessage:{addListener(){}}}}});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension/content.js'),'utf8'),c);
 await new Promise(setImmediate);handle='beta';tick();await new Promise(setImmediate);
 assert.deepEqual(messages.map(m=>m.type),['account-observed','account-observed']);
 assert.deepEqual(messages.map(m=>m.account),['x:alpha','x:beta']);
});

function collectionHarness({grow=true,end=false,stop=false}={}) {
  let listener,scrolls=0,reads=0; const handlers=[]; const progress=[];
  const win={addEventListener:(kind,fn)=>{if(kind==='message')handlers.push(fn)},removeEventListener:(kind,fn)=>{const i=handlers.indexOf(fn);if(i>=0)handlers.splice(i,1)},scrollTo:()=>scrolls++,
    postMessage:msg=>{if(msg.type==='get-buffer')setImmediate(()=>{reads++;const count=grow?reads:1;const data={__xbm:true,type:'buffer',account:'x:fixture',tweets:Array.from({length:count},(_,i)=>({tweet_id:String(i+1)})),diagnostics:{ended:end}};handlers.slice().forEach(fn=>fn({source:win,data}));});}};
  const c=vm.createContext({window:win,setTimeout:(fn,ms)=>ms===3000?setTimeout(fn,ms):setImmediate(fn),clearTimeout,setInterval(){},console,
    document:{addEventListener(){},body:{scrollHeight:1000},documentElement:{scrollHeight:1000},readyState:'complete',querySelector:()=>({getAttribute:()=>'/fixture'})},
    location:{pathname:'/i/bookmarks',href:'https://x.com/i/bookmarks'},
    chrome:{runtime:{onMessage:{addListener(fn){listener=fn}},sendMessage:async msg=>{if(msg.type==='collect-progress'){progress.push(msg);if(stop)listener({type:'stop-collect'},{},()=>{});}return {ok:true}}}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../extension/content.js'),'utf8'),c);
  return {progress,get scrolls(){return scrolls},run:()=>new Promise(resolve=>listener({type:'collect',full:true,maxScrolls:15},{},resolve))};
}
test('virtualized constant-height page continues while captures increase',async()=>{
 const h=collectionHarness();const result=await h.run();assert.equal(result.ok,true);assert.equal(result.reason,'limit');assert.equal(result.scrolls,15);assert.equal(h.progress.length,15);
});
test('no new data is stalled, not claimed complete',async()=>{
 const h=collectionHarness({grow:false});const result=await h.run();assert.equal(result.reason,'stalled');assert.ok(h.progress.length>=10);
});
test('explicit timeline end and cancellation produce distinct outcomes',async()=>{
 assert.equal((await collectionHarness({end:true}).run()).reason,'end');
 assert.equal((await collectionHarness({stop:true}).run()).reason,'stopped');
});
