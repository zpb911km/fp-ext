"""附件能力（files）并入 ask_llm / copilot 的离线回归测试（v1.3.0 起 image_paths → files）。

**不联网、不上传真文件、不花任何额度** —— provider 层一律用假对象顶替，
只验证"参数有没有一路传到位 + 能力筛选对不对 + 失败时会不会诚实报错"。

被这组测试钉住的行为（都是踩过坑的地方）：
  1. 带附件时自动选后端按**类型分诊的能力**挑（图片→vision，其它→attach，
     混传→取覆盖更窄的能力当代表）；
  2. 能力不足必须**明确报错并指路** —— 静默丢文件最难排查；
  3. 上传失败要**中止**，不能"没文件也照发"；
  4. 旧参数名 `image_paths` 仍兼容（主名是 `files`，schema 只暴露 `files`）。
"""

import asyncio
import importlib.util
import json
import pathlib

import pytest

from plugins.webai.tools import _files, ask_llm, copilot


# ── 假 provider ────────────────────────────────────────────────

class FakeProvider:
    """最小可用的 provider 假件：记录每次调用的 kwargs。"""

    default_model = "fake-1"

    def __init__(self, caps=("chat", "search", "vision", "attach"), upload_raises=None):
        self.capabilities = set(caps)
        self.upload_raises = upload_raises
        self.calls = {"upload": [], "search": [], "ask": [], "new_session": []}

    def available(self):
        return True, ""

    def upload(self, path):
        if self.upload_raises:
            raise self.upload_raises
        self.calls["upload"].append(path)
        return {"ref": "ref-" + pathlib.Path(path).name}

    def search(self, query, **kw):
        self.calls["search"].append((query, kw))
        return {"text": "答案", "references": [{"url": "u", "title": "t", "snippet": "s"}],
                "queries": [query]}

    def new_session(self, model=""):
        self.calls["new_session"].append(model)
        return "sid-1"

    def ask(self, session_id, text, **kw):
        self.calls["ask"].append((session_id, text, kw))
        return {"text": "回答", "message_id": "mid-1", "session_id": session_id,
                "references": [], "queries": []}


@pytest.fixture
def install(monkeypatch):
    """把假 provider 装进两个工具共用的 lib 包；顺带记录 next_available 的能力参数。"""
    seen: dict = {"capability": []}
    state: dict = {"provider": FakeProvider()}

    lib = ask_llm._webai                      # 与 _files / copilot 是同一个包对象
    assert copilot._webai is lib and _files._webai is lib

    def _get(name):
        # "fake" = 被测对象（能力由用例决定）；其余名字返回**与现实同形**的假件。
        # 2026-09-25 起 stepfun 也收文件：现实中 vision 与 attach 都是
        # qwen/glm/stepfun 三家、覆盖数打平 —— 混传的代表能力靠 ask_llm 里的
        # 字母序 tiebreak（attach < vision）确定为 attach，断言因此是确定的。
        if name == "fake":
            return state["provider"]
        return FakeProvider(caps={"chat", "search", "vision", "attach"})

    def _next_available(capability="", order=None):
        seen["capability"].append(capability)
        return ("fake", [])

    monkeypatch.setattr(lib, "get", _get)
    monkeypatch.setattr(lib, "names", lambda: ["fake", "qwen"])
    monkeypatch.setattr(lib, "next_available", _next_available)

    def _setup(provider):
        state["provider"] = provider
        return seen

    yield _setup


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return str(p)


