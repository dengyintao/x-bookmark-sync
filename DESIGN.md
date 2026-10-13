# X 书签同步工具 · 设计文档

> 目标：把 X（Twitter）书签里的推文自动同步到 NAS 本地：图片、视频（含 HLS）、长文章全文。
> 路线：纯免费（twifork 抓书签 + gallery-dl 下媒体），独立工具，不依赖 meizang。
> Python 3.11（与团队 PyArmor 环境一致）。

## 1. 总体架构

```text
                    ┌─────────────────┐
                    │   scheduler     │  cron / Docker，每小时一轮
                    └────────┬────────┘
                             ▼
┌──────────┐      ┌──────────────────┐      ┌──────────────────┐
│  auth    │─────▶│  fetcher         │─────▶│  downloader      │
│ cookie   │      │  twifork         │      │  gallery-dl      │
│ 自检     │      │  书签增量拉取     │      │  图片/视频落盘    │
└──────────┘      └────────┬─────────┘      └────────┬─────────┘
                           │                       │
                           ▼                       ▼
                    ┌──────────────────┐   ┌──────────────────┐
                    │  articles        │   │  store           │
                    │  长文章→Markdown  │   │  NAS目录 + sqlite │
                    └──────────────────┘   └──────────────────┘
```

核心原则：**增量、幂等、可断点续传**。任何一步失败都不污染已同步数据，下一轮自动补上。

## 2. 模块设计

### 2.1 auth —— 认证与 Cookie 管理

> 说明：X 官方没有扫码登录（二维码仅用于 2FA 验证器绑定），所以"扫码自动登录"此路不通。
> 改为下面两种**自动获取登录信息**的方式，目标都是：用户永不碰 DevTools。

**方式一：程序化登录（默认，`xbm auth login`）**

- 交互式输入用户名、邮箱、密码（`getpass` 即时输入，**密码不落盘**）。
- 调用 twifork `await client.login(auth_info_1=用户名, auth_info_2=邮箱, password=密码, cookies_file='.xbm/cookies.json')`，自动走完 X 登录流程并保存 cookie。
- 之后所有轮询都从 `cookies.json` 恢复，不再需要密码。
- 注意：若 X 弹出"验证身份"（邮箱验证码等），程序化登录可能卡住，此时降级用方式二。

**方式二：从本机浏览器自动提取（`xbm auth login --from-browser chrome`）**

- 用户在自己电脑的 Chrome/Firefox 里正常登录 X 小号一次（正常过验证，成功率 100%）。
- 工具用 gallery-dl/yt-dlp 同款机制（`--cookies-from-browser`）自动读取浏览器 cookie jar，提取 `auth_token` + `ct0`。
- 用户连密码都不用给工具。首次在有浏览器的机器（Mac/PC）上跑一次，把 `.xbm/` 拷到 NAS 即可。
- 兜底：`xbm auth login --manual` 手动粘贴两个 cookie（原 DevTools 方案保留）。

**通用规则**

- 只需要两个 cookie：`auth_token`、`ct0`；`cookies.json`（twifork 原生格式）权限 600。
- 自动导出 Netscape 格式 `cookies.txt` 供 gallery-dl 使用（`gallery-dl --cookies`）。
- 每次同步前先 `await client.is_logged_in()` 自检；失败则发告警并直接退出，**不重试、不空转**（避免无效请求触发风控）。
- ⚠️ 必须用**小号**登录，绝不用主号。这是整套方案里唯一不可妥协的风控要求。

### 2.2 fetcher —— 书签增量抓取（twifork）

- 依赖：`pip install twifork`，代码里 `import twikit`（drop-in 替换，包名和导入名不一样注意）。
- 初始化：`Client('zh-CN')` → `set_cookies({...})` 或 `load_cookies('cookies.json')`。
- 分页：`await client.get_bookmarks(count=20)` 按 cursor 翻页。
- **增量逻辑**（书签接口无 `since_id`、无收藏时间戳，只能客户端自己记）：
  - sqlite 存 `seen_tweet_ids` 集合 + `watermark`（最新一条 tweet id）；
  - 每轮从第一页开始翻，遇到已见 id 即停止；
  - 单轮翻页上限可配置（默认 10 页），防止某次书签暴增时跑太久。
- **限速**：请求间隔 ≥ 1.5s + 随机 jitter；每小时一轮；新书签少时每轮只有 1–2 个请求，账号行为几乎不可见。
- 提取字段：`tweet.id`、`text`、`created_at`、`user.screen_name`、展开后的外链 urls、media 列表（photos / videos / video_info.variants）、是否引用 article、card 信息。
- 视频直链：优先选 variants 里码率最高的渐进式 mp4；HLS-only 的不自己处理，丢给 gallery-dl。

### 2.3 downloader —— 媒体下载（gallery-dl）

- 以**子进程**方式调用（比当库用更稳定、升级互不影响）：
  `gallery-dl --cookies cookies.txt -d <dest_dir> <tweet_url>`
- 配置要点：`extractor.twitter.videos: true`；用 `--cookies-from-browser` 可免手动导出（NAS 无桌面浏览器时用 cookies.txt 方案）。
- 文件命名：`<tweet_id>_p1.jpg` / `<tweet_id>_video.mp4` —— 按 tweet_id 命名天然去重，已存在直接跳过。
- HLS（m3u8）视频：gallery-dl 自动拉 ts 分片 + ffmpeg remux 成 mp4，**NAS/容器里必须装 ffmpeg**。
- 音频：X 的语音推文/Space 本质也是 HLS，走同一链路；Space 直播回放可用 twifork 的 spaces 接口拿 HLS 地址（二期再做）。

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
- 网络：twifork/gallery-dl 需直连 x.com，NAS 上按需走代理（参考 meizang 的代理配置思路，或直接用系统环境变量 `https_proxy`）。

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

- **M1**：auth + fetcher 跑通，`xbm sync` 能打印新增书签列表（不下载）。
- **M2**：接 gallery-dl，图片/视频落盘，断点续传验证。
- **M3**：文章全文 → Markdown。
- **M4**：Web UI（总览/认证/同步/画廊/设置 5 个页面）+ fpk 打包（复用 meizang 模板）+ 失败告警。

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

### 9.2 UI 页面（MVP）

1. **总览**：同步状态（上次成功时间、新增数、下次运行）、cookie 有效性、失败告警横幅。
2. **认证**：X 小号登录表单（用户名/密码/TOTP 可选）→ 程序化登录；cookie 状态展示；手动粘贴兜底。**密码只进内存，不落盘、不记日志。**
3. **同步**：手动触发本轮同步、首次回填（可设页数）、运行日志流式查看、历史运行记录。
4. **画廊**：已同步书签按时间/作者浏览，图片视频缩略图预览，文章 Markdown 阅读视图，关键词搜索。
5. **设置**：存储目录（飞牛目录授权）、轮询间隔、告警通道（Bark/Telegram）、代理配置。

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
