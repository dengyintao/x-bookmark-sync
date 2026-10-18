"use strict";
let authenticated = false, authGeneration = 0;
const $ = (id) => document.getElementById(id);
const labels = {
  pending: "等待下载",
  downloading: "正在下载",
  done: "已下载",
  failed: "下载失败",
  ok: "已接收",
  running: "运行中",
  photo: "图片",
  video: "视频",
  gif: "动图",
  hls: "视频",
};
const views = {
  bookmarks: {
    title: "全部书签",
    subtitle: "值得保存的内容，随时回看。",
    note: "从浏览器插件同步到 NAS 的书签。点击详情查看正文和已下载媒体。",
    filters: [
      ["", "全部类型"],
      ["photo", "图片"],
      ["video", "视频"],
      ["gif", "动图"],
      ["hls", "HLS 视频"],
      ["text", "纯文字"],
    ],
  },
  runs: {
    title: "同步记录",
    subtitle: "每一次接收，都有迹可循。",
    note: "这里显示 NAS 已收到的同步批次，一次插件同步可能拆成多个批次。浏览器未送达的任务请在插件中查看。",
    filters: [],
  },
  downloads: {
    title: "下载队列",
    subtitle: "下载进度、等待重试和失败原因，一目了然。",
    note: "进度按已传输字节计算；无法获取总大小或 HLS 转码时不显示百分比。自动重试有退避等待。",
    filters: [
      ["", "全部状态"],
      ["downloading", "正在下载"],
      ["pending", "等待下载"],
      ["failed", "下载失败"],
      ["done", "已下载"],
    ],
  },
};
let view = "bookmarks",
  offset = 0,
  total = 0,
  items = [],
  generation = 0,
  loading = false;
const limit = 24;
let selectedAccount = localStorage.getItem("xbm-account") || "";
let accountsSignature = "";
const selectedIds = new Set();
let layout = localStorage.getItem("xbm-layout") || "cards";
function accountUrl(path) {
  const url = new URL(path, location.origin);
  url.searchParams.set("account", selectedAccount);
  return url.pathname + url.search;
}
async function refreshAccounts() {
  const data = await api("/api/accounts");
  if (!data.items.some((item) => item.id === selectedAccount))
    selectedAccount = "";
  const signature = JSON.stringify(data.items);
  if (signature !== accountsSignature) {
    $("account").replaceChildren(
      ...data.items.map((item) => {
        const option = el("option", item.label + " · " + item.tweets);
        option.value = item.id;
        return option;
      }),
    );
    accountsSignature = signature;
  }
  $("account").value = selectedAccount;
}
function el(tag, text, className) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (className) n.className = className;
  return n;
}
function badge(value) {
  return el("span", labels[value] || value, "badge " + value);
}
function date(value) {
  if (!value) return "—";
  const d = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(d.getTime())
    ? String(value)
    : d.toLocaleString("zh-CN", { hour12: false });
}
function bytes(n) {
  n = Number(n) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}
