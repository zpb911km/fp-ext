#!/usr/bin/env python3
"""StepFun（阶跃AI）登录器 —— 打开浏览器让你扫码，登录态自动落盘。

用法：
    python3 <fp数据目录>/public/plugins/webai/lib/stepfun_login.py              # 有头，需要时等你登录
    python3 <fp数据目录>/public/plugins/webai/lib/stepfun_login.py --headless --wait 0
                                                                    # 静默：只试 profile 复用，不等人
产出：
    <数据目录>/stepfun/profile     持久化浏览器 profile（扫码一次后可复用）
    <数据目录>/stepfun/cookies.json  cookie（webai 的 stepfun 后端读它）

支持的登录方式：微信扫码 / 手机号（页面上用哪个都行）。

返回码：0=已登录并落盘 / 2=未检测到登录（静默模式，未等人工）/ 1=出错
（统一入口见同目录 login.py —— 一般不用直接跑本脚本）
"""
import argparse
import json
import os
import pathlib
import sys
import time

from playwright.sync_api import sync_playwright

try:
    from fp_core.platform_utils import get_data_dir
    DATA = pathlib.Path(get_data_dir())
except Exception:  # noqa: BLE001
    DATA = pathlib.Path.home() / ".local" / "share" / "fp"

ROOT = DATA / "webai" / "stepfun"
PROFILE = ROOT / "profile"
COOKIE_FILE = ROOT / "cookies.json"
WAIT_SECONDS = 600
SILENT_SECONDS = 12
BASE = "https://chat.stepfun.com"


def read_auth_cookie(ctx) -> dict:
    """取 chat.stepfun.com 的 cookie；Oasis-Token 是登录态标志（游客没有）"""
    jar = {}
    for c in ctx.cookies():
        if "stepfun" in (c.get("domain") or ""):
            jar[c["name"]] = c["value"]
    tok = jar.get("Oasis-Token") or ""
    # 游客占位/空值判空；真实 token 是 "JWT...JWT" 两段
    return jar if (len(tok) > 80 and tok.count(".") >= 3) else {}


def main(headless: bool = False, wait_seconds: int = WAIT_SECONDS) -> int:
    silent = wait_seconds <= 0
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1300, "height": 880},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/chats/new", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        if not silent:
            print("请在打开的浏览器里登录（微信扫码 / 手机号均可）…")
            print(f"最多等待 {wait_seconds} 秒。")

        limit = SILENT_SECONDS if silent else wait_seconds
        deadline = time.time() + limit
        jar = read_auth_cookie(ctx)
        while not jar and time.time() < deadline:
            try:
                if page.is_closed():
                    print("❌ 浏览器被关闭，未完成登录")
                    return 1
            except Exception:  # noqa: BLE001
                return 1
            time.sleep(3)
            jar = read_auth_cookie(ctx)

        if not jar:
            print("❌ 静默模式：profile 里没有有效登录态（需要人工扫码）" if silent
                  else "❌ 超时未检测到登录")
            ctx.close()
            return 2 if silent else 1

        print(f"✅ 已登录，cookie {len(jar)} 项"
              + ("（profile 复用成功）" if not silent else ""))
        ROOT.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(json.dumps(jar, ensure_ascii=False))
        COOKIE_FILE.chmod(0o600)
        print(f"cookie 已写入 {COOKIE_FILE}")
        ctx.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="StepFun 登录器")
    ap.add_argument("--headless", action="store_true", help="无头模式（静默刷新用）")
    ap.add_argument("--wait", type=int, default=WAIT_SECONDS,
                    help="等待人工登录的秒数；0=不等待（静默尝试）")
    a = ap.parse_args()
    sys.exit(main(headless=a.headless, wait_seconds=a.wait))
