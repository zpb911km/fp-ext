#!/usr/bin/env python3
"""DeepSeek 登录器 —— 打开浏览器让你登录，登录态与 token 自动落盘。

用法：
    python3 <fp数据目录>/public/webai/deepseek_login.py             # 有头，需要时等你登录
    python3 <fp数据目录>/public/webai/deepseek_login.py --headless --wait 0
                                                                    # 静默：只试 profile 复用，不等人
产出：
    <数据目录>/deepseek/profile   持久化浏览器 profile（登录态留在这里，可复用）
    ~/.deepseek_token             Bearer token（webai 的 deepseek 后端读它）

⚠️ token 来自 localStorage 的 userToken，是 JSON：{"value":"...","__version":"0"}，取 .value。
   未登录时它也有值但为 {"value":null} 游客占位符 —— 必须校验长度，否则会误判"已登录"。

返回码：0=已登录并落盘 / 2=未检测到登录（静默模式，未等人工）/ 1=出错
（统一入口见同目录 login.py —— 一般不用直接跑本脚本）
"""
import argparse
import json
import os
import pathlib
import time

from playwright.sync_api import sync_playwright

PROFILE = pathlib.Path(os.path.expanduser("~/.local/share/fp/deepseek/profile"))
TOKEN_FILE = pathlib.Path(os.path.expanduser("~/.deepseek_token"))
WAIT_SECONDS = 600
SILENT_SECONDS = 12          # 静默模式下的短轮询：覆盖页面加载慢的情况


def read_token(page) -> str:
    """从 localStorage 取有效 token；游客占位符返回空串"""
    try:
        raw = page.evaluate(
            "() => { try { return localStorage.getItem('userToken'); } catch (e) { return null; } }"
        )
    except Exception:
        return ""
    if not raw:
        return ""
    tok = raw
    if raw.startswith("{"):
        try:
            tok = (json.loads(raw) or {}).get("value") or ""
        except json.JSONDecodeError:
            return ""
    return tok if isinstance(tok, str) and len(tok) > 40 else ""


def main(headless: bool = False, wait_seconds: int = WAIT_SECONDS) -> int:
    silent = wait_seconds <= 0
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")

        if not silent:
            print("请在打开的浏览器窗口中登录 DeepSeek（手机号 / 微信扫码均可）…")
            print(f"最多等待 {wait_seconds} 秒。")

        # 统一轮询：静默模式短轮询(避开"页面还没加载完"的假阴性)，交互模式长等待
        limit = SILENT_SECONDS if silent else wait_seconds
        deadline = time.time() + limit
        tok = read_token(page)
        while not tok and time.time() < deadline:
            if page.is_closed():
                print("❌ 浏览器被关闭，未完成登录")
                return 1
            time.sleep(3)
            tok = read_token(page)

        if not tok:
            print("❌ 静默模式：profile 里没有有效登录态（需要人工登录）" if silent
                  else "❌ 超时未检测到登录")
            ctx.close()
            return 2 if silent else 1

        print(f"✅ 已登录，token 长度 {len(tok)}"
              + ("（profile 复用成功）" if not silent else ""))
        TOKEN_FILE.write_text(tok)
        TOKEN_FILE.chmod(0o600)
        print(f"token 已写入 {TOKEN_FILE}")
        ctx.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DeepSeek 登录器")
    ap.add_argument("--headless", action="store_true", help="无头模式（静默刷新用）")
    ap.add_argument("--wait", type=int, default=WAIT_SECONDS,
                    help="等待人工登录的秒数；0=不等待（静默尝试）")
    a = ap.parse_args()
    raise SystemExit(main(headless=a.headless, wait_seconds=a.wait))
