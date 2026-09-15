#!/usr/bin/env python3
"""webai 能力层离线测试 —— 不联网，不发任何请求。

跑法::

    cd <data>/public/webai && python3 tests/test_capability_layer.py

覆盖：
  * 向后兼容（老式 dict 仍能 coerce 成 Reply，ask() 签名未变）
  * 未知 phase / 未知 content type **不丢**（已被坑过三次的那条）
  * 产物归一（URL / 内联内容 / 多张去重 / 宽高乱序）
  * Job 生命周期（含状态名归一）
  * 错误六分类 + 各家方言
  * 能力别名归一与跨家路由
"""

import importlib.util
import json
import os
import sys
import tempfile

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


def main():
    webai = load()
    core = webai.core

    print("== 1. 向后兼容：老式 dict -> Reply ==")
    r = core.coerce_reply(
        {"text": "hi", "thinking": "th", "message_id": "m1",
         "references": [{"url": "u"}], "queries": ["q"]},
        provider="qwen",
    )
    chk(r.text == "hi" and r.thinking == "th" and r.message_id == "m1", "老 5 键原样保留")
    chk(r.references and r.queries == ["q"], "references / queries 保留")
    chk(r.status == "answered" and r.provider == "qwen", "status/provider 推断正确")
    chk(r.ok, "ok 属性")
    # 缺键不能让调用方崩
    chk(core.coerce_reply({}).status == "empty", "空 dict 不炸，status=empty")
    chk(core.coerce_reply(None).text == "", "None 不炸")

    print("== 2. 未知的东西不能丢（最高优先级）==")
    r = core.coerce_reply(
        {"text": "a", "extra": {"brand_new": {"x": 1}},
         "phases": {"answer": "a", "某个将来才有的phase": "z"}}
    )
    chk("某个将来才有的phase" in r.meta["phases"], "未知 phase 进 phases")
    chk(r.meta["extra"]["brand_new"]["x"] == 1, "未知 extra 原样保留")
    chk("某个将来才有的phase" in r.summary()["phases"], "summary 里也看得到")

    print("== 3. 产物归一 ==")
    # 3.1 URL 混在文本里
    a = core.coerce_assets([{"kind": "image", "url": "https://x/1.png"}])
    chk(a and a[0].url.endswith("1.png") and a[0].kind == "image", "URL 产物")
    # 3.2 内联内容（web_dev 无 URL）
    b = core.coerce_assets([{"kind": "code", "content": "<html>x</html>"}])
    chk(b and b[0].url is None and b[0].content.startswith("<html>"), "内联源码产物（无 URL）")
    # 3.3 多张 + 去重（GLM 一次 4 张且乱序）
    imgs = [{"image_url": f"https://x/{i}.png", "output_image_hw": [[1536, 2688]]}
            for i in (0, 3, 1, 2, 2)]
    c = core.coerce_assets(imgs)
    chk(len(c) == 4, f"4 张去重后仍是 4（实际 {len(c)}）")
    chk((c[0].meta.get("h"), c[0].meta.get("w")) == (1536, 2688),
        "output_image_hw 是 [高,宽]（名字骗人，[1536,2688] 实际是横图）")
    # 3.4 provider 给的 kind 不能被调用方的能力名覆盖
    d = core.coerce_assets([{"kind": "video", "content": "https://v/a.mp4"}], kind="t2v")
    chk(d[0].kind == "video", f"provider kind 优先于兜底 kind（实际 {d[0].kind}）")
    e = core.coerce_assets([{"markdown": "# hi"}], kind="report")
    chk(e[0].kind == "report", "无 provider kind 时用兜底")
    # 3.5 URL 不进 summary（含签名/user_id，别外泄）
    s = core.coerce_assets([{"kind": "image", "url": "https://cdn.x/p.png?key=SECRET"}])[0].summary()
    chk("SECRET" not in json.dumps(s), "summary 不含 URL 全文（防签名泄露）")
    chk(s.get("url_host") == "cdn.x", "summary 只留 host")
    # 3.6 内联内容落盘
    with tempfile.TemporaryDirectory() as td:
        aa = core.Asset(kind="code", content="<html>hi</html>")
        aa.save(td, filename="p.html")
        chk(aa.local and open(aa.path, encoding="utf-8").read().startswith("<html>"),
            "内联产物可落盘")

    print("== 4. Job 生命周期 ==")
    j = core.coerce_job({"task_id": "T", "task_status": "processing"}, provider="qwen", kind="t2v")
    chk(j.status == "running" and not j.done, "processing -> running")
    chk(core.coerce_job({"task_status": "success"}).status == "done", "success -> done")
    chk(core.coerce_job({"task_status": "error"}).status == "failed", "error -> failed")
    jj = core.coerce_job({"task_status": "success", "content": "https://v/x.mp4"}, kind="t2v")
    chk(jj.assets and jj.assets[0].url.endswith(".mp4"), "异步产物从 content 字段取")
    r = core.coerce_reply({"text": "", "task_id": "T2", "task_status": "processing"})
    chk(r.status == "job" and r.job and r.job.id == "T2", "无 stream 信号的隐式 Job 识别")
    chk(core.looks_like_job({"job": {}}), "looks_like_job 兼容 job 键")

    print("== 5. 交互式能力：needs_input ==")
    ri = core.coerce_reply({"text": "请确认", "needs_input": True})
    chk(ri.needs_input and ri.status == "needs_input", "追问信号 -> needs_input")
    rn = core.coerce_reply({"text": "普通回答"})
    chk(not rn.needs_input, "普通回答不误报")

    print("== 6. 错误六分类（通用词表）==")
    for txt, want in [("401 unauthorized", "auth"), ("403 Forbidden", "auth"),
                      ("rate limit exceeded", "quota"), ("请求过于频繁", "quota"),
                      ("该模型不可用", "unsupported"), ("connection reset by peer", "transient"),
                      ("请求超时", "transient"),
                      ("涉及敏感内容，换个话题聊聊", "content_policy"),
                      ("喵喵喵喵", "unknown")]:
        chk(core.classify_text(txt).value == want, f"classify_text({txt!r}) -> {want}")
    for st, want in [(401, "auth"), (429, "quota"), (503, "transient"), (404, "unsupported")]:
        chk(core.classify_text("", status=st).value == want, f"status={st} -> {want}")
    chk("不重试" in core.retry_hint("quota"), "quota 的建议是不重试")
    chk("重试" in core.retry_hint("transient"), "transient 的建议是重试")
    chk("降级" in core.retry_hint("unsupported"), "unsupported 的建议是降级到别家")
    try:
        raise core.WebAIError("auth", "token 过期", provider="qwen")
    except core.WebAIError as e:
        chk(e.kind is core.ErrorKind.AUTH and "[qwen]" in str(e), "WebAIError 带归类与 provider")

    print("== 7. 能力别名归一（各家方言 -> 通用名）==")
    chk(core.canonical("t2i") == core.canonical("image_gen") == "image_gen", "t2i == image_gen")
    chk(core.canonical("text2img") == "image_gen" and core.canonical("cogview") == "image_gen",
        "text2img / cogview 归到 image_gen")
    chk(core.canonical("ppt") == core.canonical("slides") == "slides", "ppt == slides")
    chk(core.canonical("我们还没见过的能力") == "我们还没见过的能力", "未知能力原样保留（开放字符串）")
    chk(core.same_capability("t2v", "video_gen"), "same_capability")

    print("== 8. 各家 provider 的声明与跨家路由 ==")
    for n in webai.names():
        m = webai.get(n)
        chk(isinstance(getattr(m, "CAPABILITY_MAP", None), dict), f"{n}.CAPABILITY_MAP 是 dict")
        chk(isinstance(m.capabilities, set) and "chat" in m.capabilities, f"{n} 声明了 chat")
        chk(callable(getattr(m, "models", None)), f"{n}.models() 可调用")
        chk(callable(getattr(m, "classify", None)), f"{n}.classify() 可调用")
        # 可选钩子缺失要能优雅降级，不能抛
        chk(isinstance(webai.models(n), list), f"{n}.models() 失败也返回 list")
        chk(isinstance(webai.probe(n, "chat"), dict), f"{n} probe 失败也返回 dict")

    chk("chat" in webai.capabilities("deepseek"), "capabilities 含静态声明")
    chk(webai.has("qwen", "t2i"), "qwen 声明 t2i")
    chk(webai.has("qwen", "image_gen"), "qwen 的 t2i 可被通用名命中")
    if "glm" in webai.names() and getattr(webai.get("glm"), "CAPABILITY_MAP", {}):
        chk(webai.has("glm", "t2i"), "glm 没有 t2i 键，但通过别名可被 t2i 命中")
        chk(webai.resolve_capability("glm", "t2i")[0] == "image_gen",
            "resolve_capability: glm 侧 t2i -> image_gen")
    chk(webai.resolve_capability("qwen", "image_gen")[0] == "t2i",
        "resolve_capability: qwen 侧 image_gen -> t2i")
    chk(webai.resolve_capability("deepseek", "image_gen") == ("", {}),
        "不支持的能力返回 ('',{}) 而不是抛异常")
    chk(not webai.has("deepseek", "image_gen"), "deepseek 不假装支持生图")

    print("== 9. 各家方言归类（走 webai.classify 的完整链路）==")
    for prov, txt, want in [
        ("deepseek", "rate_limit_reached", "quota"),
        ("glm", "40014", "auth"),
        ("glm", "权限不足", "unsupported"),
        ("stepfun", "换个话题聊聊", "content_policy"),
        ("qwen", "401 unauthorized", "auth"),
    ]:
        got = webai.classify(prov, text=txt).value
        chk(got == want, f"classify({prov}, {txt!r}) -> {got}（期望 {want}）")

    print("== 9b. search() 必须透传新键（不许手写 5 个键）==")
    # 曾经的 bug：search() 里手写 {"text","references","queries","session_id","message_id"}，
    # 于是 assets/phases/model/extra 被静默丢掉。这里用假 ask 逼出来。
    for n in webai.names():
        m = webai.get(n)
        if not callable(getattr(m, "search", None)):
            continue
        fake = {
            "text": "T", "references": [{"url": "u"}], "queries": ["q"],
            "message_id": "m", "session_id": "REAL-SID",
            "assets": [{"kind": "image", "url": "https://x/1.png"}],
            "phases": {"answer": "T", "brand_new": "z"},
            "model": "some-model", "extra": {"k": 1},
            "某个将来才有的键": 42,
        }
        old_ask, old_ns = m.ask, m.new_session
        m.ask = lambda *a, **k: dict(fake)
        m.new_session = lambda *a, **k: "fake-sid"
        try:
            out = m.search("x")
        finally:
            m.ask, m.new_session = old_ask, old_ns
        chk(out.get("text") == "T", f"{n}.search 保留 text")
        chk(out.get("assets"), f"{n}.search 透传 assets（曾被丢）")
        chk("brand_new" in (out.get("phases") or {}), f"{n}.search 透传 phases（曾被丢）")
        chk(out.get("model") == "some-model", f"{n}.search 透传 model（曾被丢）")
        chk("某个将来才有的键" in out, f"{n}.search 透传**未知**键（未来新增也不丢）")
        chk(out.get("session_id") == "REAL-SID", f"{n}.search 用 provider 回写的 session_id")

    print("== 10. provider 列表与可用性 ==")
    chk(len(webai.names()) >= 4, "至少 4 家")
    chk(set(webai.names()) >= {"qwen", "deepseek", "stepfun", "glm"}, "四家齐全")
    avail = webai.available()
    chk(isinstance(avail, dict) and all(isinstance(v, tuple) for v in avail.values()),
        "available() 结构正确（凭据失效也只是 False，不抛）")
    name, tried = webai.first_available("vision")
    chk(name or tried, "first_available 返回 (name, tried)")
    chk(webai.asset_dir("qwen").endswith("qwen") or os.path.isdir(webai.asset_dir("qwen")),
        "asset_dir 可用")

    print()
    if _failed:
        print(f"❌ {_fail} failed / {_ok + _fail} total")
        for m in _failed:
            print("   -", m)
        return 1
    print(f"✅ {_ok} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
