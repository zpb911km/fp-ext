#!/usr/bin/env python3
"""stepfun 免浏览器续期 —— **真·过期**端到端验证（慢，手动跑）
================================================================================

为什么需要它（与其他测试的分工）：

* 离线测试（`test_login_layer.py` §6）用替身验证**逻辑**：临期预判 / 401 重试 /
  游客态必须拒绝 / 异常必须留痕。它们**证明不了**"服务端在 access 真过期时会
  用 refresh 段发放新 access"——那是服务端的契约，只能实测。
* `check_verify_live.py` 验证**坏凭据会被识破**（负向）。
* 本脚本验证**好凭据过期后能自愈**，且**不开浏览器**（用 profile 的 mtime 证明）。

做法：读 `Oasis-Token` 的 access 段 exp → 等它**自然过期**（≈30 分钟，不碰凭据）
→ 走真实工具路径（`tools/ask_llm.execute`）→ 断言三件事：

    1. 调用成功（没有"鉴权失败"）；
    2. stderr 里**没有** `login.silent_refresh` 的痕迹 —— 说明自愈在 provider 内闭环，
       根本没轮到开浏览器；
    3. profile 里没有任何文件在调用期间被写过。

跑法（会占约 30 分钟，建议挂着）::

    cd <fp数据目录>/public/plugins/webai/lib && python3 tests/check_renew_live.py --yes

只测其它环节、不等过期::

    python3 tests/check_renew_live.py --yes --now      # 立刻用当前凭据打一发（应成功）

返回码：0=全部断言通过 / 1=有断言失败 / 2=缺 --yes / 3=前置条件不满足
"""

import argparse
import base64
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.dirname(HERE)                         # …/webai/lib
PLUGINS = os.path.dirname(os.path.dirname(LIB))     # …/public/plugins（`import webai` 的根）

try:
    from fp_core.platform_utils import get_data_dir
    DATA = pathlib.Path(get_data_dir())
except Exception:                                   # noqa: BLE001
    DATA = pathlib.Path.home() / ".local" / "share" / "fp"

COOKIE_FILE = DATA / "webai" / "stepfun" / "cookies.json"
PROFILE = DATA / "webai" / "stepfun" / "profile"


def _log(*a):
    print(time.strftime("[%F %T]"), *a, flush=True)


def _access_exp() -> int:
    tok = json.loads(COOKIE_FILE.read_text())["Oasis-Token"].split("...")[0]
    seg = tok.split(".")[1]
    seg += "=" * (-len(seg) % 4)
    return int(json.loads(base64.urlsafe_b64decode(seg))["exp"])


def _newest_mtime() -> float:
    return max((p.stat().st_mtime for p in PROFILE.rglob("*") if p.is_file()), default=0.0)


def _run_tool() -> tuple[bool, str]:
    """走真实工具路径，返回 (是否成功, stderr)。"""
    code = (
        "import sys, asyncio\n"
        f"sys.path.insert(0, {PLUGINS!r})\n"
        "from webai.tools import ask_llm as T\n"
        'out = asyncio.run(T.execute({"keywords": "一句话说明什么是光合作用", "provider": "stepfun"}))\n'
        'print("TOOL_SUCCESS:", not out.startswith("联网搜索失败"))\n'
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    ok = "TOOL_SUCCESS: True" in p.stdout
    _log("工具输出：", (p.stdout.strip().splitlines() or ["(空)"])[0])
    return ok, p.stderr


def main() -> int:
    ap = argparse.ArgumentParser(description="stepfun 续期实测（会等 access 自然过期）")
    ap.add_argument("--yes", action="store_true", help="确认执行（会等待约 30 分钟）")
    ap.add_argument("--now", action="store_true", help="不等过期，立刻打一发")
    a = ap.parse_args()
    if not a.yes:
        print(__doc__)
        print("加上 --yes 才会真的执行。")
        return 2

    if not COOKIE_FILE.exists():
        _log("❌ 没有凭据文件：", COOKIE_FILE, "—— 先跑 lib/stepfun_login.py")
        return 3
    if not PROFILE.exists():
        _log("⚠️ 没有 profile，无法判定「是否开了浏览器」；断言 3 会被跳过")

    if not a.now:
        exp = _access_exp()
        if exp <= time.time():
            _log("⚠️ access 已过期（", time.strftime("%T", time.localtime(exp)), "）→ 无需等待")
        else:
            _log("access exp =", time.strftime("%T", time.localtime(exp)),
                 f"（{(exp - time.time()) / 60:.0f} 分钟后）→ 等它自然过期，期间不碰凭据")
            while time.time() < exp + 60:
                time.sleep(20)
            _log("已自然过期 ✓")

    before = _newest_mtime()
    _log("调用前 profile 最新文件 mtime =", time.strftime("%T", time.localtime(before)))
    t0 = time.time()
    ok, err = _run_tool()

    fails = []
    if not ok:
        fails.append("工具调用失败（没有自愈成功）")
    if "silent_refresh" in err or "凭据已刷新" in err or "凭据疑似过期" in err:
        fails.append("自愈打到了 login 层（说明 provider 内的续期没生效 → 走了浏览器）")
    after = _newest_mtime()
    if after > t0:
        fails.append(f"profile 在调用期间被写过（{time.strftime('%T', time.localtime(after))}）"
                     " → 开了浏览器")
    _log("调用后 profile 最新文件 mtime =", time.strftime("%T", time.localtime(after)))
    _log("新 access exp =", time.strftime("%T", time.localtime(_access_exp())))

    for m in fails:
        print("  ✗", m)
    if fails:
        print("❌ 有断言失败")
    elif a.now:
        print("✅ 调用路径通过（--now 未等过期，只验证了「调用成功且没开浏览器」）")
    else:
        print("✅ 续期实测通过：过期 → 静默自愈 → 未开浏览器")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