async function dashboardHeaders(headers = {}) {
  const response = await fetch('/api/dashboard-token', {cache:'no-store',credentials:'same-origin',signal:AbortSignal.timeout(10000)});
  const data = await response.json();
  if (!response.ok || !data.ok || !data.token) throw new Error('请刷新管理页面后重试');
  return {...headers,'X-XBM-Dashboard':data.token};
}
async function api(path, options = {}) {
  if (!authenticated) throw new Error("请先登录管理页面");
  const authStamp = authGeneration;
  if ((options.method || 'GET').toUpperCase() === 'POST') options = {...options,headers:await dashboardHeaders(options.headers)};
  const r = await fetch(path === "/api/accounts" ? path : accountUrl(path), {
    ...options,
    signal: AbortSignal.timeout(12000),
    cache: "no-store",
  });
  const data = await r.json();
  if (r.status === 401) lockDashboard();
  if (authStamp !== authGeneration) throw new Error("登录状态已变化，请重试");
  if (!r.ok || !data.ok)
    throw new Error(data.error || data.message || `HTTP ${r.status}`);
  return data;
}
function notice(message = "") {
  $("notice").hidden = !message;
  $("notice").textContent = message;
}
async function status() {
  const account = selectedAccount;
  try {
    const data = await api("/api/status");
    if (account !== selectedAccount) return;
    const m = data.media;
    if (data.storage_free < 512 * 1024 * 1024) notice("存储空间不足 512 MB，已暂停领取新下载任务。请释放空间或迁移数据目录。");
    $("count-tweets").textContent = data.tweets.toLocaleString();
    $("count-done").textContent = m.done.toLocaleString();
    $("count-pending").textContent = `${m.downloading} / ${m.pending}`;
    $("count-failed").textContent = m.failed;
    const count = Object.values(m).reduce((a, b) => a + b, 0);
    $("completion").textContent = count
      ? `共 ${count} 个媒体 · 完成 ${Math.round((m.done / count) * 100)}%`
      : "暂无媒体任务";
    $("connection").textContent = "服务已连接";
    $("connection").className = "dot online";
    await refreshAccounts();
  } catch (e) {
    if (account !== selectedAccount) return;
    $("connection").textContent = "连接中断";
    $("connection").className = "dot offline";
    notice("无法连接 NAS，当前显示可能是旧数据。" + e.message);
  }
}
function author(item) {
  const n = el("div", undefined, "author");
  n.append(
    el("span", (item.author || item.screen_name || "X").slice(0, 1), "avatar"),
  );
  const text = el("div");
  text.append(
    el("strong", item.author || item.screen_name || "未知作者"),
    el("small", item.screen_name ? "@" + item.screen_name : "X 书签"),
  );
  n.append(text);
  return n;
}
function original(item) {
  const a = el("a", "在 X 查看 ↗");
  a.href = "https://x.com/i/status/" + encodeURIComponent(item.tweet_id);
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}
function bookmarks() {
  const grid = el("div", undefined, "grid layout-" + layout);
  const renderedAccount = selectedAccount;
  for (const item of items) {
    const card = el("article", undefined, "card");
    const check = el('input'); check.type = 'checkbox'; check.checked = selectedIds.has(item.tweet_id);
    check.setAttribute('aria-label', '选择书签 ' + item.tweet_id);
    check.addEventListener('change', () => { check.checked ? selectedIds.add(item.tweet_id) : selectedIds.delete(item.tweet_id); selectionStatus(); });
    card.append(check);
    if (layout === 'preview') {
      const cover = (item.media || []).find(m => m.status === 'done');
      if (cover) {
        const preview = el(cover.kind === 'photo' ? 'img' : 'video');
        preview.src = accountUrl('/api/media/' + cover.id);
        preview.className = 'cover';
        if (cover.kind === 'photo') { preview.alt = '书签预览'; preview.loading = 'lazy'; }
        else { preview.controls = true; preview.preload = 'none'; }
        card.append(preview);
      }
    }
    card.append(
      author(item),
      el("p", item.text || "这条书签没有正文。", "text"),
    );
    const foot = el("div", undefined, "card-foot");
    const media = item.media || [];
    if (media.length) {
      foot.append(
        badge(media[0].kind),
        el(
          "span",
          `${media.filter((m) => m.status === "done").length}/${media.length} 已下载`,
          "badge",
        ),
      );
    } else foot.append(el("span", "纯文字", "badge"));
    foot.append(el("small", date(item.synced_at)));
    card.append(foot);
    const button = el("button", "查看详情 →");
    button.addEventListener("click", () => detail(item));
    card.append(button);
    const remove = el('button', $('trash').value === '1' ? '恢复书签' : '删除', 'outline');
    remove.addEventListener('click', () => changeBookmarks([item.tweet_id], renderedAccount));
    card.append(remove);
    grid.append(card);
  }
  return grid;
}
function detail(item) {
  const body = $("detail-body");
  body.replaceChildren(
    author(item),
    el("p", item.text || "暂无正文", "text"),
    original(item),
    el("p", "入库时间：" + date(item.synced_at), "view-note"),
  );
  for (const m of item.media || []) {
    if (m.status === "done") {
      if (m.kind === "photo") {
        const img = el("img");
        img.src = accountUrl("/api/media/" + m.id);
        img.alt = "书签图片";
        img.loading = "lazy";
        img.addEventListener("error", () => {
          img.replaceWith(
            el("p", "文件不可用，请检查 NAS 存储目录。", "media-placeholder"),
          );
        });
        body.append(img);
      } else {
        const video = el("video");
        video.controls = true;
        video.preload = "metadata";
        video.src = accountUrl("/api/media/" + m.id);
        body.append(video);
      }
    } else
      body.append(
        el(
          "p",
          (labels[m.kind] || m.kind) +
            " · " +
            (labels[m.status] || m.status) +
            (m.error ? " · " + m.error : ""),
          "media-placeholder",
        ),
      );
  }
  $("detail").showModal();
}
function table(headings) {
  const wrap = el("div", undefined, "table-wrap"),
    t = el("table"),
    head = el("thead"),
    row = el("tr");
  headings.forEach((h) => row.append(el("th", h)));
  head.append(row);
  const body = el("tbody");
  t.append(head, body);
  wrap.append(t);
  return [wrap, body];
}
function runs() {
  const [wrap, body] = table([
    "接收时间",
    "来源 / 状态",
    "收到 / 新增 / 重复",
    "媒体入队",
  ]);
  for (const item of items) {
    const row = el("tr"),
      source = el("td");
    source.append(
      badge(item.status),
      el("small", item.source === "legacy" ? "历史记录" : item.source || "—"),
    );
    if (item.error) source.append(el("small", item.error, "error"));
    const when = el("td", date(item.started_at));
    if (item.captured_at)
      when.append(el("small", "浏览器采集：" + date(item.captured_at)));
    row.append(
      when,
      source,
      el(
        "td",
        `${item.received_count ?? "—"} / ${item.new_count ?? "—"} / ${item.dup_count ?? "—"}`,
      ),
      el("td", item.media_queued ?? "—"),
    );
    body.append(row);
  }
  return wrap;
}
function downloads() {
  const [wrap, body] = table([
    "媒体",
    "下载状态",
    "进度 / 大小",
    "重试 / 错误",
    "操作",
  ]);
  for (const item of items) {
    const row = el("tr"),
      who = el("td", item.author || item.screen_name || "未知作者");
    who.append(
      el("small", `${labels[item.kind] || item.kind} · ${item.tweet_id}`),
    );
    const state = el("td");
    state.append(badge(item.status));
    const progress = el("td", undefined, "progress");
    if (item.status === "downloading") {
      const p = el("progress");
      p.max = item.total_bytes || 1;
      if (item.total_bytes) p.value = Math.min(item.bytes, item.total_bytes);
      p.setAttribute("aria-label", "下载进度");
      progress.append(
        p,
        el(
          "span",
          item.total_bytes
            ? `${Math.floor(Math.min(item.bytes / item.total_bytes, 1) * 100)}% · ${bytes(item.bytes)} / ${bytes(item.total_bytes)}`
            : `${bytes(item.bytes)} · 总大小未知`,
        ),
      );
      const speed = Date.now() / 1000 - item.updated_at < 5 ? item.speed : 0;
      progress.append(el("small", speed ? bytes(speed) + "/s" : "正在处理…"));
    } else
      progress.append(
        el(
          "span",
          item.status === "done"
            ? bytes(item.bytes)
            : `${bytes(item.bytes)} 已传输`,
        ),
      );
    const error = el("td", `已重试 ${item.retries} 次`);
    if (item.error) error.append(el("small", item.error, "error"));
    if (item.next_retry_at > Date.now() / 1000)
      error.append(el("small", "下次尝试：" + date(item.next_retry_at)));
    const action = el("td");
    if (item.status === "failed" || item.status === "pending") {
      const b = el("button", item.error ? "立即重试" : "重新排队", "outline");
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          await api(`/api/downloads/${item.id}/retry`, {
            method: "POST",
            headers: { "X-XBM-Action": "retry" },
          });
          notice("已加入重试队列，后台将在数秒内开始处理。");
          await load();
        } catch (e) {
          notice(e.message);
          b.disabled = false;
        }
      });
      action.append(b);
    } else if (item.status === "done") {
      const a = el("a", "打开文件 ↗");
      a.href = accountUrl("/api/media/" + item.id);
      a.target = "_blank";
      a.rel = "noopener";
      action.append(a);
    }
    row.append(who, state, progress, error, action);
    body.append(row);
  }
  return wrap;
}
function render() {
  if (!items.length) {
    const empty = el("div", undefined, "empty");
    empty.append(
      el(
        "strong",
        view === "bookmarks"
          ? "暂无符合条件的书签"
          : view === "runs"
            ? "还没有同步记录"
            : "暂无符合条件的下载任务",
      ),
      el(
        "span",
        view === "bookmarks"
          ? "在浏览器打开 X 书签页，通过插件同步后即可在这里查看。"
          : "任务到达后会自动显示在这里。",
      ),
    );
    $("content").replaceChildren(empty);
  } else
    $("content").replaceChildren(
      view === "bookmarks"
        ? bookmarks()
        : view === "runs"
          ? runs()
          : downloads(),
    );
  $("total").textContent = `共 ${total} 条`;
  $("page").textContent = total
    ? `${Math.floor(offset / limit) + 1} / ${Math.ceil(total / limit)}`
    : "0 / 0";
  $("prev").disabled = offset === 0;
  $("next").disabled = offset + limit >= total;
}
async function load() {
  const token = ++generation;
  loading = true;
  document.body.classList.add("busy");
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (view === "bookmarks") {
    params.set("q", $("search").value);
    params.set("kind", $("filter").value);
    params.set("status", $("download-filter").value);
    params.set("trash", $("trash").value);
  }
  if (view === "downloads") params.set("status", $("filter").value);
  try {
    const data = await api(`/api/${view}?${params}`);
    if (token !== generation) return;
    selectedIds.clear(); selectionStatus();
    items = data.items;
    total = data.total;
    if (offset && offset >= total) {
      offset = Math.max(0, Math.ceil(total / limit) - 1) * limit;
      return await load();
    }
    render();
  } catch (e) {
    if (token === generation) notice("加载失败：" + e.message);
  } finally {
    if (token === generation) {
      loading = false;
      document.body.classList.remove("busy");
    }
  }
}
function navigate() {
  selectedIds.clear(); selectionStatus();
  $("content").replaceChildren(el("div", "正在加载…", "empty"));
  view = location.hash.slice(1);
  if (!views[view]) view = "bookmarks";
  generation++;
  offset = 0;
  const config = views[view];
  $("title").textContent = config.title;
  $("subtitle").textContent = config.subtitle;
  $("view-note").textContent = config.note;
  document.querySelectorAll("[data-view]").forEach((a) => {
    a.classList.toggle("active", a.dataset.view === view);
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  $("search-form").hidden = view !== "bookmarks";
  $("library-tools").hidden = view !== "bookmarks";
  $("filter").hidden = !config.filters.length;
  $("filter").replaceChildren(
    ...config.filters.map(([v, t]) => {
      const o = el("option", t);
      o.value = v;
      return o;
    }),
  );
  notice();
  load();
}
function selectionStatus() {
  $('selection-count').textContent = selectedIds.size ? `已选 ${selectedIds.size} 条` : '';
  $('delete-selected').disabled = !selectedIds.size;
  $('purge-selected').hidden = $('trash').value !== '1';
  $('purge-selected').disabled = !selectedIds.size;
  $('delete-selected').textContent = $('trash').value === '1' ? '恢复所选' : '删除所选';
}
async function changeBookmarks(ids, account = selectedAccount) {
  if(account !== selectedAccount || loading) return;
  const restore = $('trash').value === '1';
  if (!await confirmAction(restore ? `恢复 ${ids.length} 条书签？` : `将 ${ids.length} 条书签移入回收站？本地文件保留，可恢复；不影响 X 上的收藏。`)) return;
  try {
    await api('/api/bookmarks/' + (restore ? 'restore' : 'delete'), {method:'POST', headers:{'Content-Type':'application/json','X-XBM-Action':'bookmarks'}, body:JSON.stringify({ids})});
    notice(restore ? '已恢复书签。' : '已移入回收站；保留本地文件，不会因再次同步自动恢复。');
    await load(); await status();
  } catch (error) { notice(error.message); }
}
$('layout').value = layout;
$('layout').addEventListener('change', () => {layout = $('layout').value; localStorage.setItem('xbm-layout',layout);render();});
for (const id of ['download-filter','trash']) $(id).addEventListener('change', () => {offset=0;load();});
$('select-page').addEventListener('click', () => { if(selectedIds.size === items.length) selectedIds.clear(); else items.forEach(i => selectedIds.add(i.tweet_id));selectionStatus();render(); });
$('delete-selected').addEventListener('click', () => changeBookmarks([...selectedIds]));
$("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  offset = 0;
  notice();
  load();
});
$("filter").addEventListener("change", () => {
  offset = 0;
  load();
});
$("prev").addEventListener("click", () => {
  offset = Math.max(0, offset - limit);
  load();
});
$("next").addEventListener("click", () => {
  offset += limit;
  load();
});
$("refresh").addEventListener("click", () => {
  notice();
  status();
  load();
});
$("close-detail").addEventListener("click", () => $("detail").close());
window.addEventListener("hashchange", navigate);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    status();
    load();
  }
});
$("account").addEventListener("change", () => {
  selectedAccount = $("account").value;
  localStorage.setItem("xbm-account", selectedAccount);
  $("detail").close();
  $("search").value = "";
  for (const id of [
    "count-tweets",
    "count-done",
    "count-pending",
    "count-failed",
  ])
    $(id).textContent = "—";
  navigate();
  status();
});
$("detail").addEventListener("close", () => $("detail-body").replaceChildren());
bootstrapAccess();
setInterval(() => {
  if (document.hidden || !authenticated) return;
  status();
  if (view !== "bookmarks" && !loading) load();
}, 3000);

