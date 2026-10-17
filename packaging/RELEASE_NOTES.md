X Bookmark Sync 0.4.1：飞牛独立安装包。

- FPK 内置 Python 服务、Web 管理界面、gallery-dl 和 FFmpeg；安装无需下载运行依赖。
- 应用中心独立启停，以专用应用用户运行，不再依赖 xbm.service。
- 配置、数据库、媒体和迁移备份独立于程序目录；升级保留数据。
- CI 验证冻结后的程序、安装/启动/停止/升级/卸载流程，以及空数据 HTTP 界面。

附件：`x-bookmark-sync-0.4.1.fpk`、`xbm-0.4.1-py3-none-any.whl`、`x-bookmark-extension-0.2.5.zip`、`xbm-0.4.1-source.tar.gz` 和 `SHA256SUMS`。

支持 x86_64 fnOS 1.2.0401+。在应用中心手动安装 FPK，启动后点击“X 书签库”。服务端口为 8765，请仅在可信私有网络使用。

旧入口包或手工部署升级：备份原配置及数据，停止原 xbm.service 释放端口，再安装新包。不会自动移动或接管旧数据；按 README 复制数据并授予应用用户权限，或配置可访问的旧路径。卸载时选择保留应用数据。

Python wheel 单独使用仍需要 Python 3.11+；插件解压后在 Chromium 开发者模式加载。
