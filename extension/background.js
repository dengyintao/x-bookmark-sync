/* background.js — MV3 service worker。
 * 职责：1) chrome.alarms 每小时触发一次采集（复用已打开的书签页）；
 *       2) 接收捕获数据，先持久化到浏览器，再分批 POST；
 *       3) 仅在 NAS 确认后删除队列，断线时独立补传。
 */
'use strict';
importScripts('connection.js');

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
let deliveryWork = null;
let syncWork = null;
let collectingTab = null;
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
  const res = await nasRequest(serverUrl, '/api/ingest' + (account ? '?account=' + encodeURIComponent(account) : ''), {
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
function flushQueue(serverUrl) {
  if (deliveryWork) return deliveryWork;
  deliveryWork = flushQueueUnlocked(serverUrl).finally(() => { deliveryWork = null; });
  return deliveryWork;
}
async function flushQueueUnlocked(serverUrl) {
  // Snapshot this delivery run. Network waits must never block durable captures.
  const data = await serializeQueue(() => chrome.storage.local.get({ [QUEUE_KEY]: [] }));
  let q = data[QUEUE_KEY];
  while (q.length) {
    const account = q[0]._xbm_account || '';
    let length = 0;
    while (length < q.length && length < BATCH_SIZE && (q[length]._xbm_account || '') === account) length++;
    const batch = q.slice(0, length);
    await pushToServer(serverUrl, batch, account);
    await serializeQueue(async () => {
      const current = await chrome.storage.local.get({ [QUEUE_KEY]: [] });
      // Only remove the exact acknowledged payload. A newer capture of the same
      // tweet stays queued, even if it arrived while the old copy was uploading.
      const acknowledged = new Set(batch.map(t => JSON.stringify(t)));
      await chrome.storage.local.set({ [QUEUE_KEY]: current[QUEUE_KEY].filter(t => !acknowledged.has(JSON.stringify(t))) });
    });
    q = q.slice(batch.length);
  }
  const status = await chrome.storage.local.get({ [STATUS_KEY]: {}, [QUEUE_KEY]: [] });
  if (!status[QUEUE_KEY].length && status[STATUS_KEY] && status[STATUS_KEY].state === 'pending_delivery') {
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
  }).finally(() => { syncWork = null; collectingTab = null; });
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
  collectingTab = tab.id;
  await chrome.storage.local.set({xbm_collect_progress:{count:0,scrolls:0,startedAt:Date.now(),updatedAt:Date.now()}});
  await saveStatus({ state: 'collecting' });
  const res = await collectWithRetry(tab.id, {
    type: 'collect', full: !!opts.full, maxScrolls: opts.full ? 300 : 2,
  });
  if (!res || !res.ok) throw new Error((res && res.error) || '采集失败，请检查 X 页面');
  if (!validAccount(res.account)) throw new Error('无法识别当前登录的 X 账户，请刷新书签页后重试');
  if (!res.tweets || !res.tweets.length) {
    await saveStatus({ state: res.reason === 'end' ? 'idle' : 'no_capture', reason: res.reason || '', account: res.account, count: 0, diagnostics: res.diagnostics || {},
      error: res.reason === 'end' ? '' : '本轮未捕获书签，尚不能确认书签为空。请检查页面与采集诊断。' });
    return { ok: true, count: 0, noCapture: true };
  }
  if (res.tweets && res.tweets.length) await enqueue(res.tweets, res.account);
  let deliveryError = '';
  try { await flushQueue(cfg.serverUrl); } catch (e) { deliveryError = e.message; }
  const remaining = await chrome.storage.local.get({[QUEUE_KEY]: []});
  await saveStatus({ state: deliveryError || remaining[QUEUE_KEY].length ? 'pending_delivery' : 'idle',
    account: res.account, reason:res.reason || '', count: (res.tweets || []).length, error: deliveryError, lastCollectedAt: Date.now() });
  return { ok: true, account: res.account, count: (res.tweets || []).length, pendingDelivery: !!deliveryError };

}

async function reportBrowserAccount(account, observedAt = Date.now()) {
  if (!validAccount(account)) throw new Error('无法识别 X 账户');
  await chrome.storage.local.set({ xbm_current_account: account, xbm_account_observed_at: observedAt });
  const cfg = await getConfig();
  const response = await nasRequest(cfg.serverUrl, '/api/browser-account', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    signal: AbortSignal.timeout(10000), body: JSON.stringify({ account, observed_at: observedAt / 1000 }),
  });
  if (!response.ok) throw new Error('账户识别信息尚未送达 NAS');
  const receipt = await response.json();
  if (!receipt.ok || !receipt.observed || receipt.observed.account !== account) throw new Error('NAS 账户识别确认不匹配');
}

