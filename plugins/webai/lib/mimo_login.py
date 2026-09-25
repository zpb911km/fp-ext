#!/usr/bin/env python3
"""MiMo（小米 MiMo AI Studio）登录器 —— 打开浏览器让你登录，登录态自动落盘。

用法：
    python3 <fp数据目录>/public/plugins/webai/lib/mimo_login.py              # 有头，需要时等你登录
    python3 <fp数据目录>/public/plugins/webai/lib/mimo_login.py --headless --wait 0
                                                                    # 静默：只试 profile 复用，不等人
产出：
    <数据目录>/mimo/profile     持久化浏览器 profile（登录一次后可复用）
    <数据目录>/mimo/cookies.json  cookie（webai 的 mimo 后端读它）

登录态判定：不看 cookie 名（小米 SSO 的 serviceToken 名字可能变），
直接在页面上下文调 GET /open-apis/user/mi/get —— 拿到 code=0 即登录成功。

返回码：0=已登录并落盘 / 2=未检测到登录（静默模式，未等人工）/ 1=出错
（统一入口见同目录 login.py —— 一般不用直接跑本脚本）
"""
import argparse
import json
import pathlib
import sys
import time

from playwright.sync_api import sync_playwright

try:
    from fp_core.platform_utils import get_data_dir
    DATA = pathlib.Path(get_data_dir())
except Exception:  # noqa: BLE001
    DATA = pathlib.Path.home() / ".local" / "share" / "fp"

ROOT = DATA / "webai" / "mimo"
PROFILE = ROOT / "profile"
COOKIE_FILE = ROOT / "cookies.json"
WAIT_SECONDS = 600
SILENT_SECONDS = 12
BASE = "https://aistudio.xiaomimimo.com"


def _logged_in(page) -> bool:
    """页面上下文内探测登录态（比猜 cookie 名可靠）。"""
    try:
        return bool(page.evaluate("""async () => {
            try {
                const r = await fetch('/open-apis/user/mi/get', {credentials: 'same-origin'});
                if (r.status !== 200) return false;
                const j = await r.json();
                return j && (j.code === 0 || j.code === 200);
            } catch (e) { return false; }
        }"""))
    except Exception:  # noqa: BLE001
        return False


def read_cookies(ctx) -> dict:
    """取 aistudio.xiaomimimo.com 及小米 SSO 域的 cookie。"""
    jar = {}
    for c in ctx.cookies():
        d = c.get("domain") or ""
        if "xiaomimimo.com" in d or d.endswith(".xiaomi.com") or d.endswith(".mi.com"):
            jar[c["name"]] = c["value"]
    return jar


def _save(jar: dict) -> None:
    """落盘（原子替换）。复用 provider 的 save_cookies —— 写盘只有一处实现。"""
    try:
        import mimo
        if mimo.save_cookies(jar):
            return
    except Exception:  # noqa: BLE001
        pass
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(json.dumps(jar, ensure_ascii=False))
    COOKIE_FILE.chmod(0o600)


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
        page.goto(f"{BASE}/#/c", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)          # SPA 渲染 + 可能的 SSO 跳转

        if not silent:
            print("请在打开的浏览器里登录（小米账号：手机验证码 / 扫码均可）…")
            print(f"最多等待 {wait_seconds} 秒。")

        limit = SILENT_SECONDS if silent else wait_seconds
        deadline = time.time() + limit
        ok = _logged_in(page)
        while not ok and time.time() < deadline:
            try:
                if page.is_closed():
                    print("❌ 浏览器被关闭，未完成登录")
                    return 1
            except Exception:  # noqa: BLE001
                return 1
            time.sleep(3)
            ok = _logged_in(page)

        if not ok:
            print("❌ 静默模式：profile 里没有有效登录态（需要人工登录）" if silent
                  else "❌ 超时未检测到登录")
            ctx.close()
            return 2 if silent else 1

        jar = read_cookies(ctx)
        if not jar:
            print("❌ 登录态确认但读不到 cookie（异常）")
            ctx.close()
            return 1
        print(f"✅ 已登录，cookie {len(jar)} 项"
              + ("（profile 复用成功）" if not silent else ""))
        ROOT.mkdir(parents=True, exist_ok=True)
        _save(jar)
        print(f"cookie 已写入 {COOKIE_FILE}")
        ctx.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="MiMo 登录器")
    ap.add_argument("--headless", action="store_true", help="无头模式（静默刷新用）")
    ap.add_argument("--wait", type=int, default=WAIT_SECONDS,
                    help="等待人工登录的秒数；0=不等待（静默尝试）")
    a = ap.parse_args()
    sys.exit(main(headless=a.headless, wait_seconds=a.wait))
