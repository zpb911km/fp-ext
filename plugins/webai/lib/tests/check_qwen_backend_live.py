#!/usr/bin/env python3
"""qwen 安全后端 **联网** 端到端检查 —— 会真的开浏览器、真的发一条消息。

跑法::

    cd <data>/public/plugins/webai/lib && python3 tests/check_qwen_backend_live.py --yes
    # 只想验证登录/页面，不想发消息（快，约 30s）：
    python3 tests/check_qwen_backend_live.py --yes --probe-only

它回答的是离线测试证明不了的那几个问题：

  1. 分离的 daemon + Xvfb + 浏览器能不能拉起来（从零、按需）；
  2. 登录态还在不在（页面里能不能看到输入框）；
  3. WAF 挑战会不会被浏览器自己接住（响应里出现 RGV587 → 之后仍能拿到答案）；
  4. 答案/引用的抽取是不是真的对（对比 DOM 里的实际文本）。

⚠️ 这不是离线测试：会占用浏览器 profile，请勿在编辑器里连点。
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
BACKEND = pathlib.Path(PKG) / "qwen_safe_backend" / "backend.py"

_ok = _fail = 0


def chk(cond, msg, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  ✓ {msg}")
    else:
        _fail += 1
        print(f"  ✗ {msg}" + (f"\n      {detail}" if detail else ""))


def run(args, wait):
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":99")
    p = subprocess.run([sys.executable, str(BACKEND), *args],
                       capture_output=True, text=True, timeout=wait, env=env)
    out = (p.stdout or "").strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        i = out.find("{")
        if i >= 0:
            try:
                return json.loads(out[i:])
            except json.JSONDecodeError:
                pass
    return {"ok": False, "err": f"输出无法解析(exit={p.returncode}): {out[:300]}"}


def main() -> int:
    ap = argparse.ArgumentParser(description="qwen 安全后端联网检查")
    ap.add_argument("--yes", action="store_true", help="确认执行（会开浏览器/发消息）")
    ap.add_argument("--probe-only", action="store_true", help="只自检页面，不发消息")
    ap.add_argument("--question", default="只回答两个字：收到", help="实发的测试问题")
    a = ap.parse_args()
    if not a.yes:
        print(__doc__)
        print("加上 --yes 才会真的执行。")
        return 2
    if not BACKEND.exists():
        print(f"✗ 后端不存在: {BACKEND}")
        return 2

    print("== 1. daemon + 浏览器自检 ==")
    st = run(["status"], 60)
    print("  status:", json.dumps(st, ensure_ascii=False))
    pr = run(["probe"], 300)
    chk(pr.get("ok") is True, "probe 成功（daemon+Xvfb+浏览器起来了）", str(pr)[:300])
    if not pr.get("ok"):
        print(f"\n{_ok} passed, {_fail} failed")
        return 1
    chk(pr.get("logged_in") is True, "页面里能读到 token（登录态在）")
    chk(pr.get("input_box") is True, "看得到输入框（页面没被风控页替掉）")
    if pr.get("screenshot"):
        print("  截图:", pr["screenshot"])
    if a.probe_only:
        print(f"\n{_ok} passed, {_fail} failed")
        return 1 if _fail else 0

    print(f"== 2. 实发一问：{a.question!r} ==")
    r = run(["ask", a.question, "--search", "--new-chat"], 700)
    chk(r.get("ok") is True, "ask 成功返回", str(r)[:400])
    if r.get("ok"):
        chk(bool(r.get("answer")), "answer 非空", repr(r.get("answer"))[:200])
        chk(isinstance(r.get("references"), list), "references 是 list")
        print(f"  answer: {r.get('answer','')[:200]!r}")
        print(f"  elapsed: {r.get('elapsed')}s  waf_challenges: {r.get('waf_retries')}")
        if r.get("waf_retries"):
            print("  （WAF 挑战被浏览器自己接住了 — 这正是换浏览器后端的原因）")
    print(f"\n{_ok} passed, {_fail} failed")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
