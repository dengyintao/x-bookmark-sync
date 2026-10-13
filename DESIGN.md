# X 书签同步工具 · 设计文档

> 目标：把 X（Twitter）书签里的推文自动同步到 NAS 本地：图片、视频（含 HLS）、长文章全文。
> 路线：纯免费（twifork 抓书签 + gallery-dl 下媒体），独立工具，不依赖 meizang。
> Python 3.11（与团队 PyArmor 环境一致）。

## 1. 总体架构

```text
                    ┌──────────────────┐
                    │  Chrome 扩展(MV3) │
                    │  注入页面上下文    │
                    │  hook fetch/XHR   │
                    │  捕获书签 GraphQL  │
                    └────────┬─────────┘
                             │ POST /api/ingest
                             ▼
┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│  ingestion API   │─────▶│  downloader      │─────▶│  store           │
│  按tweet_id去重   │      │  CDN直链直接下载  │      │  NAS目录 + sqlite │
│  入库            │      │  （免 cookie）    │      │                  │
└──────────────────┘      └──────────────────┘      └──────────────────┘
```

核心原则：**增量、幂等、可断点续传**。任何一步失败都不污染已同步数据，下一轮自动补上。

> 架构说明：采集层原为 twifork 主动轮询（见 git 历史），2026-10-13 起切换为 Chrome 扩展被动拦截。
> 服务端（ingestion/store/downloader/画廊 UI/fpk）80% 设计保留，只是采集方式换了；
> 已实现的 twifork 抓取器（`src/xbm/fetcher.py`）保留为**备用抓取器**。

## 2. 模块设计

### 2.1 auth —— 认证（插件路线下大幅简化）

插件路线用**浏览器现有登录态**，服务端不再需要 X cookie：

- 扩展跑在用户自己的 Chrome 里，拦截的是 X 页面自己收到的数据——登录态就是用户当前登录的号。**哪个号登录 Chrome，就同步哪个号的书签**，小号/main 两难不存在了。
- 服务端下载媒体用 CDN 签名直链（`pbs.twimg.com` / `video.twimg.com`），免登录，无需 cookie。
- 因此：**无 cookie 过期维护、无程序化登录、无密码处理**。`auth.py` 的三种登录方式降级为备用抓取器专用（见 git 历史）。

### 2.2 采集：Chrome 扩展（MV3）+ ingestion API

**原理**（被动拦截，与 rainux/TBD 同路线）：扩展向 x.com 页面注入脚本（`chrome.scripting.executeScript`，`world: 'MAIN'`），hook `window.fetch` / `XMLHttpRequest`，把 X 前端自己收到的 Bookmarks GraphQL 响应经 `postMessage` 转发给 content script，再由 background service worker POST 到服务端 `/api/ingest`。反机器人 token 由 X 自己的 JS 生成，**天然免疫反爬**（无 queryId 轮换、无 transaction-id 伪造问题）。

**扩展行为**：

- 定时（`chrome.alarms`，每小时）：自动打开 `x.com/i/bookmarks`（后台/pinned tab），滚动加载——增量只需首屏 1–2 页，首次回填模式滚到底；捕获后关 tab。
- 被动：用户平时手动刷书签页时也实时捕获。
- 推送内容：tweet_id、文本、作者、时间、媒体 URL 列表（含视频 variants）、外链、是否含 article。

**服务端 `/api/ingest`**：

- 按 tweet_id 去重（已见即跳过），新推文入库 tweets 表，媒体 URL 入下载队列。
- 幂等：重复推送无副作用；断点续传天然成立（下次推送补上）。

**依赖**：某台机器（Mac/PC）的 Chrome 保持运行并登录 X；扩展 options 页配置服务端地址（如 `http://NAS-IP:端口` 或 fnConnect 域名）。

**备选采集器：Headless 浏览器（Playwright，常驻 NAS 端）**