@pytest.fixture
def txt(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello attachment", encoding="utf-8")
    return str(p)


# ── 1. 工具面：vision 已取消，两个工具都带 files ───────────────

def test_vision_removed_and_two_tools_remain():
    from plugins.webai.plugin import _TOOL_MODULES
    assert [m.TOOL_NAME for m in _TOOL_MODULES] == ["ask_llm", "copilot"]
    assert importlib.util.find_spec("plugins.webai.tools.vision") is None


def test_both_definitions_expose_files():
    for mod in (ask_llm, copilot):
        props = mod.DEFINITION["function"]["parameters"]["properties"]
        assert "files" in props, mod.TOOL_NAME
        assert "image_paths" not in props, "旧名只在代码层兼容，schema 不暴露"
        spec = props["files"]
        # 用标准 array（不用 ["array","string"] 联合类型 —— 无先例，怕个别后端拒收）
        assert spec["type"] == "array" and spec["items"]["type"] == "string"
        # 描述里不能再指向已删除的 vision
        assert "vision" not in mod.DEFINITION["function"]["description"]


# ── 2. 参数校验与类型分诊 ──────────────────────────────────────

def test_check_paths_accepts_str_and_list(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    paths, err = _files.check_paths(str(f))
    assert err == "" and paths == [str(f)]
    paths, err = _files.check_paths([str(f), "  "])
    assert err == "" and paths == [str(f)]          # 空白项被丢掉


def test_check_paths_accepts_any_file_and_required_caps(png, txt):
    """v1.3.0：非图片文件是**合法意图**，不再在路径层被拒（能不能收是后端的事）。"""
    paths, err = _files.check_paths(txt)
    assert err == "" and paths == [txt]
    # 分诊：图片→vision，其它→attach，混合→两者
    assert _files.required_caps([png]) == {"vision"}
    assert _files.required_caps([txt]) == {"attach"}
    assert _files.required_caps([png, txt]) == {"vision", "attach"}
    assert _files.is_image(png) and not _files.is_image(txt)


def test_check_paths_rejects_missing_empty_type(tmp_path):
    assert "不存在" in _files.check_paths(str(tmp_path / "nope.png"))[1]
    assert _files.check_paths([])[1]
    assert _files.check_paths(123)[1]               # 类型不对不猜意图


def test_check_paths_caps_batch_at_six(tmp_path):
    names = [str(tmp_path / f"{i}.png") for i in range(7)]
    assert "最多传 6 个文件" in _files.check_paths(names)[1]


# ── 3. ask_llm：按类型分诊能力、files 一路传到底 ───────────────

def test_ask_llm_forwards_files_to_search(install, png):
    fake = FakeProvider()
    seen = install(fake)
    out = ask_llm.ask_llm("图里是什么", files=[png])
    assert out["success"], out
    assert seen["capability"] == ["vision"]          # ① 图片按 vision 挑后端
    q, kw = fake.calls["search"][0]
    assert q == "图里是什么"
    assert kw["files"] == [{"ref": "ref-shot.png"}]  # ② files 真的传到位了


def test_ask_llm_txt_routes_to_attach_capability(install, txt):
    """非图片文件按 attach 挑后端（attach 集合 ⊂ vision 集合，不会挑到只满足一半的）。"""
    fake = FakeProvider()
    seen = install(fake)
    out = ask_llm.ask_llm("文件里写了什么", files=[txt])
    assert out["success"], out
    assert seen["capability"] == ["attach"]
    assert fake.calls["search"][0][1]["files"] == [{"ref": "ref-note.txt"}]


def test_ask_llm_mixed_files_route_to_narrowest_capability(install, png, txt):
    """混传 → 取覆盖更窄的能力当代表（attach < vision），避免白跑一趟。"""
    fake = FakeProvider()
    seen = install(fake)
    out = ask_llm.ask_llm("图文都看看", files=[png, txt])
    assert out["success"], out
    assert seen["capability"] == ["attach"]
    refs = fake.calls["search"][0][1]["files"]
    assert [r["ref"] for r in refs] == ["ref-shot.png", "ref-note.txt"]


def test_ask_llm_without_attachment_stays_on_search_capability(install):
    fake = FakeProvider()
    seen = install(fake)
    out = ask_llm.ask_llm("随便问")
    assert out["success"], out
    assert seen["capability"] == ["search"]
    assert fake.calls["search"][0][1]["files"] is None   # 老行为一字不变


def test_ask_llm_accepts_bare_string_path(install, png):
    """schema 声明 array，但模型常传裸字符串 —— 代码必须宽容。"""
    fake = FakeProvider()
    install(fake)
    out = ask_llm.ask_llm("图里是什么", files=png)
    assert out["success"], out
    assert fake.calls["search"][0][1]["files"] == [{"ref": "ref-shot.png"}]


def test_ask_llm_legacy_image_paths_alias(install, png):
    """旧名 image_paths 仍要在函数层兼容（v1.2.0 的调用方不改也能跑）。"""
    fake = FakeProvider()
    seen = install(fake)
    out = ask_llm.ask_llm("图里是什么", image_paths=[png])
    assert out["success"], out
    assert seen["capability"] == ["vision"]
    assert fake.calls["search"][0][1]["files"] == [{"ref": "ref-shot.png"}]


def test_ask_llm_rejects_nonvision_backend(install, png):
    install(FakeProvider(caps={"chat", "search"}))     # deepseek/mimo 形态
    out = ask_llm.ask_llm("图里是什么", provider="fake", files=[png])
    assert not out["success"]
    assert "不支持看图" in out["error"] and "带图可用" in out["error"]


def test_ask_llm_rejects_nonattach_backend_for_txt(install, txt):
    """收不了附件的后端接 txt → 指路（措辞统一在 _files.cap_error）。"""
    install(FakeProvider(caps={"chat", "search", "vision"}))
    out = ask_llm.ask_llm("文件里写了什么", provider="fake", files=[txt])
    assert not out["success"]
    assert "不支持传文件" in out["error"] and "带文件可用" in out["error"]


def test_ask_llm_missing_file_fails_before_any_call(install):
    fake = FakeProvider()
    install(fake)
    out = ask_llm.ask_llm("x", files=["/no/such/file.png"])
    assert not out["success"] and "不存在" in out["error"]
    assert fake.calls["search"] == []                 # 不能"没文件也照发"


def test_ask_llm_execute_passes_files(install, png, tmp_path):
    """执行器接线（files 主名）。"""
    fake = FakeProvider()
    install(fake)
    out = asyncio.run(
        ask_llm.execute({"keywords": "图里是什么", "files": [png]}))
    assert "搜索结果" in out
    assert fake.calls["search"][0][1]["files"] == [{"ref": "ref-shot.png"}]


def test_ask_llm_execute_accepts_legacy_image_paths(install, png):
    """执行器仍收旧名 image_paths（params 层兼容）。"""
    fake = FakeProvider()
    install(fake)
    out = asyncio.run(
        ask_llm.execute({"keywords": "图里是什么", "image_paths": [png]}))
    assert "搜索结果" in out
    assert fake.calls["search"][0][1]["files"] == [{"ref": "ref-shot.png"}]


# ── 4. copilot：files 传到位、上传失败必须中止 ─────────────────

@pytest.fixture
def store(monkeypatch, tmp_path):
    """把会话仓库指到临时文件，别污染真实 sessions.json。"""
    f = tmp_path / "sessions.json"
    monkeypatch.setattr(copilot, "_STORE_FILE", str(f))
    return f


def test_copilot_forwards_files_to_ask(install, store, png):
    fake = FakeProvider()
    install(fake)
    out = copilot.copilot_say("这张图是什么", session="t1", provider="fake",
                              files=[png])
    assert out["success"], out
    kw = fake.calls["ask"][0][2]
    assert kw["files"] == [{"ref": "ref-shot.png"}]


def test_copilot_without_attachment_keeps_files_none(install, store):
    fake = FakeProvider()
    install(fake)
    out = copilot.copilot_say("你好", session="t2", provider="fake")
    assert out["success"], out
    assert fake.calls["ask"][0][2]["files"] is None


def test_copilot_upload_failure_aborts(install, store, png):
    """上传失败要中止 —— 否则用户以为文件已提交，模型却看不到。"""
    fake = FakeProvider(upload_raises=RuntimeError("OSS 500"))
    install(fake)
    out = copilot.copilot_say("这张图是什么", session="t3", provider="fake",
                              files=[png])
    assert not out["success"]
    assert fake.calls["ask"] == []                     # 一次都没发出去
    assert "OSS 500" in out["error"] or "上传" in out["error"]


def test_copilot_history_records_files(install, store, png):
    fake = FakeProvider()
    install(fake)
    copilot.copilot_say("看图", session="t4", provider="fake", files=[png])
    hist = copilot.copilot_history("t4")
    assert hist and hist[0].get("files") == [png]      # history 要能看出当时带了附件


def test_copilot_legacy_image_paths_alias(install, store, png):
    """旧名 image_paths 在 copilot_say 层仍兼容。"""
    fake = FakeProvider()
    install(fake)
    out = copilot.copilot_say("看图", session="t5", provider="fake",
                              image_paths=[png])
    assert out["success"], out
    assert fake.calls["ask"][0][2]["files"] == [{"ref": "ref-shot.png"}]


def test_copilot_history_shows_legacy_images_key(store):
    """v1.2.0 旧记录用 images 键 —— 展示层必须还认（files or images）。"""
    store.write_text(json.dumps({"sessions": {"legacy": {
        "provider": "fake",
        "history": [{"role": "user", "content": "旧消息", "images": ["/x.png"]},
                    {"role": "assistant", "content": "旧回答"}],
    }}}), encoding="utf-8")
    out = asyncio.run(copilot.execute({"action": "history", "session": "legacy"}))
    assert "📎×1" in out and "旧消息" in out


# ── 5. 能力门的报错要"指路" ────────────────────────────────────

def test_upload_files_names_capable_backends(install, tmp_path, txt):
    """顺序：路径 → 能力 → 上传；能力不足先于任何网络动作。"""
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")

    install(FakeProvider(caps={"chat", "search"}))
    files, err = _files.upload_files("fake", [str(p)])
    assert files == []
    assert "不支持看图" in err and "带图可用" in err

    install(FakeProvider(caps={"chat", "search", "vision"}))
    files, err = _files.upload_files("fake", [txt])
    assert files == []
    assert "不支持传文件" in err and "带文件可用" in err


def test_upload_images_is_alias_of_upload_files():
    assert _files.upload_images is _files.upload_files
