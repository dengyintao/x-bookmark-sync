/* background.js — MV3 service worker。
 * 职责：1) chrome.alarms 每小时触发一次采集（复用已打开的书签页）；
 *       2) 接收捕获数据，先持久化到浏览器，再分批 POST；
 *       3) 仅在 NAS 确认后删除队列，断线时独立补传。
 */
'use strict';

const ALARM_NAME = 'xbm-sync';
const QUEUE_KEY = 'xbm_pending_queue';
const STATUS_KEY = 'xbm_sync_status';
const RETRY_ALARM = 'xbm-delivery';
const BATCH_SIZE = 100;
let statusWork = Promise.resolve();
function saveStatus(value) {
  const work = statusWork.then(async () => {
    const data = await chrome.storage.local.get({ xbm_task_history: [], [QUEUE_KEY]: [] });
    const entry = { ...value, pending: data[QUEUE_KEY].length, updatedAt: Date.now() };
    await chrome.storage.local.set({ [STATUS_KEY]: entry,
      xbm_task_history: data.xbm_task_history.concat(entry).slice(-100) });
  });
  statusWork = work.catch(() => {});
  return work;
}
function isBookmarkPage(ready) {
  return ready.pageType === 'bookmarks' ||
    (!ready.pageType && /^\/i\/bookmarks(?:\/|$)/.test(new URL(ready.url).pathname));
}
// Probe actual page state: /i/history can display either bookmarks or likes.
async function findBookmarkTab() {
  const tabs = await chrome.tabs.query({ url: ['https://x.com/*', 'https://twitter.com/*'] });
  tabs.sort((a, b) => Number(!!b.active) - Number(!!a.active) || (b.lastAccessed || 0) - (a.lastAccessed || 0));
  let loginTab;
  for (const tab of tabs) {
    try {
      const ready = await chrome.tabs.sendMessage(tab.id, { type: 'ping' });
      if (!ready || !ready.ok || !ready.ready) continue;
      if (isBookmarkPage(ready)) return tab;
      if (ready.pageType === 'login' || /\/login/.test(ready.url)) loginTab = loginTab || tab;
    } catch (e) { /* A pre-upgrade page needs a refresh to reconnect its script. */ }
  }
  return loginTab || null;
}
async function collectWithRetry(tabId, options) {
  let lastError;
  for (let attempt = 0; attempt < 2; attempt++) {
    const ready = await waitForContentScript(tabId, 25000);
    if (!ready) throw new Error('页面未就绪，请检查 X 页面后重试');
    if (!isBookmarkPage(ready)) {
      if (ready.pageType === 'login' || /\/login/.test(ready.url)) {
        await chrome.tabs.update(tabId, { active: true });
        throw new Error('请在打开的 X 页面完成登录，然后再次点击同步书签');
      }
      throw new Error('请切换到历史页面的书签标签后重试');
    }
    try {
      return await chrome.tabs.sendMessage(tabId, options);
    } catch (error) {
      lastError = error;
      if (attempt === 0) {
        await saveStatus({ state: 'collecting', error: '页面跳转中，正在重新连接采集脚本' });
        await sleep(800);
      }
    }
  }
  throw new Error('采集页面连接中断，请保持 X 页面打开后重试：' + lastError.message);
}

let queueWork = Promise.resolve();
let syncWork = null;
function serializeQueue(fn) {
  const work = queueWork.then(fn);
  queueWork = work.catch(function () {});
  return work;
}

async function getConfig() {
  const cfg = await chrome.storage.sync.get({
    serverUrl: 'http://192.168.1.10:8765',
    intervalMinutes: 60,
  });
  cfg.serverUrl = (cfg.serverUrl || '').replace(/\/+$/, '');
  return cfg;
}

function validAccount(value) { return typeof value === 'string' && /^x:[a-z0-9_]{1,15}$/.test(value); }

async function pushToServer(serverUrl, tweets, account = '') {
  const res = await fetch(serverUrl + '/api/ingest' + (account ? '?account=' + encodeURIComponent(account) : ''), {
    signal: AbortSignal.timeout(20000),
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      tweets: tweets.map(({_xbm_account, ...tweet}) => tweet),
      account: account,
      source: 'extension',
      captured_at: new Date().toISOString(),
    }),
  });
  if (!res.ok) throw new Error('ingest failed: HTTP ' + res.status);
  const receipt = await res.json();
  if (!receipt || receipt.ok !== true) throw new Error('NAS 未确认持久入库');
  if (account && receipt.account !== account) throw new Error('NAS 账户确认不匹配，请升级服务端后重试');
  return receipt;
}

async function enqueueUnlocked(tweets, account = '') {
  const data = await chrome.storage.local.get({ [QUEUE_KEY]: [] });
  const q = Array.from(new Map(data[QUEUE_KEY].concat(tweets.map(t => ({...t, _xbm_account: account}))).map(t => [(t._xbm_account || '') + ':' + t.tweet_id, t])).values());
  // Persist before network I/O. Never silently discard the oldest bookmarks.
  await chrome.storage.local.set({ [QUEUE_KEY]: q });
}

