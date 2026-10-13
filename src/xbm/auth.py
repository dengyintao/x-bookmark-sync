"""认证与 Cookie 管理。

X 没有扫码登录，此模块提供三种获取登录信息的方式（用户永不碰 DevTools）：

1. 程序化登录（默认）：`xbm auth login`
   交互式输入用户名/邮箱/密码（getpass，密码不落盘），调
   twifork `client.login(auth_info_1, auth_info_2, password, cookies_file=...)`
   自动走完登录流程。若 X 弹验证挑战，可传 totp_secret 或走方式 2。
2. 浏览器提取：`xbm auth login --from-browser chrome`
   从本机已登录的浏览器 cookie jar 自动提取 auth_token + ct0
   （需 `pip install browser-cookie3`），用户连密码都不用给。
3. 兜底：`xbm auth login --manual` 手动粘贴两个 cookie。

cookies.json（twifork 原生格式）权限 600；另导出 Netscape 格式
cookies.txt 供 gallery-dl --cookies 使用。
每次同步前 is_logged_in() 自检；失败则告警并退出，不重试。

⚠️ 必须用小号，绝不用主号。
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

# twifork 的导入名仍是 twikit（drop-in 替换，注意 pip 包名和导入名不一致）
import twikit

COOKIES_JSON = "cookies.json"
COOKIES_TXT = "cookies.txt"


def _json_path(state_dir: Path) -> Path:
    return state_dir / COOKIES_JSON


def _txt_path(state_dir: Path) -> Path:
    return state_dir / COOKIES_TXT


def save_cookies(cookies: dict[str, Any], state_dir: Path) -> Path:
    """保存 twifork 原生格式 cookies.json，权限 600。"""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _json_path(state_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    export_netscape(cookies, state_dir)
    return path


def load_cookies(state_dir: Path) -> dict[str, Any]:
    path = _json_path(state_dir)
    if not path.is_file():
        raise FileNotFoundError(f"cookies 不存在：{path}，请先运行 `xbm auth login`")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def export_netscape(cookies: dict[str, Any], state_dir: Path) -> Path:
    """导出 Netscape 格式 cookies.txt 供 gallery-dl --cookies 使用。"""
    path = _txt_path(state_dir)
    lines = ["# Netscape HTTP Cookie File", ""]
    # 远期过期时间（会话 cookie 本无过期；gallery-dl 只读值）
    expiry = "9999999999"
    for name, value in cookies.items():
        if not value:
            continue
        lines.append("\t".join([".x.com", "TRUE", "/", "TRUE", expiry, name, str(value)]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _new_client(language: str = "zh-CN") -> twikit.Client:
    return twikit.Client(language)


async def login_programmatic(
    username: str,
    email: str | None,
    password: str,
    state_dir: Path,
    totp_secret: str | None = None,
) -> dict[str, Any]:
    """程序化登录：走完 X 登录流程并保存 cookie。密码只进内存，不落盘。"""
    client = _new_client()
    cookies = await client.login(
        auth_info_1=username,
        auth_info_2=email,
        password=password,
        totp_secret=totp_secret,
        cookies_file=str(_json_path(state_dir)),
    )
    # login() 已按 cookies_file 保存；统一再走一遍 save_cookies 保证权限 + 导出 txt
    save_cookies(cookies, state_dir)
    return cookies


def login_from_browser(browser: str, state_dir: Path) -> dict[str, Any]:
    """从本机已登录的浏览器提取 auth_token + ct0。"""
    try:
        import browser_cookie3
    except ImportError as e:
        raise RuntimeError(
            "需要先安装 browser-cookie3：pip install browser-cookie3"
        ) from e

    loader = {
        "chrome": browser_cookie3.chrome,
        "firefox": browser_cookie3.firefox,
        "edge": browser_cookie3.edge,
        "safari": browser_cookie3.safari,
    }.get(browser.lower())
    if loader is None:
        raise ValueError(f"不支持的浏览器：{browser}（支持 chrome/firefox/edge/safari）")

    jar = loader(domain_name="x.com")
    cookies = {c.name: c.value for c in jar}
    auth_token = cookies.get("auth_token")
    ct0 = cookies.get("ct0")
    if not auth_token or not ct0:
        raise RuntimeError(
            f"在 {browser} 中没有找到有效的 X 登录 cookie，请确认浏览器已登录 X 小号"
        )
    save_cookies(cookies, state_dir)
    return cookies


def login_manual(auth_token: str, ct0: str, state_dir: Path) -> dict[str, Any]:
    """手动粘贴两个 cookie（兜底）。"""
    cookies = {"auth_token": auth_token.strip(), "ct0": ct0.strip()}
    save_cookies(cookies, state_dir)
    return cookies


async def check_login(state_dir: Path) -> bool:
    """is_logged_in 自检。False = cookie 过期，需重新 `xbm auth login`。"""
    cookies = load_cookies(state_dir)
    client = _new_client()
    client.set_cookies(cookies)
    return await client.is_logged_in()


def make_client(state_dir: Path) -> twikit.Client:
    """已登录的 client，供 fetcher 使用。"""
    cookies = load_cookies(state_dir)
    client = _new_client()
    client.set_cookies(cookies)
    return client


def check_login_sync(state_dir: Path) -> bool:
    return asyncio.run(check_login(state_dir))
