X Bookmark Sync 0.4.0：个人 X 书签同步与 NAS 管理。

- NAS 书签搜索、详情、同步记录、下载进度与失败重试。
- 按登录的 X @用户名分库存储；待传数据绑定捕获时的账户。
- 正式旧数据归属：预览、备份、去重、校验、失败回滚、重启恢复与安全撤销。
- Chrome/Edge 扩展 0.2.5：复用已有书签页，离线队列补传，登录账户识别。

### 下载与安装

- `x-bookmark-sync-0.4.0.fpk`：**飞牛桌面入口包**。需要先部署并运行 `xbm.service`；不是包含 Python 服务的独立安装器。支持 x86 fnOS 1.2.0401 及以上。
- `xbm-0.4.0-py3-none-any.whl`：Python 服务端（Python 3.11+），安装后配置 `config.toml`，运行 `xbm serve --port 8765`。
- `x-bookmark-extension-0.2.5.zip`：解压后在 Chrome/Edge 开发者模式加载目录；升级后刷新 X 页面。
- `xbm-0.4.0-source.tar.gz`：包含源码、测试、FPK 配置与构建脚本。
- `SHA256SUMS`：下载文件校验值，Linux 可运行 `sha256sum --check SHA256SUMS`。

升级前备份书签数据库。已有账户数据继续保留；账户分类不等于访问鉴权，本服务仅用于可信私有网络。完整配置与安装步骤见 README。