// Ownership preview remains visible while only the durable task history polls.
let ownershipPlan = null;
let ownershipGeneration = 0;
const operationLabels = {
  queued: "排队中",
  running: "正在迁移",
  completed: "归属完成",
  failed: "失败，未完成迁移",
  interrupted: "已中断，请重新预览",
  rolled_back: "已撤销归属",
  rolling_back: "正在撤销",
};
let ownershipHistoryGeneration = 0;
let ownershipHistorySignature = "";
async function ownershipHistory() {
  const generation = ++ownershipHistoryGeneration;
  try {
    const data = await api("/api/migrations");
    if (generation !== ownershipHistoryGeneration) return;
    const signature = JSON.stringify(data.items);
    if (signature === ownershipHistorySignature) return;
    ownershipHistorySignature = signature;
    const openJobs = new Set(
      Array.from($("ownership-history").querySelectorAll("details[open]")).map(
        (row) => row.dataset.job,
      ),
    );
    const nodes = data.items.map((job) => {
      const row = el("details");
      row.dataset.job = job.id;
      row.open = openJobs.has(job.id);
      row.append(
        el(
          "summary",
          date(job.created_at) +
            " · @" +
            job.account.slice(2) +
            " · " +
            (operationLabels[job.status] || job.status),
        ),
      );
      const result = job.result || {};
      if (result.imported)
        row.append(
          el(
            "p",
            `旧回执记录：${result.tweets} 条书签、${result.media} 个媒体、${result.runs} 条同步记录；未重新执行迁移，原备份继续保留。`,
            "view-note",
          ),
        );
      if (result.validated)
        row.append(
          el(
            "p",
            `${result.tweets} 条书签、${result.media} 个媒体、${result.runs} 条同步记录；文件和数量校验通过。`,
          ),
        );
      if (result.backup)
        row.append(el("p", "备份：" + result.backup, "view-note"));
      if (job.error) row.append(el("p", job.error, "error"));
      if (job.status === "completed" && result.after_token) {
        const undo = el("button", "撤销此次归属", "outline");
        undo.addEventListener("click", async () => {
          if (
            !await confirmAction(
              "确认撤销此次归属？程序会先检查是否有新数据；发生变化时会拒绝覆盖。",
            )
          )
            return;
          undo.disabled = true;
          try {
            await api("/api/migrations/" + job.id + "/rollback", {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-XBM-Action": "migrate",
              },
              body: "{}",
            });
            ownershipHistory();
            status();
            load();
          } catch (error) {
            $("ownership-plan").textContent = error.message;
            undo.disabled = false;
          }
        });
        row.append(undo);
      }
      return row;
    });
    $("ownership-history").replaceChildren(
      ...(nodes.length ? nodes : [el("p", "暂无归属任务", "view-note")]),
    );
  } catch (error) {
    $("ownership-history").textContent = error.message;
  }
}
async function readObservedAccount() {
  const data = await api("/api/browser-account");
  if (!data.observed)
    throw new Error(
      "尚未收到插件识别信息。请在已登录的浏览器打开或刷新 X 页面，等待约 15 秒后重试。",
    );
  await refreshAccounts();
  $("ownership-target").replaceChildren(
    ...Array.from($("account").options)
      .filter((o) => o.value)
      .map((o) => {
        const option = el("option", o.textContent);
        option.value = o.value;
        return option;
      }),
  );
  $("ownership-target").value = data.observed.account;
  $("ownership-account-note").textContent =
    "最近由插件识别：@" +
    data.observed.account.slice(2) +
    " · " +
    date(data.observed.observed_at) +
    "。请确认这是旧数据所属的账户。";
}
function clearOwnershipPlan() {
  ownershipGeneration++;
  ownershipPlan = null;
  $("ownership-confirm").hidden = true;
  $("ownership-plan").replaceChildren();
}
$("ownership-open").addEventListener("click", async () => {
  clearOwnershipPlan();
  $("ownership").showModal();
  try {
    await refreshAccounts();
    $("ownership-target").replaceChildren(
      ...Array.from($("account").options)
        .filter((o) => o.value)
        .map((o) => {
          const option = el("option", o.textContent);
          option.value = o.value;
          return option;
        }),
    );
    if (selectedAccount) $("ownership-target").value = selectedAccount;
    $("ownership-account-note").textContent =
      "可从插件读取最近识别的登录账户，或手动选择已知账户。";
  } catch (error) {
    $("ownership-plan").textContent = error.message;
  }
  ownershipHistory();
});
$("ownership-close").addEventListener("click", () => $("ownership").close());
$("ownership-target").addEventListener("change", clearOwnershipPlan);
$("ownership-read").addEventListener("click", async () => {
  clearOwnershipPlan();
  try {
    await readObservedAccount();
  } catch (error) {
    $("ownership-account-note").textContent = error.message;
  }
});
$("ownership-preview").addEventListener("click", async () => {
  clearOwnershipPlan();
  const token = ownershipGeneration;
  const account = $("ownership-target").value;
  try {
    if (!account) throw new Error("请先让插件识别一个登录账户");
    const plan = await api(
      "/api/migrations/preview?target=" + encodeURIComponent(account),
    );
    if (token !== ownershipGeneration) return;
    ownershipPlan = plan;
    $("ownership-plan").replaceChildren(
      el(
        "p",
        `目标 @${account.slice(2)}：${plan.tweets} 条书签、${plan.media_files} 个媒体、${plan.sync_runs} 条同步记录。`,
      ),
      el(
        "p",
        `重复书签 ${plan.duplicate_tweets} 条，重复媒体 ${plan.duplicate_media} 个，将合并而不是重复入库。`,
        "view-note",
      ),
    );
    if (plan.busy)
      $("ownership-plan").append(
        el("p", "仍有下载正在进行，请等待完成后重新预览。"),
      );
    if (plan.errors.length)
      $("ownership-plan").append(el("p", plan.errors.join("；")));
    if (!plan.tweets && !plan.media_files && !plan.sync_runs)
      $("ownership-plan").append(el("p", "未归属库为空，无需重复迁移。"));
    $("ownership-confirm").textContent = "确认归属到 @" + account.slice(2);
    $("ownership-confirm").hidden = !plan.ready;
  } catch (error) {
    $("ownership-plan").textContent = error.message;
  }
});
$("ownership-confirm").addEventListener("click", async () => {
  if (!ownershipPlan) return;
  const plan = ownershipPlan;
  $("ownership-confirm").disabled = true;
  try {
    await api("/api/migrations", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-XBM-Action": "migrate",
      },
      body: JSON.stringify({ account: plan.account, token: plan.token }),
    });
    ownershipPlan = null;
    $("ownership-confirm").hidden = true;
    $("ownership-plan").textContent =
      "归属任务已提交。下方记录会自动更新，关闭页面不会取消任务。";
    ownershipHistory();
  } catch (error) {
    $("ownership-plan").textContent = error.message;
  } finally {
    $("ownership-confirm").disabled = false;
  }
});
setInterval(() => {
  if ($("ownership").open && !document.hidden) ownershipHistory();
}, 3000);

