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
  §5 能力声明诚实：没实测的不许声明（probe 里 supported=False + 理由）
  §6 浏览器 DOM 驱动面齐全（离线静态检查：多级模型菜单/模式芯片/子配置/风控自停）
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
    """2026-10-19 能力补全后的契约：方言、透传、诚实声明（离线，绝不拉浏览器）。"""
    print("== §5 能力声明诚实（能力补全版契约）==")
    q = webai.raw("qwen")
    chk({"chat", "search"} <= q.capabilities,
        f"capabilities 至少含 chat/search（实测 {sorted(q.capabilities)}）")
    pr = q.probe()
    declared = {k for k, v in pr.items() if v.get("supported")}
    chk(q.capabilities <= declared,
        f"capabilities 不得超出 probe 支持集（{sorted(q.capabilities)} ⊄ {sorted(declared)}）")
    # CAPABILITY_MAP：五项旧通道生成能力 + 三个页面新增模式 + search；
    # 值方言统一 {mode: 菜单条目 id}（backend PLUS_MODES 翻译）
    want = {"t2i", "t2v", "slides", "web_dev", "deep_research",
            "preview", "study", "travel", "search"}
    chk(set(q.CAPABILITY_MAP) == want,
        f"CAPABILITY_MAP 键 = {sorted(want)}（实测 {sorted(q.CAPABILITY_MAP)}）")
    chk(all(set(v) == {"mode"} for v in q.CAPABILITY_MAP.values()),
        "方言值全是 {mode: ...}（backend PLUS_MODES 翻译约定）")
    # models()：stub 掉后端，只验解析（离线测试绝不允许真拉浏览器）
    payload = {"ok": True, "current": "Qwen3.7-Plus", "models": [
        {"id": "Qwen3.7-Plus", "name": "Qwen3.7-Plus", "capabilities": [],
         "ctx": 0, "default": True},
        {"id": "Qwen3.8-Max", "name": "Qwen3.8-Max", "capabilities": [],
         "ctx": 0, "default": False}]}
    old, q._run_backend = q._run_backend, (lambda args, wait: payload)
    try:
        ms = q.models()
    finally:
        q._run_backend = old
    chk([m["name"] for m in ms] == ["Qwen3.7-Plus", "Qwen3.8-Max"],
        "models() 解析后端 models 载荷为 dict 清单")
    chk(isinstance(ms[0]["capabilities"], set) and ms[0]["default"] is True,
        "models() 条目规整 capabilities:set / default:bool")
    # new_session：已支持能力登记会话模式；未知能力点名抛（不静默降级）
    sid = q.new_session(capability="t2i", mode="t2i")
    chk(q._SESSION_MODE.get(sid) == "t2i", "t2i 会话登记 mode=t2i")
    try:
        q.new_session(capability="holo_deck")
        chk(False, "未知能力必须抛（不能静默降级）")
    except Exception as e:  # noqa: BLE001
        chk("holo_deck" in str(e), "不支持的能力报错里点名能力名")
    # ask() 把 model/think/mode 透传给后端 CLI
    fake = FakeBackend(OK_RESULT)
    old2, q._run_backend = q._run_backend, fake
    try:
        q.ask(q.new_session(), "你好", model="Qwen3.8-Max", think=True)
        q.ask(q.new_session(capability="t2i", mode="t2i"), "画只猫")
    finally:
        q._run_backend = old2
    call0, call1 = fake.calls[0], fake.calls[1]
    chk("--model" in call0 and "Qwen3.8-Max" in call0, "ask() 透传 --model")
    chk("--think" in call0, "ask() 透传 --think")
    chk("--mode" in call1 and "t2i" in call1, "会话能力 → ask() 透传 --mode")
    chk(str(q._MODE_TIMEOUT["t2i"]) in call1, "生成模式放宽 --timeout")
    # 后端源码：有头 + 新驱动入口都在
    chk(q.BACKEND.exists(), f"安全后端在: {q.BACKEND}")
    src = q.BACKEND.read_text(encoding="utf-8")
    compile(src, str(q.BACKEND), "exec")
    chk("headless=False" in src, "后端是有头启动（headless 过不了 WAF 挑战）")
    for marker in ("wms-trigger", "mode-select-open", "生成图像", "深入研究",
                   "自动", "思考", "快速", "PLUS_MODES", "SLOW_MODES"):
        chk(marker in src, f"后端含 {marker!r} 驱动")


