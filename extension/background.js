/* background.js — MV3 service worker。
 * 职责：1) chrome.alarms 每小时触发一次采集（自动开书签页/滚动/回收/关页）；
 *       2) 接收 content.js 被动捕获的数据，POST 到服务端；
 *       3) 推送失败时入本地队列，下轮补推。
 */
'use strict';

const ALARM_NAME = 'xbm-sync';
const QUEUE_KEY = 'xbm_pending_queue';

async function getConfig() {
  const cfg = await chrome.storage.sync.get({
    serverUrl: 'http://192.168.1.10:8765',
    intervalMinutes: 60,
  });
  cfg.serverUrl = (cfg.serverUrl || '').replace(/\/+$/, '');
  return cfg;
}

async function pushToServer(serverUrl, tweets) {
  const res = await fetch(serverUrl + '/api/ingest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      tweets: tweets,
      source: 'extension',
      captured_at: new Date().toISOString(),
    }),
  });
  if (!res.ok) throw new Error('ingest failed: HTTP ' + res.status);
  return res.json();
}

async function enqueue(tweets) {
  const data = await chrome.storage.local.get({ [QUEUE_KEY]: [] });
  const q = data[QUEUE_KEY].concat(tweets);
  // 上限防爆
  await chrome.storage.local.set({ [QUEUE_KEY]: q.slice(-2000) });
}

async function flushQueue(serverUrl) {
  const data = await chrome.storage.local.get({ [QUEUE_KEY]: [] });
  const q = data[QUEUE_KEY];
  if (!q.length) return;
  await pushToServer(serverUrl, q);
  await chrome.storage.local.set({ [QUEUE_KEY]: [] });
}

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

async function waitForContentScript(tabId, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 20000);
  while (Date.now() < deadline) {
    try {
      await chrome.tabs.sendMessage(tabId, { type: 'ping' });
      return true;
    } catch (e) {
      await sleep(800);
    }
  }
  return false;
}

async function runSync(opts) {
  opts = opts || {};
  const cfg = await getConfig();
  // 先把积压队列推出去
  try { await flushQueue(cfg.serverUrl); } catch (e) { /* 下轮再试 */ }

  const tab = await chrome.tabs.create({
    url: 'https://x.com/i/bookmarks',
    active: false,
  });
  try {
    const ready = await waitForContentScript(tab.id, 25000);
    if (!ready) throw new Error('content script 未就绪（页面可能加载失败）');
    const res = await chrome.tabs.sendMessage(tab.id, {
      type: 'collect',
      full: !!opts.full,
      maxScrolls: opts.full ? 60 : 2,
    });
    if (res && res.ok && res.tweets && res.tweets.length) {
      try {
        await pushToServer(cfg.serverUrl, res.tweets);
      } catch (e) {
        await enqueue(res.tweets);
        throw e;
      }
    }
    return { ok: true, count: (res && res.tweets && res.tweets.length) || 0 };
  } finally {
    try { await chrome.tabs.remove(tab.id); } catch (e) {}
  }
}

// 定时采集
chrome.alarms.onAlarm.addListener(function (alarm) {
  if (alarm.name === ALARM_NAME) {
    runSync({}).catch(function (e) {
      console.warn('[xbm] 定时采集失败:', e && e.message);
    });
  }
});

// 被动捕获：用户手动刷书签页时 content.js 实时转发的
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (msg && msg.type === 'tweets-captured' && msg.tweets && msg.tweets.length) {
    (async function () {
      const cfg = await getConfig();
      try {
        await pushToServer(cfg.serverUrl, msg.tweets);
      } catch (e) {
        await enqueue(msg.tweets);
      }
    })();
    // 不需要回执
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
});
chrome.runtime.onStartup.addListener(async function () {
  const cfg = await getConfig();
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: cfg.intervalMinutes });
});
