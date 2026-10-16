# xbm — X 书签同步到 NAS

把 X（Twitter）书签里的推文自动同步到 NAS 本地：图片、视频、长文章全文。

- 路线：**Chrome 扩展被动拦截 + 服务端下载**（不使用付费 API；仍受登录、限流和页面改版影响）
- 详细设计见 [DESIGN.md](DESIGN.md)

## 快速开始

### 服务端（NAS / 本机）

```bash
# 1. 建虚拟环境（Python 3.11+）
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

# 2. 配置
cp config.example.toml config.toml
# 按需改 data.root（NAS 上的落地目录）

# 3. 启动服务（含 POST /api/ingest 去重入库）
xbm serve --port 8765
```

### Chrome 扩展

1. Chrome 打开 `chrome://extensions`，打开开发者模式 → 「加载已解压的扩展程序」→ 选择 `extension/` 目录。
2. 点扩展图标 → 设置服务端地址（如 `http://192.168.1.10:8765`）→ 「测试连接」。
3. 在 x.com 登录 X 账号（哪个号登录就同步哪个号的书签，无需小号）。
4. 打开 X 书签页（历史页面请选择“书签”），点「立即同步书签」；之后每小时从已有书签页采集，不再自动新建或关闭标签页。

详见 [extension/README.md](extension/README.md)。

## 目录结构

```text
extension/
  manifest.json / injected.js  # MAIN world：hook fetch/XHR，捕获书签 GraphQL
  content.js                   # 自动滚动采集 + 数据转发
  background.js                # 定时采集、推送服务端、失败本地队列
  popup.html/js / options.html/js
src/xbm/
  server.py       # Web 服务 + POST /api/ingest（去重入库、CORS）
  cli.py          # 命令行入口（serve / 备用抓取器调试）
  config.py       # config.toml 加载
  db.py           # sqlite：去重集合 / watermark / 运行日志
  auth.py         # （备用）twifork 登录与 cookie 管理
  fetcher.py      # （备用）twifork 书签增量抓取
  downloader.py   # （M2）CDN 直链下载图片/视频
  articles.py     # （M3）长文章全文 → Markdown
  notify.py       # 失败告警（Bark/Telegram/日志）
```

## 运维要点

- 当前部署形态：独立 Python HTTP 服务，可由 systemd 常驻运行；0.3.0 内置 Web 管理界面，飞牛入口包连接已部署的服务。
- 扩展与服务端走同一份 ingest 协议（`POST /api/ingest`），采集器可替换；备选采集器见 DESIGN.md §2.2（Headless 浏览器）。
- 媒体下载走 CDN 签名直链，免登录、无 cookie 维护。

## 0.2.0 本地改进

- 扩展先持久保存再分批投递；NAS 不通时独立补传，弹窗显示待传数量。
- 登录失效保留页面并暂停自动采集；历史回填有次数上限，不再宣称一定滚到底。
- 服务端事务提交元数据与下载任务；响应丢失可安全重传。
- 当前 API 无独立鉴权，仅适用于可信私有网络。
- 产品设计、边界与后续工作见 [DESIGN.md](DESIGN.md)。

验证命令：

```sh
python -m unittest discover -s tests -v
node --test tests/extension.test.cjs
```

## NAS 管理界面（0.3.0）

启动服务后打开 `http://NAS地址:8765/`：

- **全部书签**：搜索正文、作者、用户名，按媒体类型筛选，分页浏览，查看正文、原文链接与本地已下载的图片/视频。
- **同步记录**：显示 NAS 收到的批次，含接收时间、采集时间、来源、收到/新增/重复数量和媒体入队数。一次插件同步可能拆成多个批次；插件尚未送达的数据不会出现在这里。旧记录缺失的计数显示 `—`。
- **下载队列**：已传输字节、总大小、速度、百分比、重试次数、退避时间与错误原因，每 3 秒更新；可立即重试失败任务。HLS/总大小未知的任务只显示状态，不伪造百分比。

界面无需外部 CDN 或构建工具，随 Python 包分发。数据库升级自动补列并保留原数据；升级生产服务前请备份 `.xbm/xbm.db`。

### 飞牛桌面入口包

`packaging/fnos/` 是连接既有 `xbm.service` 的入口包，并不是独立安装器；先部署 Python 服务，再运行 `fnpack build -d packaging/fnos`，安装生成的 `x-bookmark-sync.fpk`。桌面显示“X 书签库”，点击打开本机 8765 端口；应用中心启停操作对应 systemd 服务。卸载入口包保留项目和书签数据。

入口依赖 NAS 地址可直连 8765 端口，不通过飞牛登录网关鉴权；仅在原有可信私有网络使用。CLI 启动方式仍可用。
