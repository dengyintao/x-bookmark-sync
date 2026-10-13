/* options.js */
async function load() {
  const cfg = await chrome.storage.sync.get({ serverUrl: 'http://192.168.1.10:8765', intervalMinutes: 60 });
  document.getElementById('serverUrl').value = cfg.serverUrl;
  document.getElementById('intervalMinutes').value = cfg.intervalMinutes;
}
document.getElementById('save').addEventListener('click', async function () {
  const serverUrl = document.getElementById('serverUrl').value.trim().replace(/\/+$/, '');
  const intervalMinutes = Math.max(15, parseInt(document.getElementById('intervalMinutes').value, 10) || 60);
  await chrome.storage.sync.set({ serverUrl: serverUrl, intervalMinutes: intervalMinutes });
  chrome.alarms.create('xbm-sync', { periodInMinutes: intervalMinutes });
  setMsg('已保存');
});
document.getElementById('test').addEventListener('click', async function () {
  const serverUrl = document.getElementById('serverUrl').value.trim().replace(/\/+$/, '');
  try {
    const r = await fetch(serverUrl + '/api/status');
    const j = await r.json();
    setMsg(r.ok ? '连接成功：' + JSON.stringify(j) : 'HTTP ' + r.status);
  } catch (e) {
    setMsg('连接失败：' + e.message);
  }
});
function setMsg(t) { document.getElementById('msg').textContent = t; }
load();
