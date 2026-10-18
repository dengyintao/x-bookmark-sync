/* Credentials are local to this browser and bound to the exact NAS origin. */
'use strict';
function nasOrigin(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('请填写 NAS 的 HTTP/HTTPS 地址，不含路径、用户名或密码');
  }
  return url.origin;
}
async function nasRequest(server, path, options = {}, authenticated = true) {
  const origin = nasOrigin(server);
  if (!path.startsWith('/api/') || path.startsWith('//')) throw new Error('无效的 NAS 请求');
  const headers = {...options.headers};
  if (authenticated) {
    const data = await chrome.storage.local.get({xbm_connections: {}});
    const connection = data.xbm_connections[origin];
    if (!connection || !connection.token || connection.expires * 1000 <= Date.now()) {
      throw new Error('请先在设置中配对此 NAS；待传书签仍保存在浏览器');
    }
    headers.Authorization = 'Bearer ' + connection.token;
  }
  const response = await fetch(origin + path, {...options, headers, redirect:'error', credentials:'omit',
    signal: options.signal || AbortSignal.timeout(10000)});
  if (response.status === 401 || response.status === 403) {
    throw new Error('NAS 配对已失效或权限不足，请重新配对；待传书签已保留');
  }
  return response;
}
// Content scripts do not need storage access. Never expose pairing tokens to X pages.
if (chrome.storage.local.setAccessLevel) chrome.storage.local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'}).catch(console.warn);
