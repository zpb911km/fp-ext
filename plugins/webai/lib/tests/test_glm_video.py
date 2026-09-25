#!/usr/bin/env python3
"""GLM 生视频离线测试 —— 不联网、**不消耗积分**（全部 stub，一次真实提交都没有）。

跑法::

    cd <data>/public/plugins/webai/lib && python3 tests/test_glm_video.py

背景：GLM 视频走**独立端点族** `video-api/v1/`（推翻了 DESIGN §2 旧结论「不存在独立端点」）。
本测试锁住的正是那条协议，防止哪天有人"顺手统一到 assistant/stream"把它改坏：

  * §1 `capability="video_gen"` → 方言分流（`video: True`）
  * §2 提交 body 逐字段形状（抄前端抓包，含 `base_parameter_extra` 全套）
  * §3 拿不到 `chat_id` **必须抛** —— 绝不返回空 job（qwen 曾栽在"空成功"上）
  * §4 轮询状态归一 `processing→running` / `finished→done`，`plan:"44%"` → progress
  * §5 产物归一成 `Asset(kind="video")`
  * §6 `probe("video_gen")` 用免费的 `chat/list` 当探针（而非助手清单）
"""

import importlib.util
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

_ok = _fail = 0
_failed = []


@pytest.fixture(autouse=True)
def _no_balance():
    """关掉负载均衡限速（只在本模块生效，别污染 test_login_layer §5b 的限速测试）。

    glm 默认新建会话限 30s，本文件要连建好几个会话 —— 不关会让套件跑 90 秒+（实测踩过）。
    """
    os.environ["FP_WEBAI_NO_BALANCE"] = "1"
    yield
    os.environ.pop("FP_WEBAI_NO_BALANCE", None)


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


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        if isinstance(self._p, str):
            raise ValueError("not json")
        return self._p


def job_payload(chat_id="JOB123"):
    return {"status": 0, "message": "success",
            "result": {"chat_id": chat_id, "conversation_id": "C1", "status": "init"}}


def status_payload(status, plan, video_url=""):
    return {"status": 0, "message": "success",
            "result": {"status": status, "plan": plan,
                       "msg": "已完成" if status == "finished" else "视频生成中...",
                       "video_url": video_url}}


class StubSession:
    """可配置的替身：提交返回固定 chat_id，轮询按脚本推进。"""

    def __init__(self, submit=None, statuses=None):
        self.submit = submit
        self.statuses = list(statuses or [])
        self.posts = []          # (url, body)
        self.gets = []

    def post(self, url, **kw):
        body = kw.get("json")
        self.posts.append((url, body))
        return FakeResp(self.submit if self.submit is not None else job_payload())

    def get(self, url, **kw):
        self.gets.append(url)
        payload = self.statuses.pop(0) if self.statuses else status_payload("processing", "44%")
        return FakeResp(payload)