- 原理相同（真浏览器 + X 自身 JS 生成 token，免疫反爬），只是把"用户的桌面 Chrome"换成 NAS 上常驻的 headless Chromium：Playwright 定时打开 `x.com/i/bookmarks`，用 `page.on('response')` 拦截 GraphQL 响应，解析后 POST 到 `/api/ingest`。与扩展是同一份 ingest 协议，可并存。
- 代价：① **cookie 维护回归**——headless 也要登录态，NAS 无显示器，首次登录需在 PC 上登好后把 `auth_token`/`ct0` 注入（`context.add_cookies`），并处理过期；② **吃资源**——Chromium headless 约 300–500MB 内存 + ~170MB 浏览器二进制 + 一堆系统依赖（`playwright install-deps` 走 apt）。
- 结论：扩展能覆盖时不做；只有当"必须摆脱桌面 Chrome"成为刚需时再加。M1/M2 不做。

**代码位置**：`extension/` 目录（manifest.json、injected.js、content.js、background.js、options 页），M1 实现。

### 2.3 downloader —— 媒体下载（CDN 直链，免 cookie）✅ M2 已实现

- 扩展捕获到的媒体 URL 是 CDN 签名直链（`pbs.twimg.com` 图片 / `video.twimg.com` 视频），**免登录可直接下载**——服务端用 httpx 分块下载即可，无需 cookie。
- 选址：`downloader.pick_best()` —— 图片转原图（`name=orig`）；视频/GIF 选 variants 里码率最高的 MP4；仅 HLS（m3u8）时走 ffmpeg remux，gallery-dl 兜底。
- 后台队列：`xbm serve` 启动 N 个 worker 线程（`[download] workers`，默认 2），原子领取 `media_files` 中 pending/可重试任务；失败指数退避（1m→1h），超 `max_retries`（默认 5）记永久 failed。
- 断点续传：`.part` 临时文件 + HTTP Range；已有完整文件直接标记 done（幂等）。
- 落盘：`{download.dir || data.root/media}/{tweet_id}/{tweet_id}_{序号}.{ext}`；媒体齐后写 `meta.json`（推文元数据 + 媒体清单）。下载目录可独立配置（`[download] dir`，如指向另一块盘）。
- 状态：`GET /api/status` 返回 `media{pending,downloading,done,failed}` + `media_dir`。

### 2.4 articles —— 长文章全文

- 官方 API 取不到全文，只能走登录态：
  1. 优先 twifork/twikit 的 article 接口（若当前版本可用）；
  2. 备选直接打 `TweetDetail` GraphQL（带 article 字段开关）；
  3. 兜底 Playwright 真浏览器方案（bookmark-eXport 思路），只做手动 fallback，不进自动流程。
- 存为 Markdown：标题、作者、发布时间、原文链接、正文。
- 推文 flag：`has_article`，文章失败不阻塞媒体同步，下一轮重试。

### 2.5 store —— 存储

目录结构（`$DATA_ROOT` 可配置，如飞牛上的 `/vol1/x-bookmarks`）：

```text
$DATA_ROOT/
  2026/10/<tweet_id>/
    meta.json       # 推文元数据
    article.md      # 有长文章才有
    <tweet_id>_p1.jpg
    <tweet_id>_video.mp4
  .xbm/
    xbm.db          # sqlite：去重集合、watermark、运行日志
    cookies.json    # twifork 格式（600 权限）
    cookies.txt     # Netscape 格式，给 gallery-dl
```

`meta.json` 示例字段：`tweet_id, text, created_at, author{screen_name,name}, urls[], media[{type,local_path,remote_url}], article{has_article,md_path}, bookmark_index_hint`。

sqlite 三张表：`tweets(tweet_id PK, author, text, created_at, synced_at)`、`media_files(tweet_id, kind, local_path, bytes)`、`sync_runs(started_at, new_count, status, error)`。watermark 取 `tweets` 里最新 `tweet_id` 即可，不另建表。

### 2.6 调度与部署（fpk 原生应用，带 Web UI）

**形态**：打成飞牛 fpk 原生应用（复用 meizang 的 `fnpack` 打包模板），常驻服务形态：
`Web 服务（常驻）+ 同步引擎（后台线程，每小时触发）+ SQLite`。

- 入口复用 meizang 模式：`/app/xbm` 统一网关 + Unix Socket，不暴露 TCP 端口。
- `run-as=package` 专用用户运行；数据库/PID/日志放 `TRIM_PKGVAR`（升级不丢）；媒体目录走飞牛目录授权（`TRIM_DATA_ACCESSIBLE_PATHS`），沿用 meizang 的路径校验思路。
- CLI 保留，用于无头调试与手动运维。