// 定时采集
chrome.alarms.onAlarm.addListener(function (alarm) {
  if (alarm.name === RETRY_ALARM) {
    chrome.storage.local.get({xbm_current_account:'',xbm_account_observed_at:0}).then(data => {
      if (data.xbm_current_account) return reportBrowserAccount(data.xbm_current_account, data.xbm_account_observed_at || Date.now());
    }).catch(e => console.warn('[xbm] 账户识别信息待重试:',e.message));
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
  if (msg && msg.type === 'account-observed') {
    reportBrowserAccount(msg.account, msg.observedAt).then(() => sendResponse({ok:true}), error => sendResponse({ok:false,error:error.message}));
    return true;
  }
  if (msg && msg.type === 'collect-progress') {
    // Frequent page messages keep a long collection observable without storing tweet text.
    if (sender.tab && (collectingTab === null || sender.tab.id === collectingTab)) {
      collectingTab = sender.tab.id;
      chrome.storage.local.set({ xbm_collect_progress: { account:msg.account,count:msg.count,scrolls:msg.scrolls,phase:msg.phase,startedAt:msg.startedAt,maxScrolls:msg.maxScrolls,updatedAt:Date.now() } }).catch(console.warn);
    }
  }
  if (msg && msg.type === 'tweets-captured' && msg.tweets && msg.tweets.length) {
    (async function () {
      if (!validAccount(msg.account)) throw new Error('无法识别捕获数据所属的 X 账户，请刷新书签页后重试');
      await enqueue(msg.tweets, msg.account);
      sendResponse({ ok: true });
      if (!syncWork) await saveStatus({ state: 'pending_delivery', account: msg.account, lastCollectedAt: Date.now() });
      const cfg = await getConfig();
      try { await flushQueue(cfg.serverUrl); }
      catch (e) { if (!syncWork) await saveStatus({ state: 'pending_delivery', error: e.message }); }
    })().catch(e => {
      sendResponse({ ok: false, error: e.message });
      saveStatus({ state: 'error', error: e.message }).catch(console.warn);
    });
    return true;
  }
  if (msg && msg.type === 'stop-sync') {
    (async () => {
      const tab = collectingTab !== null ? {id:collectingTab} : await findBookmarkTab();
      if (tab) await chrome.tabs.sendMessage(tab.id,{type:'stop-collect'});
      sendResponse({ok:true});
    })().catch(e => sendResponse({ok:false,error:e.message}));
    return true;
  }
  if (msg && msg.type === 'retry-delivery' && !sender.tab) {
    getConfig().then(cfg => flushQueue(cfg.serverUrl)).then(() => sendResponse({ok:true}), e => sendResponse({ok:false,error:e.message}));
    return true;
  }
  if (msg && msg.type === 'manual-sync') {
    // Acknowledge immediately: popup closure must not own a long-running job.
    runSync({ full: !!msg.full }).catch(e => console.warn('[xbm] collection:', e.message));
    sendResponse({ ok: true, started: true });
    return;
  }
});

// Startup repairs stale collection state and immediately retries the durable outbox.
async function initialize() {
  const cfg = await getConfig();
  await chrome.alarms.create(ALARM_NAME, { periodInMinutes: cfg.intervalMinutes });
  await chrome.alarms.create(RETRY_ALARM, { periodInMinutes: 5 });
  const data = await chrome.storage.local.get({[STATUS_KEY]: {}, [QUEUE_KEY]: []});
  if (!syncWork && data[STATUS_KEY].state === 'collecting') {
    await saveStatus({...data[STATUS_KEY], state: 'interrupted', reason: 'interrupted',
      error: '浏览器或扩展已重启，历史采集完整性未确认；已保存内容会继续补传，请重新开始采集。'});
  }
  try { await flushQueue(cfg.serverUrl); }
  catch (e) { await saveStatus({...data[STATUS_KEY], state:'pending_delivery',
    reason: data[STATUS_KEY].state === 'collecting' ? 'interrupted' : data[STATUS_KEY].reason,
    error:e.message}); }
}
chrome.runtime.onInstalled.addListener(initialize);
chrome.runtime.onStartup.addListener(initialize);
