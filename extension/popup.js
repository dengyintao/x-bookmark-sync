/* popup.js */
document.getElementById('sync').addEventListener('click', function () {
  setStatus('同步中…');
  chrome.runtime.sendMessage({ type: 'manual-sync' }, function (res) {
    if (res && res.ok && res.result.noCapture) setStatus('本轮未捕获书签；请查看设置与任务记录中的诊断。');
    else if (res && res.ok) setStatus((res.result.account ? '@' + res.result.account.slice(2) + ' · ' : '') + '捕获 ' + (res.result.count || 0) + ' 条，' + (res.result.pendingDelivery ? '已本地保存，等待 NAS 恢复' : '已提交 NAS；媒体下载请查看 NAS 状态'));
    else setStatus('失败：' + ((res && res.error) || '未知错误'));
  });
});
document.getElementById('full').addEventListener('click', function () {
  if (!confirm('历史回填最多滚动 60 轮，不能保证覆盖全部书签，继续？')) return;
  setStatus('回填中…');
  chrome.runtime.sendMessage({ type: 'manual-sync', full: true }, function (res) {
    if (res && res.ok && res.result.noCapture) setStatus('本轮未捕获书签；请查看设置与任务记录中的诊断。');
    else if (res && res.ok) setStatus((res.result.account ? '@' + res.result.account.slice(2) + ' · ' : '') + '捕获 ' + (res.result.count || 0) + ' 条，' + (res.result.pendingDelivery ? '已本地保存，等待 NAS 恢复' : '已提交 NAS；媒体下载请查看 NAS 状态'));
    else setStatus('失败：' + ((res && res.error) || '未知错误'));
  });
});
document.getElementById('options').addEventListener('click', function () {
  chrome.runtime.openOptionsPage();
});
function setStatus(t) { document.getElementById('status').textContent = t; }

async function showPersistentStatus() {
  const data = await chrome.storage.local.get({ xbm_sync_status: {}, xbm_pending_queue: [] });
  const labels = { waiting_browser: '等待打开书签页', no_capture: '未捕获数据（未确认成功）', idle: '已提交 NAS', collecting: '正在采集', needs_login: '需要登录 X，自动采集已暂停',
    pending_delivery: '等待补传至 NAS', error: '上次操作失败' };
  const status = data.xbm_sync_status;
  if (status.state) setStatus((status.account ? '@' + status.account.slice(2) + ' · ' : '') + (labels[status.state] || status.state) +
    '；待传 ' + data.xbm_pending_queue.length + ' 条' + (status.error ? '。' + status.error : ''));
}
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && (changes.xbm_sync_status || changes.xbm_pending_queue)) showPersistentStatus();
});
showPersistentStatus();
