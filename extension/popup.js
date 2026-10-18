const $ = id => document.getElementById(id);
async function start(full) {
  $('status').textContent = '正在连接已有书签页…';
  try {
    const result = await chrome.runtime.sendMessage({type:'manual-sync',full});
    if (!result || !result.ok) throw new Error(result && result.error || '采集连接中断，请刷新 X 页面');
    await showPersistentStatus();
  } catch(e) { $('status').textContent = e.message; }
}
$('sync').onclick = () => start(false);
$('full').onclick = () => start(true);
$('stop').onclick = async () => { await chrome.runtime.sendMessage({type:'stop-sync'}); $('status').textContent='正在停止，已捕获内容继续补传…'; };
$('options').onclick = () => chrome.runtime.openOptionsPage();
$('nas').onclick = async () => {
  const {serverUrl} = await chrome.storage.sync.get({serverUrl:''});
  try { const url = new URL(serverUrl); if (!['http:','https:'].includes(url.protocol)) throw new Error(); chrome.tabs.create({url:url.origin}); }
  catch (_) { chrome.runtime.openOptionsPage(); }
};
async function showPersistentStatus() {
  const d = await chrome.storage.local.get({xbm_sync_status:{},xbm_pending_queue:[],xbm_collect_progress:{}});
  const s=d.xbm_sync_status,p=d.xbm_collect_progress;
  const active = s.state === 'collecting' && Date.now()-(p.updatedAt || s.updatedAt || 0)<45000;
  $('sync').disabled=active; $('full').disabled=active; $('stop').hidden=!active;
  $('progress').hidden=!active;
  if(active) $('progress').textContent=`页面已捕获 ${p.count || 0} 条 · 自动滚动 ${p.scrolls || 0} 轮\n${p.phase==='waiting'?'等待 X 加载更多…':'正在自动采集…'} · 已运行 ${Math.floor((Date.now()-(p.startedAt || Date.now()))/1000)} 秒`;
  const labels={interrupted:'采集已中断，已保存内容可补传',collecting:active?'采集中':'采集长时间无响应，请检查 X 页面后重试',idle:'已提交 NAS',pending_delivery:'等待补传',no_capture:'未捕获书签，请刷新 X 书签页',needs_login:'请先登录 X',error:'操作失败',waiting_browser:'请先打开 X 书签页'};
  const reasons={interrupted:'历史采集完整性未确认，请重新开始采集',end:'X 已返回列表结束信号',stalled:'持续无新增，已停止；可能尚未采集完整，可稍后继续',limit:'达到本轮上限，可继续采集',stopped:'已停止采集，已捕获内容保留'};
  $('status').textContent=[s.account?'@'+s.account.slice(2):'',labels[s.state] || '等待首次同步',`待传 NAS：${d.xbm_pending_queue.length} 条`,s.count!==undefined?`页面捕获：${s.count} 条`:'',reasons[s.reason] || (s.state === 'idle' ? '已上传本次捕获内容；历史采集完整性未确认' : ''),s.error || ''].filter(Boolean).join('\n');
}
chrome.storage.onChanged.addListener((changes,area)=>{if(area==='local')showPersistentStatus();});
setInterval(showPersistentStatus,1000);showPersistentStatus();
