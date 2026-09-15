#!/usr/bin/env python3
"""StepFun（阶跃AI）登录器 —— 打开浏览器让你扫码，登录态自动落盘。

用法：
    python3 <fp数据目录>/public/webai/stepfun_login.py

产出：
    <数据目录>/stepfun/profile     持久化浏览器 profile（扫码一次后可复用）
    <数据目录>/stepfun/cookies.json  cookie（stepfun provider 读它）

支持的登录方式：微信扫码 / 手机号（页面上用哪个都行）。
"""
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

ROOT = DATA / "stepfun"
PROFILE = ROOT / "profile"
COOKIE_FILE = ROOT / "cookies.json"
WAIT_SECONDS = 600
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


def main() -> int:
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1300, "height": 880},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/chats/new", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        jar = read_auth_cookie(ctx)
        if jar:
            print(f"✅ 已是登录态（profile 复用成功），cookie {len(jar)} 项")
        else:
            print("请在打开的浏览器里登录（微信扫码 / 手机号均可）…")
            print(f"最多等待 {WAIT_SECONDS} 秒。")
            deadline = time.time() + WAIT_SECONDS
            while time.time() < deadline:
                jar = read_auth_cookie(ctx)
                if jar:
                    print(f"\n✅ 登录成功，cookie {len(jar)} 项")
                    break
                try:
                    if page.is_closed():
                        print("\n❌ 浏览器被关闭，未完成登录")
                        return 1
                except Exception:  # noqa: BLE001
                    return 1
                time.sleep(3)
            if not jar:
                print("\n❌ 超时未检测到登录")
                ctx.close()
                return 1

        ROOT.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(json.dumps(jar, ensure_ascii=False))
        COOKIE_FILE.chmod(0o600)
        print(f"cookie 已写入 {COOKIE_FILE}")
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