function enqueue(tweets, account = '') { return serializeQueue(() => enqueueUnlocked(tweets, account)); }
function flushQueue(serverUrl) { return serializeQueue(() => flushQueueUnlocked(serverUrl)); }
async function flushQueueUnlocked(serverUrl) {
  const data = await chrome.storage.local.get({ [QUEUE_KEY]: [] });
  let q = data[QUEUE_KEY];
  while (q.length) {
    const account = q[0]._xbm_account || '';
    let length = 0;
    while (length < q.length && length < BATCH_SIZE && (q[length]._xbm_account || '') === account) length++;
    const batch = q.slice(0, length);
    await pushToServer(serverUrl, batch, account);
    q = q.slice(batch.length);
    await chrome.storage.local.set({ [QUEUE_KEY]: q });
  }
  const status = await chrome.storage.local.get({ [STATUS_KEY]: {} });
  if (status[STATUS_KEY] && status[STATUS_KEY].state === 'pending_delivery') {
    await saveStatus({ ...status[STATUS_KEY], state: 'idle', error: '', lastDeliveredAt: Date.now() });
  }
}

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

async function waitForContentScript(tabId, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 20000);
  while (Date.now() < deadline) {
    try {
      const res = await chrome.tabs.sendMessage(tabId, { type: 'ping' });
      if (res && res.ok && res.ready) return res;
    } catch (e) {}
    await sleep(800);
  }
  return false;
}

function runSync(opts) {
  if (syncWork) return syncWork;
  syncWork = runSyncOnce(opts).catch(async error => {
    await saveStatus({ state: /登录/.test(error.message) ? 'needs_login' : 'error', error: error.message });
    throw error;
  }).finally(() => { syncWork = null; });
  return syncWork;
}
async function runSyncOnce(opts) {
  opts = opts || {};
  const cfg = await getConfig();
  const previous = await chrome.storage.local.get({ [STATUS_KEY]: {} });
  if (opts.automatic && previous[STATUS_KEY].state === 'needs_login') return { ok: false, needsLogin: true };
  // 先把积压队列推出去
  try { await flushQueue(cfg.serverUrl); } catch (e) { /* 下轮再试 */ }

  const tab = await findBookmarkTab();
  if (!tab) {
    const error = '请先打开 X 书签页（历史页面请选择“书签”），刷新页面后再同步';
    if (opts.automatic) {
      await saveStatus({ state: 'waiting_browser', error });
      return { ok: false, waitingBrowser: true };
    }
    throw new Error(error);
  }
  await saveStatus({ state: 'collecting' });
  const res = await collectWithRetry(tab.id, {
    type: 'collect', full: !!opts.full, maxScrolls: opts.full ? 60 : 2,
  });
  if (!res || !res.ok) throw new Error((res && res.error) || '采集失败，请检查 X 页面');
  if (!validAccount(res.account)) throw new Error('无法识别当前登录的 X 账户，请刷新书签页后重试');
  if (!res.tweets || !res.tweets.length) {
    await saveStatus({ state: 'no_capture', count: 0, diagnostics: res.diagnostics || {},
      error: '本轮未捕获书签，尚不能确认书签为空。请检查保留页面与采集诊断。' });
    return { ok: true, count: 0, noCapture: true };
  }
  if (res.tweets && res.tweets.length) await enqueue(res.tweets, res.account);
  let deliveryError = '';
  try { await flushQueue(cfg.serverUrl); } catch (e) { deliveryError = e.message; }
  await saveStatus({ state: deliveryError ? 'pending_delivery' : 'idle',
    account: res.account, count: (res.tweets || []).length, error: deliveryError, lastCollectedAt: Date.now() });
  return { ok: true, account: res.account, count: (res.tweets || []).length, pendingDelivery: !!deliveryError };

}

// 定时采集
chrome.alarms.onAlarm.addListener(function (alarm) {
  if (alarm.name === RETRY_ALARM) {
    getConfig().then(cfg => flushQueue(cfg.serverUrl)).catch(e => console.warn('[xbm] 补传失败:', e.message));
  }
  if (alarm.name === ALARM_NAME) {
    runSync({ automatic: true }).catch(function (e) {
      console.warn('[xbm] 定时采集失败:', e && e.message);
    });
  }
});

// 被动捕获：用户手动刷书签页时 content.js 实时转发的
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (msg && msg.type === 'collect-progress') {
    // Frequent page messages keep a long collection observable without storing tweet text.
    chrome.storage.local.set({ xbm_collect_progress: { scrolls: msg.scrolls, updatedAt: Date.now() } }).catch(console.warn);
  }
  if (msg && msg.type === 'tweets-captured' && msg.tweets && msg.tweets.length) {
    (async function () {
      if (!validAccount(msg.account)) throw new Error('无法识别捕获数据所属的 X 账户，请刷新书签页后重试');
      await enqueue(msg.tweets, msg.account);
      sendResponse({ ok: true });
      await saveStatus({ state: 'pending_delivery', account: msg.account, lastCollectedAt: Date.now() });
      const cfg = await getConfig();
      try { await flushQueue(cfg.serverUrl); }
      catch (e) { await saveStatus({ state: 'pending_delivery', error: e.message }); }
    })().catch(e => {
      sendResponse({ ok: false, error: e.message });
      saveStatus({ state: 'error', error: e.message }).catch(console.warn);
    });
    return true;
  }
  if (msg && msg.type === 'manual-sync') {
    runSync({ full: !!msg.full }).then(
      function (r) { sendResponse({ ok: true, result: r }); },
      function (e) { sendResponse({ ok: false, error: String(e && e.message || e) }); }
    );
    return true;
  }
});

// 安装/启动时建定时器
chrome.runtime.onInstalled.addListener(async function () {
  const cfg = await getConfig();
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: cfg.intervalMinutes });
  chrome.alarms.create(RETRY_ALARM, { periodInMinutes: 5 });
});
chrome.runtime.onStartup.addListener(async function () {
  const cfg = await getConfig();
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: cfg.intervalMinutes });
  chrome.alarms.create(RETRY_ALARM, { periodInMinutes: 5 });
});
