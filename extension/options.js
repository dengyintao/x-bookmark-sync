/* options.js */
async function allowServer(serverUrl) {
  const url = new URL(nasOrigin(serverUrl));
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('请填写完整的 HTTP/HTTPS 服务地址，例如 http://192.168.192.2:8765');
  }
  const allowed = await chrome.permissions.request({ origins: [url.origin + '/*'] });
  if (!allowed) throw new Error('需要允许访问 NAS 地址才能同步');
}
async function load() {
  const cfg = await chrome.storage.sync.get({ serverUrl: 'http://192.168.1.10:8765', intervalMinutes: 60 });
  document.getElementById('serverUrl').value = cfg.serverUrl;
  document.getElementById('intervalMinutes').value = cfg.intervalMinutes;
}
document.getElementById('save').addEventListener('click', async function () {
  const serverUrl = document.getElementById('serverUrl').value.trim().replace(/\/+$/, '');
  const intervalMinutes = Math.max(15, parseInt(document.getElementById('intervalMinutes').value, 10) || 60);
  try {
  await allowServer(serverUrl);
  await chrome.storage.sync.set({ serverUrl: serverUrl, intervalMinutes: intervalMinutes });
  chrome.alarms.create('xbm-sync', { periodInMinutes: intervalMinutes });
  setMsg('已保存');
  } catch (e) { setMsg(e.message); }
});
document.getElementById('test').addEventListener('click', async function () {
  const serverUrl = document.getElementById('serverUrl').value.trim().replace(/\/+$/, '');
  try {
    await allowServer(serverUrl);
    const health = await nasRequest(serverUrl, '/api/health', {}, false);
    const info = await health.json();
    if (!health.ok || info.service !== 'xbm' || info.protocol_version !== 1 || !info.pairing_required)
      throw new Error('服务版本不支持安全配对，请先升级 NAS 服务');
    const r = await nasRequest(serverUrl, '/api/client/status');
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error('HTTP ' + r.status);
    setMsg('连接正常 · NAS ' + info.version + ' · 扩展 ' + chrome.runtime.getManifest().version + '。下一步：打开 X 书签页，在扩展弹窗开始自动采集。');
  } catch (e) {
    setMsg('连接失败：' + e.message);
  }
});
function setMsg(t) { document.getElementById('msg').textContent = t; }
load();

async function loadHistory() {
  const data = await chrome.storage.local.get({ xbm_task_history: [], xbm_sync_status: {} });
  const entries = data.xbm_task_history.length ? data.xbm_task_history :
    (data.xbm_sync_status.state ? [data.xbm_sync_status] : []);
  const labels = {interrupted: '采集已中断，完整性未确认', waiting_browser: '等待打开书签页', no_capture:'未捕获数据（未确认成功）', idle:'已提交 NAS', collecting:'采集中', needs_login:'需要登录', pending_delivery:'等待补传', error:'失败'};
  const container = document.getElementById('history');
  container.replaceChildren();
  if (!entries.length) container.textContent = '暂无任务记录';
  for (const entry of entries.slice().reverse().slice(0, 30)) {
    const row = document.createElement('p');
    row.textContent = (entry.account ? '@' + entry.account.slice(2) + ' · ' : '') + new Date(entry.updatedAt).toLocaleString() + ' · ' + (labels[entry.state] || entry.state) +
      (entry.count !== undefined ? ' · 捕获 ' + entry.count + ' 条' : '') +
      (entry.pending !== undefined ? ' · 待传 ' + entry.pending + ' 条' : '') +
      (entry.error ? ' · ' + entry.error : '') +
      (entry.diagnostics ? ' · 诊断：' + JSON.stringify(entry.diagnostics) : '');
    container.append(row);
  }
}
document.getElementById('refreshHistory').addEventListener('click', loadHistory);
loadHistory();

document.getElementById('pair').addEventListener('click', async () => {
  const button = document.getElementById('pair');
  button.disabled = true;
  try {
    const origin = nasOrigin(document.getElementById('serverUrl').value.trim());
    await allowServer(origin);
    const health = await nasRequest(origin, '/api/health', {}, false);
    const info = await health.json();
    if (!health.ok || info.service !== 'xbm' || info.protocol_version !== 1 || !info.pairing_required)
      throw new Error('请先升级 NAS 服务到支持安全配对的版本');
    const response = await nasRequest(origin, '/api/access/pair', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        code:document.getElementById('pairCode').value.trim(), name:document.getElementById('clientName').value.trim()})}, false);
    const result = await response.json();
    if (!response.ok || !result.ok || typeof result.token !== 'string' || !result.token || !Number.isFinite(result.expires))
      throw new Error(result.error || '配对失败，请重新生成配对码');
    const data = await chrome.storage.local.get({xbm_connections:{}});
    data.xbm_connections[origin] = {token:result.token, id:result.id, expires:result.expires};
    await chrome.storage.local.set({xbm_connections:data.xbm_connections});
    await chrome.storage.sync.set({serverUrl:origin});
    document.getElementById('pairCode').value = '';
    chrome.runtime.sendMessage({type:'retry-delivery'}).catch(console.warn);
    setMsg('配对成功。现在打开 X 书签页，在扩展弹窗点击“自动采集历史书签”。已有待传内容会自动补传。');
  } catch (error) { setMsg(error.message); }
  finally { button.disabled = false; }
});
