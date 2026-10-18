"""命令行入口。

    xbm auth login [--from-browser chrome|firefox|edge|safari] [--manual]
    xbm auth check
    xbm sync [--full] [--max-pages N]      # 备用抓取器（twifork）
    xbm serve [--port 8765]                # 启动 Web 服务（含 /api/ingest）
    xbm export [--out bookmarks.jsonl]
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from . import auth, config as config_mod
from .db import Store
from .fetcher import run_sync


def _ctx(args):
    cfg = config_mod.load(getattr(args, "config", None))
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.state_dir / "xbm.db")
    return cfg, store


def cmd_auth_login(args) -> int:
    cfg, store = _ctx(args)
    store.close()
    state_dir = cfg.state_dir
    try:
        if args.from_browser:
            print(f"从本机 {args.from_browser} 提取 X 登录 cookie…")
            auth.login_from_browser(args.from_browser, state_dir)
        elif args.manual:
            at = getpass.getpass("auth_token（粘贴后回车，输入不可见）: ")
            ct0 = getpass.getpass("ct0（粘贴后回车，输入不可见）: ")
            if not at.strip() or not ct0.strip():
                print("auth_token / ct0 不能为空")
                return 2
            auth.login_manual(at, ct0, state_dir)
        else:
            print("程序化登录（密码只进内存，不落盘）。")
            username = input("X 用户名: ").strip()
            email = input("邮箱（可空，回车跳过）: ").strip() or None
            password = getpass.getpass("密码: ")
            totp = getpass.getpass("TOTP 密钥（无 2FA 直接回车）: ").strip() or None
            if not username or not password:
                print("用户名和密码不能为空")
                return 2
            asyncio.run(auth.login_programmatic(username, email, password,
                                                state_dir, totp))
        ok = auth.check_login_sync(state_dir)
        print(f"cookies 已保存到 {state_dir}，登录自检：{'通过' if ok else '失败'}")
        return 0 if ok else 1
    except Exception as e:
        print(f"登录失败：{e}")
        return 1


def cmd_auth_check(args) -> int:
    cfg, store = _ctx(args)
    store.close()
    try:
        ok = auth.check_login_sync(cfg.state_dir)
    except FileNotFoundError as e:
        print(e)
        return 2
    except Exception as e:
        print(f"自检异常：{e}")
        return 1
    print("cookie 有效" if ok else "cookie 已过期，请重新 `xbm auth login`")
    return 0 if ok else 1


def cmd_sync(args) -> int:
    cfg, store = _ctx(args)
    try:
        ok = auth.check_login_sync(cfg.state_dir)
    except FileNotFoundError as e:
        print(e)
        store.close()
        return 2
    if not ok:
        print("cookie 已过期，请重新 `xbm auth login`")
        store.close()
        return 1

    client = auth.make_client(cfg.state_dir)
    max_pages = args.max_pages if args.max_pages else cfg.max_pages
    run_id = store.start_run()
    try:
        found = asyncio.run(run_sync(client, store, cfg.page_size,
                                     max_pages, cfg.request_delay))
    except Exception as e:
        store.finish_run(run_id, 0, status="failed", error=str(e)[:500])
        fails = store.consecutive_failures()
        print(f"同步失败：{e}（连续失败 {fails} 次）")
        if fails >= cfg.fail_alert_threshold:
            print(f"已达告警阈值（{cfg.fail_alert_threshold}），请检查 notify 配置")
        store.close()
        return 1

    store.finish_run(run_id, len(found))
    store.close()
    if not found:
        print("没有新书签。")
        return 0
    print(f"新增 {len(found)} 条书签：")
    for info in found:
        preview = info["text"].replace("\n", " ")[:80]
        print(f"- [{info['created_at']}] @{info['screen_name']} "
              f"({info['media_count']} 媒体): {preview} "
              f"[id={info['tweet_id']}]")
    return 0


def cmd_export(args) -> int:
    cfg, store = _ctx(args)
    out = Path(args.out or "bookmarks.jsonl")
    rows = store._conn.execute(
        "SELECT tweet_id, author, screen_name, text, created_at, synced_at "
        "FROM tweets ORDER BY rowid"
    ).fetchall()
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
    store.close()
    print(f"已导出 {len(rows)} 条到 {out}")
    return 0


def cmd_serve(args) -> int:
    from . import server as server_mod
    cfg, store = _ctx(args)
    store.close()  # server 会自己建 Store
    server_mod.run(cfg, port=args.port)
    return 0


def cmd_migrate(args) -> int:
    # Use the running service: it coordinates downloads, ingestion and journal locks.
    import urllib.request
    from urllib.parse import urlencode
    try:
        headers = {'Content-Type':'application/json', 'X-XBM-Action':'migrate'}
        if args.key_file:
            headers['Authorization'] = 'Bearer ' + Path(args.key_file).read_text().strip()
        if args.rollback:
            request = urllib.request.Request(args.server.rstrip('/') + '/api/migrations/' + args.rollback + '/rollback',
                data=b'{}', headers=headers)
            with urllib.request.urlopen(request,timeout=30) as response:
                print(json.dumps(json.load(response),ensure_ascii=False))
            return 0
        if not args.account:
            raise ValueError('请指定 --account 或 --rollback')
        account = 'x:' + args.account.lstrip('@').removeprefix('x:').lower()
        base = args.server.rstrip('/')
        with urllib.request.urlopen(urllib.request.Request(base + '/api/migrations/preview?' + urlencode({'target': account}), headers=headers), timeout=30) as response:
            plan = json.load(response)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if not args.apply:
            return 0
        if not plan.get('ready'):
            print('迁移条件未满足，请处理预览中的提示。')
            return 1
        request = urllib.request.Request(base + '/api/migrations',
            data=json.dumps({'account': account, 'token': plan['token']}).encode(),
            headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            print(json.dumps(json.load(response), ensure_ascii=False))
        return 0
    except Exception as error:
        print('迁移请求失败：' + str(error))
        return 1


def cmd_access_key(args) -> int:
    from .access import Access
    cfg = config_mod.load(args.config)
    access = Access(cfg.state_dir)
    if args.reset:
        access.reset()
        print('已撤销全部连接并更新管理密钥。请用新密钥登录并重新配对扩展。')
    print(access.key_path.read_text().strip())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xbm", description="同步 X 书签到 NAS 本地")
    p.add_argument("--config", help="config.toml 路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    pk = sub.add_parser('access-key', help='本机读取管理密钥；不要分享输出')
    pk.add_argument('--reset', action='store_true', help='更新管理密钥并撤销全部登录/配对')
    pk.set_defaults(func=cmd_access_key)

    pa = sub.add_parser("auth", help="登录与自检")
    pa_sub = pa.add_subparsers(dest="auth_cmd", required=True)
    pl = pa_sub.add_parser("login", help="登录（默认程序化登录）")
    pl.add_argument("--from-browser",
                    help="从本机浏览器提取 cookie（chrome/firefox/edge/safari）")
    pl.add_argument("--manual", action="store_true", help="手动粘贴 auth_token + ct0")
    pl.set_defaults(func=cmd_auth_login)
    pc = pa_sub.add_parser("check", help="cookie 有效性自检")
    pc.set_defaults(func=cmd_auth_check)

    ps = sub.add_parser("sync", help="增量同步书签")
    ps.add_argument("--full", action="store_true", help="首次全量回填（配合 --max-pages）")
    ps.add_argument("--max-pages", type=int, default=0, help="本轮最大翻页数（默认用配置）")
    ps.set_defaults(func=cmd_sync)

    pe = sub.add_parser("export", help="导出书签索引 JSONL")
    pe.add_argument("--out", default="bookmarks.jsonl")
    pe.set_defaults(func=cmd_export)

    pv = sub.add_parser("serve", help="启动 Web 服务（含 /api/ingest）")
    pv.add_argument("--port", type=int, default=8765)
    pv.set_defaults(func=cmd_serve)
    pm = sub.add_parser('migrate-legacy', help='预览旧数据归属；--apply 发起备份和迁移')
    pm.add_argument('--account', help='已由插件识别的 @用户名')
    pm.add_argument('--server', default='http://127.0.0.1:8765')
    pm.add_argument('--rollback', help='撤销指定任务；有新数据时拒绝覆盖')
    pm.add_argument('--apply', action='store_true')
    pm.add_argument('--key-file', help='此目标 NAS 的管理密钥文件；不会自动发送本机密钥')
    pm.set_defaults(func=cmd_migrate)
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