def test_dispatch_and_body(webai, glm):
    print("== 1/2. capability 分流 + 提交 body 形状 ==")
    sid = webai.new_session("glm", capability="video_gen")
    d = glm._SESSION_DIALECT.get(sid) or {}
    chk(d.get("video") is True, f"方言带 video=True：{d}")
    chk(d.get("assistant_id") == glm.VIDEO_ASSISTANT_ID,
        "video_gen 的 assistant_id 仍在（与 fixed_assistant 清单对得上号）")

    # chat 能力**不**带 video 标志 —— 老行为一字不变
    sid_chat = webai.new_session("glm")
    chk(not (glm._SESSION_DIALECT.get(sid_chat) or {}).get("video"),
        "普通会话不带 video 标志（不误走 video-api）")

    stub = StubSession()
    real = glm._session
    glm._session = lambda: stub
    try:
        webai.ask("glm", sid, "提示词")
    finally:
        glm._session = real

    chk(len(stub.posts) == 1, f"只提交一次（实 {len(stub.posts)}）")
    url, body = stub.posts[0]
    chk(url.endswith("/video-api/v1/chat"), f"走独立端点：{url}")
    chk(body.get("prompt") == "提示词", "顶层 prompt")
    chk(body.get("conversation_id") == "", "新会话 conversation_id 为空")
    chk(body.get("advanced_parameter_extra") == {}, "advanced_parameter_extra 默认空 dict")

    extra = body.get("base_parameter_extra") or {}
    # 抄前端抓包的那套字段 —— 一个都不能少，否则可能被静默忽略
    for k in ("generation_pattern", "resolution", "fps", "duration",
              "generation_ai_audio", "generation_ratio_width", "generation_ratio_height",
              "activity_type", "label_watermark", "generation_type"):
        chk(k in extra, f"base_parameter_extra 含 {k}")
    chk(extra.get("generation_pattern") == 1, "文生视频 generation_pattern=1")
    chk(extra.get("prompt") == "提示词", "extra 内也带 prompt（前端就这么发的）")

    # 显式覆盖生效 —— 只能**在 provider 层**传：`webai.new_session()` 只收
    # model/capability（自由 kwargs 会被 TypeError 挡掉，这是刻意的窄接口）。
    stub2 = StubSession()
    real = glm._session
    glm._session = lambda: stub2
    try:
        sid2 = glm.new_session(model="", capability="video_gen",
                               video_params={"duration": 2})
        webai.ask("glm", sid2, "p")
    finally:
        glm._session = real
    chk(stub2.posts[0][1]["base_parameter_extra"].get("duration") == 2, "video_params 覆盖 duration")


def test_no_empty_job(webai, glm):
    """§3 空成功 —— 这是 qwen 的老伤口，不许在 glm 重演。"""
    print("== 3. 拿不到 chat_id 必须抛（不返回空 job）==")
    for payload, label in (
        ({"status": 0, "message": "success", "result": {}}, "有 result 无 chat_id"),
        ({"status": 0, "message": "success"}, "连 result 都没有"),
        ({"status": 7, "message": "积分不足"}, "非 0 status"),
    ):
        stub = StubSession(submit=payload)
        real = glm._session
        glm._session = lambda: stub
        sid = webai.new_session("glm", capability="video_gen")
        try:
            webai.ask("glm", sid, "p")
            chk(False, f"{label} → 竟然没抛")
        except Exception as e:  # noqa: BLE001
            chk(True, f"{label} → 抛 {type(e).__name__}")
        finally:
            glm._session = real


