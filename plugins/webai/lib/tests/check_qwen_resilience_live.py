#!/usr/bin/env python3
"""qwen 抗断联 **联网** 端到端检查 —— 会真的打 chat.qwen.ai（约 10 秒）。

跑法::

    cd <data>/public/plugins/webai/lib && python3 tests/check_qwen_resilience_live.py --yes

它回答的是"离线测试证明不了"的那几个问题：

  1. 正常一轮 / 连续两轮还通不通（改动没把主流程改坏）；
  2. `truncated` 会不会在正常流上**误报**（误报比不报更烦人）；
  3. 真·WAF 挑战下用户看到的是什么（修前是 JSONDecodeError，修后应是说人话的
     WebAIError，且文案不甩锅给 cookie）—— 用 UA 换成 python-requests 触发，
     这正是实测中唯一能稳定唤起 WAF 的开关；
  4. 真·连接抖动能不能被退避重试救回来（第一次 post 抛 ConnectionError）。
"""

import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

_ok = _fail = 0


def chk(cond, msg, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  ✅ {msg}")
    else:
        _fail += 1
        print(f"  ❌ {msg}" + (f"\n     {detail}" if detail else ""))


def load():
    for k in [k for k in list(sys.modules) if k == "webai" or k.startswith("webai.")]:
        sys.modules.pop(k, None)
    spec = importlib.util.spec_from_file_location(
        "webai", os.path.join(PKG, "__init__.py"), submodule_search_locations=[PKG]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webai"] = mod
    spec.loader.exec_module(mod)
    return mod


def main(yes: bool) -> int:
    if not yes:
        print("这会真的请求 chat.qwen.ai（约 10 秒、建几个空会话）。加 --yes 确认。")
        return 2
    webai = load()
    q = webai.raw("qwen")
    GOOD_UA = q.USER_AGENT

    ok, why = webai.get("qwen").available()
    if not ok:
        print(f"⏭  没有 qwen 凭据（{why}）—— 跳过联网检查")
        return 2

    print("== 1. 正常一轮 ==")
    try:
        out = webai.get("qwen").search("只回答两个字：收到")
        txt = (out.get("text") or "").strip()
        chk(bool(txt), f"拿到回答：{txt[:40]!r}")
        chk(out.get("truncated") is not True, "正常流**不**误报 truncated")
    except Exception as e:  # noqa: BLE001
        chk(False, "正常一轮就失败了", f"{type(e).__name__}: {e}")
        return 1

    print("== 2. 连续两轮（续接指针）==")
    try:
        p = webai.get("qwen")
        sid = p.new_session()
        r1 = p.ask(sid, "记住数字 4242，只回答 OK")
        r2 = p.ask(sid, "我刚让你记的数字是？只回答数字",
                   parent_message_id=r1.get("message_id"))
        chk(bool(r1.get("message_id")), "第一轮拿到 message_id（= 下一轮 parent）")
        chk("4242" in (r2.get("text") or ""), f"第二轮记得上下文：{r2.get('text')!r}")
    except Exception as e:  # noqa: BLE001
        chk(False, "连续两轮失败", f"{type(e).__name__}: {e}")

    print("== 3. 真·WAF 挑战下的报错长什么样 ==")
    q.USER_AGENT = "python-requests/2.31.0"          # 实测唯一稳定的触发开关
    try:
        webai.get("qwen").new_session()
        chk(False, "UA=python-requests 本该被 WAF 拦下（没拦？那这条断言要重写）")
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        chk(name != "JSONDecodeError" and not isinstance(e, ValueError),
            f"不再是 JSONDecodeError（实际 {name}）")
        chk(isinstance(e, webai.WebAIError), f"是带归类的 WebAIError（实际 {name}）")
        if isinstance(e, webai.WebAIError):
            chk(e.kind == webai.ErrorKind.TRANSIENT, f"归类 transient（实际 {e.kind}）")
        chk("与登录态无关" in str(e), "文案点明『与登录态无关』")
        chk("cookie 可能失效" not in str(e), "文案不再甩锅给 cookie")
        chk(getattr(e, "no_retry", False) is True, "标记 no_retry（不为配置问题反复喂风控）")
        print(f"     → 用户实际看到：{str(e)[:110]}…")
    finally:
        q.USER_AGENT = GOOD_UA

    print("== 4. 真·连接抖动 → 退避重试 ==")
    real_session = q._session
    state = {"n": 0}

    def flaky_session():
        state["n"] += 1
        if state["n"] == 1:
            raise __import__("requests").exceptions.ConnectionError("Connection reset by peer")
        return real_session()

    q._session = flaky_session
    try:
        out = webai.get("qwen").search("只回答两个字：收到")
        chk(bool((out.get("text") or "").strip()),
            f"抖一次后被重试救回：{(out.get('text') or '')[:30]!r}")
        chk(state["n"] >= 2, f"确实重发过（_session 被调 {state['n']} 次）")
    except Exception as e:  # noqa: BLE001
        chk(False, "抖动没被救回", f"{type(e).__name__}: {e}")
    finally:
        q._session = real_session

    print()
    print(f"{'✅' if not _fail else '❌'} {_ok} passed, {_fail} failed")
    return 1 if _fail else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="qwen 抗断联联网检查")
    ap.add_argument("--yes", action="store_true", help="确认要真的请求 chat.qwen.ai")
    a = ap.parse_args()
    sys.exit(main(a.yes))
