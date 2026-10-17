/* content.js — 运行在 ISOLATED world。
 * 职责：1) 与 MAIN world 的 injected.js 经 postMessage 通信；
 *       2) 响应 background 的采集指令（自动滚动 + 回收缓冲）；
 *       3) 用户被动浏览时把捕获到的数据实时转给 background。
 */
(function () {
  'use strict';

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function pageType() {
    if (/^\/i\/bookmarks(?:\/|$)/.test(location.pathname)) return 'bookmarks';
    if (/^\/i\/history(?:\/|$)/.test(location.pathname)) {
      const tab = document.querySelector('[role="tab"][aria-selected="true"]');
      if (!tab) return 'loading';
      return /^(书签|Bookmarks)$/i.test(tab.textContent.trim()) ? 'bookmarks' : 'history_other';
    }
    return /\/(?:login|flow\/login|account\/access)(?:\/|$)/.test(location.pathname) ? 'login' : 'other';
  }

  function currentAccount() {
    const link = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
    const match = link && /^\/([a-zA-Z0-9_]{1,15})\/?$/.exec(link.getAttribute('href') || '');
    if (!match) return '';
    return 'x:' + match[1].toLowerCase();
  }

  // 转发 injected.js 推送的实时捕获（用户被动浏览场景）
  window.addEventListener('message', function (ev) {
    if (ev.source !== window || !ev.data || ev.data.__xbm !== true) return;
    if (pageType() === 'bookmarks' && ev.data.type === 'tweets' && ev.data.tweets && ev.data.tweets.length) {
      if (!ev.data.account || ev.data.account !== currentAccount()) return;
      chrome.runtime.sendMessage({
        type: 'tweets-captured',
        account: ev.data.account,
        tweets: ev.data.tweets,
        page: location.href,
      }).catch(function () { /* background 休眠时忽略，下轮补 */ });
    }
  });

  function getBuffer() {
    return new Promise(function (resolve, reject) {
      let timeout;
      function onMsg(ev) {
        if (ev.source !== window || !ev.data || ev.data.__xbm !== true) return;
        if (ev.data.type === 'buffer') {
          clearTimeout(timeout);
          window.removeEventListener('message', onMsg);
          resolve({ account: ev.data.account, tweets: ev.data.tweets || [], diagnostics: ev.data.diagnostics || {} });
        }
      }
      window.addEventListener('message', onMsg);
      window.postMessage({ __xbm: true, type: 'get-buffer' }, '*');
      // 兜底超时：injected.js 未就绪时返回空
      timeout = setTimeout(function () {
        window.removeEventListener('message', onMsg);
        reject(new Error('页面采集缓冲未响应，请刷新 X 页面后重试'));
      }, 3000);
    });
  }

  async function autoScroll(maxScrolls) {
    let lastHeight = -1;
    let stable = 0;
    for (let i = 0; i < maxScrolls; i++) {
      window.scrollTo(0, document.body.scrollHeight);
      chrome.runtime.sendMessage({ type: 'collect-progress', scrolls: i + 1 }).catch(function () {});
      await sleep(2200);
      const h = document.body.scrollHeight;
      if (h === lastHeight) {
        stable++;
        if (stable >= 3) break; // 到底了
      } else {
        stable = 0;
        lastHeight = h;
      }
    }
  }

  // background 指令：开始一次采集
  chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    if (msg && msg.type === 'ping') {
      sendResponse({ ok: true, url: location.href, pageType: pageType(), account: currentAccount(), ready: document.readyState !== 'loading' && pageType() !== 'loading' });
      return;
    }
    if (msg && msg.type === 'collect') {
      if (pageType() !== 'bookmarks') {
        sendResponse({ ok: false, error: pageType() === 'login' ? '请先完成 X 登录，再点击同步书签' : '请切换到历史页面的书签标签后重试' });
        return;
      }
      (async function () {
        try {
          const account = currentAccount();
          if (!account) throw new Error('无法识别当前登录的 X 账户，请刷新书签页后重试');
          // 先等页面初始加载出第一批
          await sleep(3000);
          if (msg.full) {
            await autoScroll(msg.maxScrolls || 60);
          } else {
            await autoScroll(2); // 增量：首屏即可
          }
          await sleep(1500);
          if (pageType() !== 'bookmarks') {
            throw new Error('页面已离开书签标签，请切换回书签后重试');
          }
          const capture = await getBuffer();
          if (currentAccount() !== account || capture.account !== account) throw new Error('采集期间 X 账户发生变化，请刷新页面后重试');
          sendResponse({ ok: true, ...capture });
        } catch (e) {
          sendResponse({ ok: false, error: String(e && e.message || e) });
        }
      })();
      return true; // 异步回执
    }
  });
})();