**备选（无 UI 纯脚本）**：SSH + cron，venv/cron/代码放数据卷 + `deploy.sh` 一键重装；或社区 `fn-scheduledtask` fpk 图形化管理。见 git 历史中的早期方案。

**通用**

- 同步引擎单实例锁，防止上一轮没跑完又起一轮。
- 健康检查：连续失败 N 次（默认 3）→ webhook 告警。告警通道可配：Bark / Server酱 / Telegram Bot，默认只打日志。
- 日志按天轮转，保留 30 天。
- 网络：服务端从 `pbs.twimg.com` / `video.twimg.com` 下载媒体，国内需走代理；在 `config.toml` 的 `[proxy]` 节配置 `url`（如 `http://127.0.0.1:7890`），留空则直连。应用级代理，不碰系统代理（与 meizang 的代理设计思路一致）。

## 3. CLI 设计

```text
xbm auth login                        # 程序化登录（交互式输账号密码，密码不落盘）
xbm auth login --from-browser chrome   # 从本机 Chrome 自动提取 cookie（免密码）
xbm auth login --manual                # 手动粘贴 auth_token + ct0（兜底）
xbm auth check          # is_logged_in 自检
xbm sync                # 增量同步（默认）
xbm sync --full --max-pages 50   # 首次全量回填
xbm export              # 导出书签索引（JSONL，可选）
```

## 4. 依赖与版本策略

```toml
requires-python = ">=3.11"
dependencies = ["twifork==x.y.z", "gallery-dl>=1.26"]  # twifork 必须 pin 死版本
```

- **twifork 升级流程**：X 每 2–4 周改一次反爬，社区靠补丁跟进。升级前先在测试环境跑 `xbm auth check && xbm sync` smoke test，通过再升生产。禁止生产环境自动 `pip install -U`。
- 系统依赖：ffmpeg。

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| X 改反爬导致 twifork 失效 | pin 版本 + 升级前 smoke test + 失败告警；终极兜底 Playwright |
| 账号被限流/锁定 | 用小号；每小时低频轮询；失败即停不轰炸 |
| cookie 过期（几周~几个月一次） | 每轮 `is_logged_in` 自检，过期告警，人工重新 `xbm auth login` |
| 违反 X ToS（灰色地带） | 个人自用、低频、无商业用途；风险自担 |
| 书签量极大（>几千） | `--max-pages` 限流 + 多轮渐进回填，不追求一轮跑完 |

## 6. MVP 里程碑

- **M1**：Chrome 扩展 MVP（拦截书签 GraphQL + POST 推送）+ 服务端 `/api/ingest`（去重入库）；`xbm ingest --mock` 本地调通。
- **M2**：downloader（CDN 直链下载 + ffmpeg HLS），图片/视频落盘，断点续传验证。
- **M3**：文章全文（扩展用页面会话代抓 TweetDetail，或用户点开文章时捕获）→ Markdown。
- **M4**：Web UI（总览/同步/画廊/设置 4 个页面，认证页取消）+ fpk 打包（复用 meizang 模板）+ 失败告警。

## 7. 待确认

1. 小号是否已有？（没有的话先注册养一下再跑）
2. 告警通道偏好：Bark / Telegram / 邮件 / 暂时只要日志？
3. NAS 上 Docker 是否可用？数据目录打算放哪？

## 8. GitHub 类似工具调研（2026-10-13）

结论：有相关项目，但**没有开箱即用的完整方案**；以下模块值得借鉴：

