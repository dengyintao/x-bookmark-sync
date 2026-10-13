# xbm — X 书签同步到 NAS

把 X（Twitter）书签里的推文自动同步到 NAS 本地：图片、视频、长文章全文。

- 路线：**Chrome 扩展被动拦截 + 服务端下载**（$0，天然免疫反爬）
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
4. 点「立即同步书签」测试；之后每小时自动采集一次。

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

- 部署形态：飞牛 fpk 原生应用（复用 meizang 打包模板），带 Web UI，见 DESIGN.md §9。
- 扩展与服务端走同一份 ingest 协议（`POST /api/ingest`），采集器可替换；备选采集器见 DESIGN.md §2.2（Headless 浏览器）。
- 媒体下载走 CDN 签名直链，免登录、无 cookie 维护。
