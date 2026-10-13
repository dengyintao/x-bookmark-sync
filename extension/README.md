# 扩展安装说明（Chrome / Edge）

1. 打开 `chrome://extensions`，右上角打开**开发者模式**。
2. 点击「加载已解压的扩展程序」，选择本目录（`extension/`）。
3. 点击扩展图标 → 「设置服务端地址」，填 NAS 上 `xbm serve` 的地址，
   例如 `http://192.168.1.10:8765`，点「测试连接」确认。
4. 在 x.com 登录你的 X 账号（哪个号登录就同步哪个号的书签）。
5. 之后每小时自动采集一次；也可以点扩展图标 → 「立即同步书签」手动触发；
   首次全量回填用「全量回填（滚到底）」。

## 文件说明

| 文件 | 说明 |
|---|---|
| `manifest.json` | MV3 清单 |
| `injected.js` | MAIN world：hook fetch/XHR，捕获书签 GraphQL，只读监听 |
| `content.js` | ISOLATED world：转发数据 + 自动滚动采集 |
| `background.js` | service worker：定时任务、推送服务端、失败队列 |
| `popup.html/js` | 扩展弹窗：手动同步/回填 |
| `options.html/js` | 设置页：服务端地址、采集间隔 |

## 隐私说明

- 扩展只读取 X 书签页的数据，不修改、不发送推文/点赞/取关等任何写操作。
- 捕获的数据只 POST 到你自己配置的服务端地址，不经过任何第三方。