def test_backend_dom_drivers(webai):
    """§6 浏览器后端的 DOM 驱动面必须齐全（离线静态检查，不开浏览器）。

    这一节守的是「页面逆向知识有没有被改丢」：多级模型菜单、模式芯片、
    子配置下拉、风控自停、产物三类（图/视频/附件卡）。
    """
    print("== §6 浏览器 DOM 驱动面（离线静态检查）==")
    q = webai.get("qwen")
    src = q.BACKEND.read_text(encoding="utf-8")
    compile(src, str(q.BACKEND), "exec")

    # 多级模型菜单：一级 + 「展开更多模型」hover 出的二级
    chk("wms-more-entry" in src, "模型菜单含「展开更多模型」二级入口")
    chk("wms-popup--secondary" in src, "模型菜单读二级面板 .wms-popup--secondary")
    chk("pin_model" in src, "模型「设为默认」驱动在（pin_model）")
    chk("_model_menu_open" in src, "模型菜单用 aria-expanded 判开合（不靠盲点）")

    # 模式：芯片是唯一状态源，退出靠芯片 X
    chk("mode-select-current-mode" in src, "模式芯片 .mode-select-current-mode")
    chk("mode-select-current-mode-close" in src, "芯片关闭钮（退出模式）")
    chk("def current_mode" in src and "def exit_mode" in src, "current_mode/exit_mode 都在")
    chk("mode-select-sub-popover-container" in src, "「更多」二级子菜单容器")
    chk('"preview": "预览模式"' in src and '"travel": "旅行规划师"' in src,
        "加号菜单二级三项（预览模式/学习/旅行规划师）已登记")

    # 子配置（比例/分辨率/研究深度）
    chk("def read_config" in src and "def set_config" in src, "子配置读写驱动在")
    chk("def mode_config_options" in src, "子配置选项枚举在")

    # 风控自保：DOM 级检测 + 轮询中也查
    chk("def risk_state" in src and "def guard" in src, "DOM 级人机验证检测（risk_state/guard）")
    chk('self.guard("ask:poll")' in src, "等待回答的轮询里也会查风控（命中即停手）")

    # 产物：图/视频/文件/附件卡都给
    for key in ('"images"', '"videos"', '"files_out"', '"attachments"'):
        chk(key in src, f"结果含 {key}")
    chk("attachment-card-container" in src, "附件卡（网页开发/幻灯片产物）有读取")
    chk("qwen-chat-thinking-and-sources-content" in src,
        "思考正文取「思考与搜索」面板（不是老 class）")
    chk("PASTE_THRESHOLD" in src, "长文本走粘贴式 fill（逐字敲会撞动作超时）")

    # 模式清单一致性：provider 的能力键必须在 backend PLUS_MODES 里可翻译
    for cap, dialect in q.CAPABILITY_MAP.items():
        mode = (dialect or {}).get("mode", "")
        if cap == "search":
            continue
        chk(f'"{mode}"' in src, f"能力 {cap} 的模式键 {mode!r} 在 backend 里有对应")


# ── runner ────────────────────────────────────────────────────


TESTS = [test_ask_contract, test_error_funnel, test_upload_deferred,
         test_capability_honesty, test_backend_dom_drivers]


def main():
    webai = load()
    for t in TESTS:
        t(webai)
    print(f"\n{_ok} passed, {_fail} failed" + (f" → {_failed}" if _failed else ""))
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
