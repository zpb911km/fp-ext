#!/usr/bin/env python3
"""Qwen（chat.qwen.ai）登录器 —— 打开浏览器登录，cookie 落盘到 <数据目录>/webai/qwen/cookie。

用法：
    python3 <fp数据目录>/public/webai/qwen_login.py                 # 有头，需要时自动账密登录
    python3 <fp数据目录>/public/webai/qwen_login.py --headless --wait 0
                                                                    # 静默：只试 profile 复用，不等人
产出：
    <数据目录>/qwen/profile   持久化浏览器 profile（登录态留在这里 → 静默刷新依赖它）
    <数据目录>/webai/qwen/cookie            **整条 cookie 串**（不是 JSON）：name=value; name=value; …

凭据来源（按优先级）：
    环境变量 QWEN_EMAIL / QWEN_PASSWORD
    <数据目录>/webai/qwen/credentials.json    {"email": "...", "password": "..."}   chmod 600
无凭据时会退化为"等人手动登录"。

⚠️ cookie 里的 token 是 JWT，有效期约 1 个月；过期重跑本脚本即可。
⚠️ 有头模式需要 X：无 DISPLAY 时默认用 :0（本机 Xorg 在 tty2）。

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

ROOT = DATA / "webai" / "qwen"
PROFILE = ROOT / "profile"
COOKIE_FILE = ROOT / "cookie"            # 整条 cookie 串（原 <数据目录>/webai/qwen/cookie）
CRED_FILE = ROOT / "credentials.json"    # 可选账密（原 <数据目录>/webai/qwen/credentials.json）
WAIT_SECONDS = 600
SILENT_SECONDS = 12
BASE = "https://chat.qwen.ai"
COOKIE_DOMAINS = ("chat.qwen.ai", ".qwen.ai", "qwen.ai")


def load_credentials() -> tuple[str, str]:
    """账密：环境变量优先，其次 <数据目录>/webai/qwen/credentials.json。"""
    email = os.environ.get("QWEN_EMAIL", "").strip()
    pwd = os.environ.get("QWEN_PASSWORD", "").strip()
    if not (email and pwd):
        try:
            j = json.loads(CRED_FILE.read_text())
            email = email or (j.get("email") or "").strip()
            pwd = pwd or (j.get("password") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    return email, pwd


def read_cookie(ctx) -> str:
    """整条 cookie 串（qwen 后端就是按这个字符串发的）。"""
    jar = [c for c in ctx.cookies()
           if any((c.get("domain") or "").endswith(d) for d in COOKIE_DOMAINS)]
    return "; ".join(f"{c['name']}={c['value']}" for c in jar)


def _looks_logged_in(page, cookie: str) -> bool:
    """判定是否已登录：不在 /auth 页，且 cookie 里带了 token。"""
    if "/auth" in (page.url or ""):
        return False
    return bool(cookie) and "token" in cookie.lower()


def _try_password_login(page, email: str, pwd: str) -> bool:
    """账密表单登录；页面结构变了就返回 False（交给人工）。"""
    try:
        page.goto(f"{BASE}/auth", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        page.fill("input[name='email']", email)
        page.fill("input[name='password']", pwd)
        page.click("button[type='submit']")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 账密自动登录未成功（{type(e).__name__}: {e}）—— 转人工")
        return False


def main(headless: bool = False, wait_seconds: int = WAIT_SECONDS) -> int:
    silent = wait_seconds <= 0
    PROFILE.mkdir(parents=True, exist_ok=True)
    if not headless and not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"       # 本机 Xorg 在 tty2

    email, pwd = load_credentials()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1300, "height": 880},
            locale="zh-CN",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        cookie = read_cookie(ctx)
        if _looks_logged_in(page, cookie):
            print(f"✅ 已是登录态（profile 复用成功），cookie {len(cookie)} 字符")
        else:
            # 先试静默 reuse（短轮询）
            if silent:
                deadline = time.time() + SILENT_SECONDS
                while not _looks_logged_in(page, read_cookie(ctx)) and time.time() < deadline:
                    time.sleep(3)
                cookie = read_cookie(ctx)
                if not _looks_logged_in(page, cookie):
                    print("❌ 静默模式：profile 里没有有效登录态（需要人工登录）")
                    ctx.close()
                    return 2
                print(f"✅ 已是登录态，cookie {len(cookie)} 字符")
            else:
                if email and pwd:
                    print("尝试用已保存的账密自动登录…")
                    _try_password_login(page, email, pwd)
                else:
                    print("⚠️ 未找到账密（QWEN_EMAIL/QWEN_PASSWORD 或 <数据目录>/webai/qwen/credentials.json），请手动登录。")
                print(f"等待登录完成，最多 {wait_seconds} 秒（可能有人机验证，请留意浏览器窗口）…")
                deadline = time.time() + wait_seconds
                ok = False
                while time.time() < deadline:
                    if _looks_logged_in(page, read_cookie(ctx)):
                        ok = True
                        break
                    if page.is_closed():
                        print("\n❌ 浏览器被关闭，未完成登录")
                        return 1
                    time.sleep(5)
                if not ok:
                    print("\n❌ 超时未检测到登录")
                    try:
                        page.screenshot(path="/tmp/qwen_login_fail.png")
                        print("   失败截图：/tmp/qwen_login_fail.png")
                    except Exception:  # noqa: BLE001
                        pass
                    ctx.close()
                    return 1
                cookie = read_cookie(ctx)
                print(f"\n✅ 登录成功，cookie {len(cookie)} 字符")

        COOKIE_FILE.write_text(cookie)
        COOKIE_FILE.chmod(0o600)
        print(f"cookie 已写入 {COOKIE_FILE}（{len(cookie)} 字符）")
        ctx.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Qwen 登录器")
    ap.add_argument("--headless", action="store_true", help="无头模式（静默刷新用）")
    ap.add_argument("--wait", type=int, default=WAIT_SECONDS,
                    help="等待人工登录的秒数；0=不等待（静默尝试）")
    a = ap.parse_args()
    sys.exit(main(headless=a.headless, wait_seconds=a.wait))