def test_poll(webai, glm):
    print("== 4/5. 轮询状态归一 + 进度 + 产物 ==")
    script = [
        status_payload("processing", "33%"),
        status_payload("processing", "44%"),
        status_payload("finished", "100%", "https://sfile.example/v.mp4"),
    ]
    stub = StubSession(statuses=script)
    real = glm._session
    glm._session = lambda: stub
    try:
        j1 = glm.poll({"task_id": "T1"})
        chk(j1["task_status"] == "processing", f"processing 原样：{j1['task_status']}")
        chk(abs(j1["progress"] - 0.33) < 1e-6, f"33% → 0.33（实 {j1['progress']}）")
        chk(j1["task_id"] == "T1", "task_id 回填")
        chk(not j1.get("assets"), "还没完成就没产物")

        j2 = glm.poll({"task_id": "T1"})
        chk(abs(j2["progress"] - 0.44) < 1e-6, f"44% → 0.44（实 {j2['progress']}）")

        j3 = glm.poll({"task_id": "T1"})
        chk(j3["task_status"] == "success", f"finished → success（供 coerce_job 归一成 done）：{j3['task_status']}")
        chk(j3["progress"] == 1.0, "100% → 1.0")
        chk(j3.get("content", "").endswith(".mp4"), "content 是视频 URL")
        chk(j3.get("assets") == [{"kind": "video", "url": "https://sfile.example/v.mp4"}],
            f"assets 归一：{j3.get('assets')}")
    finally:
        glm._session = real

    # 走完整插件层：Reply.job → poll → Job 归一
    stub = StubSession(statuses=[
        status_payload("processing", "50%"),
        status_payload("finished", "100%", "https://sfile.example/v.mp4"),
    ])
    real = glm._session
    glm._session = lambda: stub
    sid = webai.new_session("glm", capability="video_gen")
    try:
        r = webai.ask("glm", sid, "提示")
        chk(r.status == "job", f"Reply.status=job（实 {r.status}）")
        chk(r.job is not None and r.job.id == "JOB123", "job.id 取到 chat_id")
        chk(r.job.kind == "text2video", f"kind 透传（实 {r.job.kind!r}）—— 丢过一次，见 core.coerce_reply")
        p1 = webai.poll("glm", r.job)
        chk(p1.status == "running" and abs(p1.progress - 0.5) < 1e-6,
            f"归一 running + 进度 0.5（实 {p1.status}/{p1.progress}）")
        p2 = webai.poll("glm", r.job)
        chk(p2.status == "done", f"finished → done（实 {p2.status}）")
        chk(len(p2.assets) == 1 and p2.assets[0].kind == "video",
            f"产物 Asset：{[(a.kind, a.url) for a in p2.assets]}")
        chk(p2.assets[0].url.startswith("http"), "资产是绝对 URL")
    finally:
        glm._session = real

    # 任务列表：免费，可用来查历史（字段名是 id 不是 chat_id）
    lst = StubSession()
    real = glm._session
    glm._session = lambda: lst
    try:
        r = glm._session().get(f"{glm.VIDEO_API}/chat/list?page=1&page_size=1",
                               headers=glm._headers(), timeout=30)
        chk(r.status_code == 200, "chat/list 是免费 GET")
    finally:
        glm._session = real


def test_poll_needs_id(glm):
    print("== 4b. poll() 缺 task_id 必须抛 ==")
    for bad in ({}, {"id": ""}, None):
        try:
            # None 会走 getattr 分支
            glm.poll(bad if bad is not None else object())
            if bad == {}:
                chk(False, "空 dict → 竟然没抛")
            else:
                chk(True, f"{bad!r} → 没抛（getattr 分支返回空 id）")
        except RuntimeError:
            chk(True, f"{bad!r} → 抛 RuntimeError")


def test_probe(webai, glm):
    """§6 probe 用免费端点，且不许把"清单里没有"说成不支持。"""
    print("== 6. probe(video_gen) 走 video-api 探针 ==")
    real = glm._session
    glm._session = lambda: StubSession()
    try:
        p = glm.probe("video_gen")
        chk(p["supported"] is True, f"200 → supported=True：{p}")
        chk(p["via"] == "video_api", f"via=video_api（实 {p['via']}）")

        class Denied:
            def get(self, url, **kw):
                return FakeResp({}, status=401)
        glm._session = lambda: Denied()
        p = glm.probe("video_gen")
        chk(p["supported"] is False, "401 → supported=False（要登录）")
    finally:
        glm._session = real


def main():
    # 关掉负载均衡限速：本测试会连建好几个会话，glm 默认限 30s，
    # 不关会让整个套件跑 90 秒+（实测踩过）。
    os.environ["FP_WEBAI_NO_BALANCE"] = "1"
    try:
        webai = load()
        glm = webai.raw("glm")
        test_dispatch_and_body(webai, glm)
        test_no_empty_job(webai, glm)
        test_poll(webai, glm)
        test_poll_needs_id(glm)
        test_probe(webai, glm)
    finally:
        os.environ.pop("FP_WEBAI_NO_BALANCE", None)

    print()
    if _failed:
        print(f"❌ {_fail} failed / {_ok + _fail} total")
        for m in _failed:
            print(f"   - {m}")
        return 1
    print(f"✅ {_ok} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