let diagnosticReport = null;
$("diagnostics-close").onclick = () => $("diagnostics").close();
$("diagnostics-open").onclick = async () => {
  $("diagnostics").showModal();
  $("diagnostics-result").textContent = "检查中…";
  $("diagnostics-export").disabled = true;
  diagnosticReport = null;
  try {
    diagnosticReport = await api("/api/diagnostics");
    $("diagnostics-result").textContent = "服务版本：" + diagnosticReport.version + "\n" +
      diagnosticReport.checks.map(c => (c.name === "media_storage" ? "媒体存储" : "数据库存储") + "：" + c.message + (c.free_bytes !== undefined ? " · 剩余 " + bytes(c.free_bytes) : "")).join("\n") + "\n" + diagnosticReport.scope;
    $("diagnostics-export").disabled = false;
  } catch (error) { $("diagnostics-result").textContent = "诊断失败：" + error.message; }
};
$("diagnostics-export").onclick = () => {
  if (!diagnosticReport) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(diagnosticReport, null, 2)], {type:"application/json"}));
  const link = document.createElement("a");link.href = url;link.download = "xbm-diagnostics.json";link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

function lockDashboard() {
  authenticated = false; authGeneration++;
  document.body.classList.add("locked");
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  items = []; selectedIds.clear(); generation++; accountsSignature = "";
  $("content").replaceChildren(); $("detail-body").replaceChildren();
  $("pair-result").textContent = ""; $("paired-clients").replaceChildren();
  $("login-dialog").showModal();
}
async function unlockDashboard() {
  authenticated = true; authGeneration++;
  $("admin-key").value = ""; $("login-message").textContent = "";
  $("login-dialog").close(); document.body.classList.remove("locked");
  try { await refreshAccounts(); navigate(); await status(); }
  catch (error) { notice(error.message); }
}
async function accessRequest(path, data) {
  const response = await fetch(path, {method:"POST", credentials:"same-origin", redirect:"error",
    headers:await dashboardHeaders({"Content-Type":"application/json", "X-XBM-Action":"access"}),
    body:JSON.stringify(data), signal:AbortSignal.timeout(10000)});
  const result = await response.json();
  if (!response.ok || !result.ok) {
    if (response.status === 401) lockDashboard();
    throw new Error(result.error || "连接失败，请重试");
  }
  return result;
}
async function bootstrapAccess() {
  try {
    const response = await fetch("/api/access/status", {cache:"no-store", signal:AbortSignal.timeout(10000)});
    const data = await response.json();
    if (data.authenticated && data.role === "admin") return unlockDashboard();
    lockDashboard();
  } catch (_) { lockDashboard(); $("login-message").textContent = "无法连接服务，请检查 NAS 后刷新页面。"; }
}
$("login-dialog").addEventListener("cancel", event => event.preventDefault());
$("login-form").addEventListener("submit", async event => {
  event.preventDefault(); $("login-submit").disabled = true;
  try {
    await accessRequest("/api/access/login", {key:$("admin-key").value.trim()});
    await unlockDashboard();
  } catch (error) { $("login-message").textContent = error.message; }
  finally { $("login-submit").disabled = false; $("admin-key").value = ""; }
});
async function refreshClients() {
  try {
    const data = await api("/api/access/clients");
    $("paired-clients").replaceChildren();
    if (!data.items.length) $("paired-clients").textContent = "尚未配对浏览器。";
    for (const item of data.items) {
      const row = el("div"), button = el("button", "撤销连接", "outline");
      row.append(el("span", item.name + " · " + date(item.created)), button);
      button.onclick = async () => {
        button.disabled = true;
        try { await accessRequest("/api/access/revoke", {id:item.id}); await refreshClients(); }
        catch (error) { $("pair-result").textContent = error.message; button.disabled = false; }
      };
      $("paired-clients").append(row);
    }
  } catch (error) { $("paired-clients").textContent = error.message; }
}
$("access-open").onclick = () => { $("pair-server").textContent = location.origin; $("pair-result").textContent = ""; $("access-dialog").showModal(); refreshClients(); };
$("access-close").onclick = () => { $("pair-result").textContent = ""; $("access-dialog").close(); };
$("access-refresh").onclick = refreshClients;
$("pair-create").onclick = async () => {
  $("pair-create").disabled = true;
  try {
    const result = await accessRequest("/api/access/pair-code", {});
    $("pair-result").textContent = result.code + " · 5 分钟内在扩展设置中使用";
  } catch (error) { $("pair-result").textContent = error.message; }
  finally { $("pair-create").disabled = false; }
};
$("logout").onclick = async () => {
  try { await accessRequest("/api/access/logout", {}); lockDashboard(); }
  catch (error) { $("pair-result").textContent = error.message; }
};

