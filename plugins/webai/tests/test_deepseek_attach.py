"""deepseek 安全后端附件（v1.5.0）离线回归 —— 不联网、不启动浏览器。

钉住的行为（都是踩过坑的地方）：
  1. upload() 是**延迟引用**：真实上传发生在安全后端页面内（PoW 必须由前端
     计算，伪造请求曾导致封号）——本地只校验并返回 {"path":...}；
  2. ask() 把引用还原成路径，经 `--files` 透传给 backend CLI（曾忘进 rpc 载荷
     导致 files:0 静默丢件——CLI 解析与 rpc payload 两处都要有）；
  3. 文件与智能搜索互斥（前端 v9 同款 uploadBlockedBySearch 规则）→
     强制关搜索**且必须给 warnings**，不许静默；
  4. 能力声明：attach/vision 均有实测背书（.md 逐字读出 / 紫底绿三角描述正确）。
"""

import pytest

from plugins.webai.lib import deepseek as ds


def test_capabilities_declare_attach_and_vision():
    assert {"chat", "search", "attach", "vision"} <= ds.capabilities


def test_upload_returns_deferred_local_ref(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# x")
    ref = ds.upload(str(f))
    assert ref["kind"] == "local"
    assert ref["deferred"] is True
    assert ref["path"] == str(f.resolve())
    assert ref["name"] == "note.md"


def test_upload_missing_file_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="文件不存在"):
        ds.upload(str(tmp_path / "ghost.md"))


def test_ask_passes_files_and_forces_search_off(tmp_path, monkeypatch):
    f = tmp_path / "note.md"
    f.write_text("# x")
    ref = ds.upload(str(f))
    seen = {}

    def fake_run(args, wait):
        seen["args"] = list(args)
        return {"ok": True, "answer": "A", "references": []}

    monkeypatch.setattr(ds, "_run_backend", fake_run)
    sid = ds.new_session()
    out = ds.ask(sid, "读文件", search=True, files=[ref])
    assert "--files" in seen["args"], "文件必须透传到 backend CLI"
    assert str(f.resolve()) in seen["args"]
    assert "--search" not in seen["args"], "文件在场必须关搜索"
    assert out.get("warnings"), "互斥不许静默，必须给 warnings"
    assert out["text"] == "A"


def test_ask_without_files_keeps_old_contract(tmp_path, monkeypatch):
    seen = {}

    def fake_run(args, wait):
        seen["args"] = list(args)
        return {"ok": True, "answer": "A", "references": []}

    monkeypatch.setattr(ds, "_run_backend", fake_run)
    sid = ds.new_session()
    out = ds.ask(sid, "hi", search=True)
    assert "--files" not in seen["args"]
    assert "--search" in seen["args"]
    assert not out.get("warnings")
