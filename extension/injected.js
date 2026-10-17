/* injected.js — 运行在页面 MAIN world，hook fetch/XHR 捕获书签 GraphQL 响应。
 * 与 content.js（ISOLATED world）通过 window.postMessage 通信。
 * 反机器人 token 由 X 自己的 JS 生成，本脚本只做只读监听，不伪造任何请求。
 */
(function () {
  'use strict';
  if (window.__xbm_injected) return;
  window.__xbm_injected = true;

  function currentAccount() {
    const link = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
    const match = link && /^\/([a-zA-Z0-9_]{1,15})\/?$/.exec(link.getAttribute('href') || '');
    if (!match) return '';
    return 'x:' + match[1].toLowerCase();
  }

  let bufferAccount = '';
  const seen = new Set();
  const buffer = [];
  const diagnostics = { graphqlResponses: 0, responses: 0, parsed: 0, operations: [], dataKeys: [] };

  function parseTweet(result) {
    try {
      // 兼容 tombstone / 包裹结构
      const r = result && result.tweet ? result.tweet : result;
      const legacy = r && r.legacy;
      if (!legacy || !(legacy.id_str || r.rest_id)) return null;
      const userLegacy =
        (r.core && r.core.user_results && r.core.user_results.result &&
          r.core.user_results.result.legacy) || {};

      // 媒体：优先 extended_entities（含完整 video_info），回退 entities
      const entities = legacy.extended_entities || legacy.entities || {};
      const media = (entities.media || []).map(function (m) {
        const item = {
          type: m.type || 'photo',
          url: m.media_url_https || m.media_url || '',
          expanded_url: m.expanded_url || '',
        };
        const variants = (m.video_info && m.video_info.variants) || [];
        if (variants.length) {
          item.variants = variants.map(function (v) {
            return {
              content_type: v.content_type || '',
              url: v.url || '',
              bitrate: v.bitrate || 0,
            };
          });
        }
        return item;
      });

      const urls = ((legacy.entities && legacy.entities.urls) || []).map(function (u) {
        return u.expanded_url || u.url || '';
      }).filter(Boolean);

      return {
        tweet_id: legacy.id_str || r.rest_id,
        text: legacy.full_text || '',
        created_at: legacy.created_at || '',
        author: userLegacy.name || '',
        screen_name: userLegacy.screen_name || '',
        url: 'https://x.com/' + (userLegacy.screen_name || 'i') +
             '/status/' + (legacy.id_str || r.rest_id),
        media: media,
        urls: urls,
        // 是否含 X Article：timeline 只给元数据，全文 M3 由扩展代抓
        has_article: !!(r.article || (legacy.entities && legacy.entities.urls || [])
          .some(function (u) { return (u.expanded_url || '').includes('/article/'); })),
      };
    } catch (e) {
      return null;
    }
  }

  function extractFromPayload(payload) {
    const tweets = [];
    try {
      const data = payload && payload.data;
      if (!data) return tweets;
      // Walk timeline instructions, including nested timeline_v2 and module entries.
      // Only tweet_results containers inside instructions may become bookmarks.
      function readEntries(node) {
        if (!node || typeof node !== 'object') return;
        if (node.tweet_results && node.tweet_results.result) {
          const t = parseTweet(node.tweet_results.result);
          if (t && !seen.has(t.tweet_id)) { seen.add(t.tweet_id); tweets.push(t); }
          return;
        }
        for (const value of Object.values(node)) readEntries(value);
      }
      function findTimelines(node) {
        if (!node || typeof node !== 'object') return;
        if (Array.isArray(node.instructions)) { readEntries(node.instructions); return; }
        for (const value of Object.values(node)) findTimelines(value);
      }
      findTimelines(data);
    } catch (e) { /* 解析失败就跳过这批 */ }
    return tweets;
  }

  function handleJson(url, json, account) {
    // 只关心 GraphQL 响应；Bookmarks 相关优先，其他时间线也顺手收（去重兜底）
    if (url.indexOf('/graphql/') === -1) return;
    const operation = url.split('/graphql/')[1].split('?')[0].split('/').pop();
    diagnostics.graphqlResponses++;
    diagnostics.operations = Array.from(new Set(diagnostics.operations.concat(operation))).slice(-10);
    // Avoid parsing unrelated home/likes timelines even while the bookmarks page is open.
    if (!/bookmark/i.test(operation)) return;
    diagnostics.responses++;
    diagnostics.dataKeys = Object.keys((json && json.data) || {}).slice(0, 20);
    // Bind the response to the account at request time, never the currently selected author.
    if (!account || account !== currentAccount()) return;
    if (bufferAccount !== account) { buffer.length = 0; seen.clear(); bufferAccount = account; }
    const tweets = extractFromPayload(json);
    diagnostics.parsed += tweets.length;
    if (tweets.length) {
      buffer.push.apply(buffer, tweets);
      window.postMessage({ __xbm: true, type: 'tweets', tweets: tweets, account: account }, '*');
    }
  }

  // hook fetch（X 网页端主要通道）
  const origFetch = window.fetch;
  window.fetch = async function () {
    const args = arguments;
    let url = '';
    try {
      url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
    } catch (e) {}
    const account = currentAccount();
    const resp = await origFetch.apply(this, args);
    if (url.indexOf('/graphql/') !== -1) {
      try {
        const clone = resp.clone();
        clone.json().then(function (j) { handleJson(url, j, account); }).catch(function () {});
      } catch (e) {}
    }
    return resp;
  };

  // hook XHR（兜底）
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__xbm_url = url;
    this.__xbm_account = currentAccount();
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener('load', function () {
      try {
        const url = this.__xbm_url || '';
        if (url.indexOf('/graphql/') !== -1 && this.responseText) {
          handleJson(url, JSON.parse(this.responseText), this.__xbm_account);
        }
      } catch (e) {}
    });
    return origSend.apply(this, arguments);
  };

  // content.js 通过 postMessage 索取缓冲（用于自动滚动采集结束时）
  window.addEventListener('message', function (ev) {
    if (ev.source !== window || !ev.data || ev.data.__xbm !== true) return;
    if (ev.data.type === 'get-buffer') {
      const account = currentAccount();
      const out = account && account === bufferAccount ? buffer.slice() : [];
      // 保留页面缓冲：本地持久化失败时仍可再次读取。
      const uniq = [];
      const local = new Set();
      out.forEach(function (t) {
        if (!local.has(t.tweet_id)) { local.add(t.tweet_id); uniq.push(t); }
      });
      window.postMessage({ __xbm: true, type: 'buffer', tweets: uniq, account: account, diagnostics: diagnostics }, '*');
    }
    if (ev.data.type === 'ping') {
      window.postMessage({ __xbm: true, type: 'pong' }, '*');
    }
  });
})();