| 项目 | 简介 | 状态 | 可借鉴点 |
|---|---|---|---|
| [rainux/twitter-bookmarks-downloader](https://github.com/rainux/twitter-bookmarks-downloader) | Go 后端 daemon + Tampermonkey 用户脚本**被动拦截**浏览器收到的 X 数据；SQLite 去重、媒体后台下载、文件 mtime 设为推文发布时间 | 22★，46 commits，MIT，roadmap 中 | SQLite 去重表设计、mtime=推文时间的思路；但它需手动打开书签页滚动，非纯无人值守，且无文章全文 |
| [chrism/x-bookmarks-md](https://github.com/chrism/x-bookmarks-md) | Node.js：书签→Markdown + gallery-dl 下媒体，`npm run update` 增量更新（遇到已知 tweet_id 即停） | 约一年未更新，可能已失效 | 增量 watermark 思路与我们的设计一致，是现成的验证 |
| [pauljump/bookmark-eXport](https://github.com/pauljump/bookmark-eXport) | Playwright 真浏览器监听 GraphQL，输出 JSONL，含**完整 thread 和 X Article 全文**（Draft.js content_state 解码） | 新项目，MIT | 文章全文的抓取/解码逻辑可直接参考移植；缺点是不下载媒体文件 |
| [sankabuilds/xer](https://github.com/sankabuilds/xer) | Rust CLI，一条命令下书签媒体（`xer x bookmarks --all`），有预编译二进制 | 新项目 0★，MIT | 只是个下载器，无元数据/增量/文章；证明"书签页直下"这条路走得通 |
| [SPhillips1337/x-bookmarks-export](https://github.com/SPhillips1337/x-bookmarks-export) | Node.js，官方 API OAuth2 路线 | 需开发者账号（现付费） | 不符合免费路线，仅作对比 |

借鉴策略：媒体下载层直接用 gallery-dl（xer 也是这个思路）；文章模块参考 bookmark-eXport 的解码逻辑；其余按本设计自研。

## 9. Web UI 与 fpk 应用设计

**形态决策**：打成飞牛 fpk 原生应用（复用 meizang 的 `fnpack` 打包模板）。
理由：① 明确要 Web/飞牛应用界面；② 团队已有 meizang 的打包模板与发布流程，边际成本低；③ 常驻服务形态天然适合"内置定时器 + Web UI"，比 cron 更易做状态展示与手动触发。

### 9.1 应用架构

```text
fpk (run-as=package, install_dep_apps=python312)
├── Web 服务（常驻，/app/xbm 网关 + Unix Socket，不暴露 TCP）
│   ├── REST API（§9.3）
│   └── Web UI（§9.2，5 个页面）
├── 同步引擎（后台线程，每小时触发；复用 fetcher/downloader/articles）
└── SQLite（去重/watermark/运行日志，存 TRIM_PKGVAR，升级不丢）
```

### 9.2 UI 页面（MVP，插件路线下 4 个页面——认证页取消）

1. **总览**：同步状态（上次成功时间、新增数、下次运行）、扩展连接状态、失败告警横幅。
2. **同步**：手动触发本轮同步（让扩展立即刷一次）、首次回填、运行日志流式查看、历史运行记录。
3. **画廊**：已同步书签按时间/作者浏览，图片视频缩略图预览，文章 Markdown 阅读视图，关键词搜索。
4. **设置**：存储目录（飞牛目录授权）、扩展推送地址、轮询间隔、告警通道（Bark/Telegram）、代理配置。

（认证页取消：登录态完全由浏览器持有，服务端不再管 X 登录。）

### 9.3 REST API（草案）

- `GET /api/status` → 上次运行、下次运行、统计、cookie 状态
- `POST /api/auth/login` {username, password, totp?} → 程序化登录（密码不存储）
- `GET /api/auth/status`
- `POST /api/sync/run` / `POST /api/sync/backfill` {max_pages}
- `GET /api/sync/runs` / `GET /api/sync/log?run_id=`
- `GET /api/bookmarks?q=&author=&page=` / `GET /api/bookmarks/:id`
- `GET/PUT /api/settings`

### 9.4 技术选型建议

- 后端：Python，与 meizang 同风格的轻量 HTTP 服务（stdlib 或 Flask 二选一，不引入重型框架）。
- 前端：MVP 服务端渲染 + 少量 JS，保证可交付；后续再考虑 SPA。
- 鉴权：MVP 依赖飞牛桌面登录（内网使用）；如需对外暴露，再移植 meizang 的登录体系（本地账号/NAS 网关免登）。

### 9.5 打包要点（复用 meizang 模板）

- `manifest`：`platform=all`（纯 Python），`install_dep_apps=python312`，`run-as=package`。
- 图标、wizard、`config/privilege`、`config/resource` 按 meizang 结构照抄改名。
- 数据库/PID/日志放 `TRIM_PKGVAR`；媒体目录走目录授权 + 路径校验。
- 发布前验证：安装/启动/停止/重启/升级/保留数据卸载（同 meizang 清单）。
