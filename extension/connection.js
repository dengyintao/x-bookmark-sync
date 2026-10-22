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
      if (connection) throw new Error('NAS 配对凭据已过期，请重新配对；待传书签已保留');
      const legacy = await chrome.storage.local.get({xbm_legacy_origins:{}});
      const cfg = await chrome.storage.sync.get({serverUrl:''});
      // An unpacked 0.3 worker can read updated files before Chrome reloads its manifest.
      const oldRuntime = chrome.runtime.getManifest?.().version === '0.3.0' && cfg.serverUrl && nasOrigin(cfg.serverUrl) === origin;
      if (!legacy.xbm_legacy_origins[origin] && !oldRuntime)
        throw new Error('请先在设置中配对此 NAS；待传书签仍保存在浏览器');
      const health = await fetch(origin+'/api/health', {redirect:'error',credentials:'omit',signal:AbortSignal.timeout(10000)});
      if (health.status !== 404) throw new Error('NAS 已升级，请在设置中重新配对；待传书签已保留');
      const status = await fetch(origin+'/api/status', {redirect:'error',credentials:'omit',signal:AbortSignal.timeout(10000)});
      const info = await status.json();
      if (!status.ok || info.service !== 'xbm' || !/^0\.5\./.test(info.version || ''))
        throw new Error('旧 NAS 服务身份无法确认，请升级并重新配对');
      // This origin was already configured in the pre-pairing extension. Never send a pairing token in legacy mode.
    } else headers.Authorization = 'Bearer ' + connection.token;
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

async function saveNasConnection(origin, result) {
  if (!result.ok || typeof result.token !== 'string' || !result.token || !Number.isFinite(result.expires))
    throw new Error('连接未完成，请重新连接 NAS');
  const data = await chrome.storage.local.get({xbm_connections:{},xbm_legacy_origins:{}});
  data.xbm_connections[origin] = {token:result.token, id:result.id, expires:result.expires};
  delete data.xbm_legacy_origins[origin];
  await chrome.storage.local.set({xbm_connections:data.xbm_connections,xbm_legacy_origins:data.xbm_legacy_origins});
  await chrome.storage.sync.set({serverUrl:origin});
}
async function beginNasConnection(origin, name) {
  const health = await nasRequest(origin, '/api/health', {}, false);
  const info = await health.json();
  if (!health.ok || info.service !== 'xbm' || info.protocol_version !== 1 || !info.connection_approval)
    throw new Error('此 NAS 尚不支持一键连接，请升级服务或展开备用配对码');
  const response = await nasRequest(origin, '/api/access/connect', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})}, false);
  const request = await response.json();
  if (!response.ok || !request.ok || !/^[a-f0-9]{32}$/.test(request.id) || typeof request.secret !== 'string' || !Number.isFinite(request.expires_in) || request.expires_in <= 0 || request.expires_in > 300)
    throw new Error(request.error || '无法发起连接，请重试');
  return request;
}
async function claimNasConnection(origin, request) {
  const response = await nasRequest(origin, '/api/access/claim', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:request.id,secret:request.secret})}, false);
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || '连接确认失败，请重试');
  if (!result.pending) await saveNasConnection(origin, result);
  return !result.pending;
}