$('purge-selected').onclick = async () => {
  if ($('trash').value !== '1' || !selectedIds.size || loading) return;
  const ids = [...selectedIds];
  if (!await confirmAction(`彻底删除 ${ids.length} 条书签及其本地文件？此操作无法恢复，不影响 X 上的收藏。`)) return;
  try {
    const result = await api('/api/bookmarks/purge', {method:'POST',headers:{'Content-Type':'application/json','X-XBM-Action':'bookmarks'},body:JSON.stringify({ids})});
    notice(`已彻底删除 ${result.purged} 条；${result.cleanup_pending ? '部分文件等待清理，请查看存储管理。' : '文件清理已完成，共享引用文件会保留。'}`);
    await load(); await status();
  } catch (error) { notice(error.message); }
};
$('storage-close').onclick = () => $('storage-dialog').close();
$('storage-open').onclick = async () => {
  $('storage-dialog').showModal(); $('storage-result').textContent = '检查中…';
  try {
    const data = await api('/api/storage');
    $('download-mode').value = data.policy.mode;
    $('download-budget').value = data.policy.budget / (1024 ** 3);
    $('download-paused').checked = !!data.policy.paused;
    $('storage-result').textContent = `所选账户媒体记录：${bytes(data.media_bytes)} · 回收站 ${data.trash_count} 条 · 待清理文件 ${data.cleanup_pending} 个 · 存储卷剩余 ${bytes(data.free_bytes)} / ${bytes(data.total_bytes)}`;
  } catch (error) { $('storage-result').textContent = error.message; }
};

