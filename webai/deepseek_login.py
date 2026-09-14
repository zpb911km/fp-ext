#!/usr/bin/env python3
"""DeepSeek 登录器 —— 打开浏览器让你登录，登录态与 token 自动落盘。

用法：
    python3 ~/.local/share/fp/private/tools/extensions/deepseek_login.py

产出：
    ~/.local/share/fp/deepseek/profile   持久化浏览器 profile（登录态留在这里，可复用）
    ~/.deepseek_token                    Bearer token（copilot 的 deepseek 后端读它）

⚠️ token 来自 localStorage 的 userToken，是 JSON：{"value":"...","__version":"0"}，取 .value。
   未登录时它也有值但为 {"value":null} 游客占位符 —— 必须校验长度，否则会误判"已登录"。
"""
import json
import os
import pathlib
import time

from playwright.sync_api import sync_playwright

PROFILE = pathlib.Path(os.path.expanduser("~/.local/share/fp/deepseek/profile"))
TOKEN_FILE = pathlib.Path(os.path.expanduser("~/.deepseek_token"))
WAIT_SECONDS = 600


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


def main() -> int:
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")

        tok = read_token(page)
        if tok:
            print(f"✅ 已处于登录态（profile 复用成功），token 长度 {len(tok)}")
        else:
            print("请在打开的浏览器窗口中登录 DeepSeek（手机号 / 微信扫码均可）…")
            print(f"最多等待 {WAIT_SECONDS} 秒。")
            deadline = time.time() + WAIT_SECONDS
            while time.time() < deadline:
                tok = read_token(page)
                if tok:
                    print(f"\n✅ 登录成功，token 长度 {len(tok)}")
                    break
                if page.is_closed():
                    print("\n❌ 浏览器被关闭，未完成登录")
                    return 1
                time.sleep(3)
            if not tok:
                print("\n❌ 超时未检测到登录")
                ctx.close()
                return 1

        TOKEN_FILE.write_text(tok)
        TOKEN_FILE.chmod(0o600)
        print(f"token 已写入 {TOKEN_FILE}")
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
