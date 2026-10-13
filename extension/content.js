/* content.js — 运行在 ISOLATED world。
 * 职责：1) 与 MAIN world 的 injected.js 经 postMessage 通信；
 *       2) 响应 background 的采集指令（自动滚动 + 回收缓冲）；
 *       3) 用户被动浏览时把捕获到的数据实时转给 background。
 */
(function () {
  'use strict';

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // 转发 injected.js 推送的实时捕获（用户被动浏览场景）
  window.addEventListener('message', function (ev) {
    if (ev.source !== window || !ev.data || ev.data.__xbm !== true) return;
    if (ev.data.type === 'tweets' && ev.data.tweets && ev.data.tweets.length) {
      chrome.runtime.sendMessage({
        type: 'tweets-captured',
        tweets: ev.data.tweets,
        page: location.href,
      }).catch(function () { /* background 休眠时忽略，下轮补 */ });
    }
  });

  function getBuffer() {
    return new Promise(function (resolve) {
      function onMsg(ev) {
        if (ev.source !== window || !ev.data || ev.data.__xbm !== true) return;
        if (ev.data.type === 'buffer') {
          window.removeEventListener('message', onMsg);
          resolve(ev.data.tweets || []);
        }
      }
      window.addEventListener('message', onMsg);
      window.postMessage({ __xbm: true, type: 'get-buffer' }, '*');
      // 兜底超时：injected.js 未就绪时返回空
      setTimeout(function () {
        window.removeEventListener('message', onMsg);
        resolve([]);
      }, 3000);
    });
  }

  async function autoScroll(maxScrolls) {
    let lastHeight = -1;
    let stable = 0;
    for (let i = 0; i < maxScrolls; i++) {
      window.scrollTo(0, document.body.scrollHeight);
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
    if (msg && msg.type === 'collect') {
      (async function () {
        try {
          // 先等页面初始加载出第一批
          await sleep(3000);
          if (msg.full) {
            await autoScroll(msg.maxScrolls || 60);
          } else {
            await autoScroll(2); // 增量：首屏即可
          }
          await sleep(1500);
          const tweets = await getBuffer();
          sendResponse({ ok: true, tweets: tweets });
        } catch (e) {
          sendResponse({ ok: false, error: String(e && e.message || e) });
        }
      })();
      return true; // 异步回执
    }
  });
})();
