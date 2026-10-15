#!/usr/bin/env python3
"""qwen 安全后端 provider 契约测试 —— 不联网、不开浏览器、不发真实请求。

跑法::

    cd <data>/public/plugins/webai/lib && python3 tests/test_qwen_backend.py

背景：2026-10 chat.qwen.ai 的 /api/v2/chat/completions 上了阿里 WAF（RGV587），
纯 HTTP 通道整体废弃，qwen 改走 ``qwen_safe_backend``（真实浏览器）。
本文件取代旧 ``test_qwen_resilience.py``（它测的 _session/_collect_stream/_parse_sse
已不存在）：那些「WAF 页识别 / 流截断 / 空流重发」的失效模式在浏览器通道上
**不会发生**（浏览器自己会跑完 bx 挑战），所以测点换成了新通道真正的风险：

  §1 老契约形状不变（text/thinking/message_id/references/queries 一个不少）
  §2 会话新鲜度：new_session() 后的首次 ask 必须开新对话
  §3 错误收口：后端的 err 必须变成异常，且 classify() 能归类（风控→auth）
  §4 附件是延迟引用（本地路径透传，不在 provider 层伪造上传）
  §5 能力声明诚实：没实测的不许声明（CAPABILITY_MAP 空 / probe 里 supported=False）
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

_ok = _fail = 0
_failed = []


def chk(cond, msg):
    global _ok, _fail
    if cond:
        _ok += 1
    else:
        _fail += 1
        _failed.append(msg)
        print(f"  ✗ {msg}")


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


# ── 假后端 ─────────────────────────────────────────────────────


class FakeBackend:
    """替掉 qwen._run_backend：只记参数，不碰网络也不开浏览器。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, args, wait):
        self.calls.append(list(args))
        return self.result


OK_RESULT = {
    "ok": True,
    "answer": "杭州市西湖区",
    "thinking": "",
    "references": [{"title": "baike.baidu.com", "url": "https://baike.baidu.com"}],
    "elapsed": 12.3,
    "search": True,
    "waf_retries": 1,
    "files": 0,
    "url": "https://chat.qwen.ai/c/x",
}


# ── §1/§2 契约形状 + 会话新鲜度 ───────────────────────────────


def test_ask_contract(webai):
    print("== §1 ask() 老契约形状 ==")
    q = webai.raw("qwen")
    fake = FakeBackend(OK_RESULT)
    old, q._run_backend = q._run_backend, fake
    try:
        sid = q.new_session()
        r = q.ask(sid, "西湖在哪", search=True)
    finally:
        q._run_backend = old
    for k in ("text", "thinking", "message_id", "references", "queries"):
        chk(k in r, f"ask() 返回带 {k}")
    chk(r["text"] == "杭州市西湖区", "text 取 answer")
    chk(r["references"] and r["references"][0]["url"].startswith("https://"), "references 透传")
    chk(r["phases"].get("answer") == r["text"], "phases.answer 与 text 一致")
    chk(isinstance(r["message_id"], str) and r["message_id"], "message_id 非空字符串")

    print("== §2 会话新鲜度 ==")
    chk("--new-chat" in fake.calls[0], "new_session() 后首次 ask 带 --new-chat")
    fake2 = FakeBackend(OK_RESULT)
    old, q._run_backend = q._run_backend, fake2
    try:
        q.ask(sid, "再问一次")
    finally:
        q._run_backend = old
    chk("--new-chat" not in fake2.calls[0], "同一会话第二轮不再开新对话")
    chk("--search" in fake.calls[0], "search=True 透传 --search")


# ── §3 错误收口 ────────────────────────────────────────────────


def test_error_funnel(webai):
    print("== §3 错误收口 / 归类 ==")
    q = webai.raw("qwen")
    fake = FakeBackend({"ok": False,
                        "err": "qwen 触发风控人机验证（RGV587），自动接住失败"})
    old, q._run_backend = q._run_backend, fake
    try:
        try:
            q.search("随便问问")
            chk(False, "后端报错必须抛异常")
        except Exception as e:  # noqa: BLE001
            chk("RGV587" in str(e), "异常里保留后端原文（不丢东西）")
            chk("风控" in str(e), "文案说人话（提到风控）")
            kind = webai.classify("qwen", e)
            chk(str(kind) in ("auth", "ErrorKind.AUTH"), f"风控归成 auth（实测 {kind}）")
    finally:
        q._run_backend = old

    chk(q.classify(text="等待回答超时") == "transient", "超时→transient")
    chk(q.classify(text="普通错误") == "", "认不出→交回通用词表（不猜）")


# ── §4 附件延迟引用 ───────────────────────────────────────────


def test_upload_deferred(webai):
    print("== §4 附件延迟引用 ==")
    q = webai.raw("qwen")
    ref = q.upload(__file__)
    chk(isinstance(ref, dict) and ref.get("path") and ref.get("deferred") is True,
        "upload() 回延迟引用（含本地 path）")
    try:
        q.upload("/no/such/file-xyz")
        chk(False, "不存在的文件必须拒绝")
    except Exception:  # noqa: BLE001
        pass
    fake = FakeBackend(OK_RESULT)
    old, q._run_backend = q._run_backend, fake
    try:
        q.search("带附件问题", files=[ref])
    finally:
        q._run_backend = old
    chk("--files" in fake.calls[0] and any("qwen_backend" in a for a in fake.calls[0]),
        "ask() 把本地路径透传给后端（provider 层不做上传）")


# ── §5 能力声明诚实 ───────────────────────────────────────────


def test_capability_honesty(webai):
    print("== §5 能力声明诚实 ==")
    q = webai.raw("qwen")
    chk(q.capabilities == {"chat", "search"},
        f"capabilities 只声明实测过的 chat/search（实测 {sorted(q.capabilities)}）")
    chk(q.CAPABILITY_MAP == {}, "CAPABILITY_MAP 为空（生成类能力未实现，不许吹）")
    chk(q.models() == [], "models() 返回 []（浏览器后端不切页面模型）")
    pr = q.probe()
    chk(pr["chat"]["supported"] is True and pr["search"]["supported"] is True,
        "probe(): chat/search 标 True")
    chk(pr["vision"]["supported"] is False, "probe(): 未实测的 vision 标 False")
    try:
        q.new_session(capability="t2i")
        chk(False, "不支持的能力必须抛（不能静默降级）")
    except Exception as e:  # noqa: BLE001
        chk("t2i" in str(e), "不支持的能力报错里点名能力名")
    # 后端入口存在且可编译
    chk(q.BACKEND.exists(), f"安全后端在: {q.BACKEND}")
    src = q.BACKEND.read_text(encoding="utf-8")
    compile(src, str(q.BACKEND), "exec")
    chk("headless=False" in src, "后端是有头启动（headless 过不了 WAF 挑战）")


# ── runner ────────────────────────────────────────────────────


TESTS = [test_ask_contract, test_error_funnel, test_upload_deferred,
         test_capability_honesty]


def main():
    webai = load()
    for t in TESTS:
        t(webai)
    print(f"\n{_ok} passed, {_fail} failed" + (f" → {_failed}" if _failed else ""))
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
