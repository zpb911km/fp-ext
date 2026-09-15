#!/usr/bin/env python3
"""GLM（智谱清言 chatglm.cn）登录器 —— 打开浏览器让你登录，凭据自动落盘。

用法：
    python3 <fp数据目录>/public/webai/glm_login.py

产出：
    <数据目录>/glm/profile      持久化浏览器 profile（登录一次可复用）
    <数据目录>/glm/cookies.json {cookies: {...}, device_id: "..."}

登录方式：手机号 / 微信扫码（页面上用哪个都行）。
凭据在 cookie 里：chatglm_token(Bearer) / chatglm_refresh_token / chatglm_user_id。
"""
import json
import pathlib
import sys
import time
import uuid

from playwright.sync_api import sync_playwright

try:
    from fp_core.platform_utils import get_data_dir
    DATA = pathlib.Path(get_data_dir())
except Exception:  # noqa: BLE001
    DATA = pathlib.Path.home() / ".local" / "share" / "fp"

ROOT = DATA / "glm"
PROFILE = ROOT / "profile"
CRED_FILE = ROOT / "cookies.json"
WAIT_SECONDS = 600
BASE = "https://chatglm.cn"


def read_creds(ctx) -> dict:
    """取 chatglm.cn 的 cookie。chatglm_token 是登录态标志（游客没有）。"""
    jar = {}
    for c in ctx.cookies():
        if "chatglm" in (c.get("domain") or ""):
            jar[c["name"]] = c["value"]
    tok = jar.get("chatglm_token") or ""
    if len(tok) < 60:      # 游客/空值判空
        return {}
    return jar


def main() -> int:
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1300, "height": 880},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        jar = read_creds(ctx)
        if jar:
            print(f"✅ 已是登录态（profile 复用成功），cookie {len(jar)} 项")
        else:
            print("请在打开的浏览器里登录智谱清言（手机号 / 微信扫码均可）…")
            print(f"最多等待 {WAIT_SECONDS} 秒。")
            deadline = time.time() + WAIT_SECONDS
            while time.time() < deadline:
                jar = read_creds(ctx)
                if jar:
                    print(f"\n✅ 登录成功，cookie {len(jar)} 项")
                    break
                if page.is_closed():
                    print("\n❌ 浏览器被关闭，未完成登录")
                    return 1
                time.sleep(3)
            if not jar:
                print("\n❌ 超时未检测到登录")
                ctx.close()
                return 1

        # device_id：沿用已存的，没有就生成一个（服务端只要求存在且稳定）
        old = {}
        try:
            old = json.loads(CRED_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
        device_id = old.get("device_id") or uuid.uuid4().hex

        ROOT.mkdir(parents=True, exist_ok=True)
        CRED_FILE.write_text(json.dumps(
            {"cookies": jar, "device_id": device_id}, ensure_ascii=False))
        CRED_FILE.chmod(0o600)
        print(f"凭据已写入 {CRED_FILE}（cookie {len(jar)} 项，device_id 已保留）")
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