$('storage-policy-form').onsubmit = async event => {
  event.preventDefault();
  try {
    const result = await api('/api/storage/policy', {method:'POST',headers:{'Content-Type':'application/json','X-XBM-Action':'storage'},body:JSON.stringify({mode:$('download-mode').value,budget:Math.round(Number($('download-budget').value)*1024**3),paused:$('download-paused').checked})});
    $('storage-result').textContent = result.policy.budget_reached ? '已保存，媒体预算已达到，新任务暂停。' : '下载设置已保存。正在进行的任务不受影响。';
  } catch (error) { $('storage-result').textContent = error.message; }
};

let confirmationResolve = null;
function confirmAction(message) {
  if (confirmationResolve) return Promise.resolve(false);
  $('confirm-message').textContent = message;
  $('confirm-dialog').showModal();
  return new Promise(resolve => { confirmationResolve = resolve; });
}
function resolveConfirmation(value) {
  const resolve = confirmationResolve; confirmationResolve = null;
  $('confirm-dialog').close(); if(resolve) resolve(value);
}
$('confirm-yes').onclick = () => resolveConfirmation(true);
$('confirm-no').onclick = () => resolveConfirmation(false);
$('confirm-dialog').addEventListener('cancel', event => {event.preventDefault();resolveConfirmation(false);});
$('confirm-dialog').addEventListener('close', () => {if(confirmationResolve) resolveConfirmation(false);});
