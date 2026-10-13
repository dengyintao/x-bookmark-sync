# xbm — X 书签同步到 NAS

把 X（Twitter）书签里的推文自动同步到 NAS 本地：图片、视频、长文章全文。

- 路线：纯免费（twifork 抓书签 + gallery-dl 下媒体），$0
- 详细设计见 [DESIGN.md](DESIGN.md)

## 快速开始

```bash
# 1. 建虚拟环境（Python 3.11）
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e .

# 2. 系统依赖
# Debian/Ubuntu: apt install ffmpeg

# 3. 配置
cp config.example.toml config.toml
# 按需改 data.root

# 4. 登录（⚠️ 建议小号，权衡见 DESIGN.md §2.1；二选一）
# 方式一：程序化登录，交互式输账号密码（密码不落盘）
xbm auth login
# 方式二：本机 Chrome 已登录小号，直接自动提取 cookie（连密码都不用给）
xbm auth login --from-browser chrome
xbm auth check

# 5. 首次回填 + 之后每小时增量（cron）
xbm sync --full --max-pages 50
xbm sync
```

## 目录结构

```text
src/xbm/
  cli.py          # 命令行入口（无头调试/运维）
  server.py       # Web 服务 + REST API（fpk 常驻，M4）
  web/            # 前端模板与静态资源（M4）
  config.py       # config.toml 加载
  db.py           # sqlite：去重集合 / watermark / 运行日志
  auth.py         # cookie 存取、is_logged_in 自检、导出 cookies.txt
  fetcher.py      # twifork 书签增量抓取
  downloader.py   # gallery-dl 子进程下载图片/视频
  articles.py     # 长文章全文 → Markdown
  notify.py       # 失败告警（Bark/Telegram/日志）
```

## 运维要点

- 部署形态：飞牛 fpk 原生应用（复用 meizang 打包模板），带 Web UI，见 DESIGN.md §9。
- twifork 版本已 pin 死，升级前先在测试环境跑 `xbm auth check && xbm sync`。
- cookie 过期（几周~几个月一次）时 `xbm auth check` 会失败并告警，重新 `xbm auth login` 即可。
- Docker 部署见 `Dockerfile`（M4）。
