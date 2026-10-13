/* popup.js */
document.getElementById('sync').addEventListener('click', function () {
  setStatus('同步中…');
  chrome.runtime.sendMessage({ type: 'manual-sync' }, function (res) {
    if (res && res.ok) setStatus('完成，捕获 ' + (res.result.count || 0) + ' 条');
    else setStatus('失败：' + ((res && res.error) || '未知错误'));
  });
});
document.getElementById('full').addEventListener('click', function () {
  if (!confirm('全量回填会滚动加载全部书签，可能需要几分钟，继续？')) return;
  setStatus('回填中…');
  chrome.runtime.sendMessage({ type: 'manual-sync', full: true }, function (res) {
    if (res && res.ok) setStatus('完成，捕获 ' + (res.result.count || 0) + ' 条');
    else setStatus('失败：' + ((res && res.error) || '未知错误'));
  });
});
document.getElementById('options').addEventListener('click', function () {
  chrome.runtime.openOptionsPage();
});
function setStatus(t) { document.getElementById('status').textContent = t; }
