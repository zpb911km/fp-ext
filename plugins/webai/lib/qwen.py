"""Qwen provider —— 安全后端版（真实浏览器通道）。

历史：旧实现直接伪造 chat.qwen.ai 的 ``/api/v2/chat/completions`` 请求，2026-10
该端点上了阿里 WAF 风控（``RGV587_ERROR`` → ``punish?x5secdata=...``），补 cookie、
换 curl_cffi、拷 bx-ua 全部无效——纯 HTTP 客户端必被拦，且**只有有头浏览器**能跑完
bx 挑战。本文件是替换后端：问答一律经由 ``qwen_safe_backend``（真实 chromium +
Xvfb 懒启动 daemon，见同目录 backend.py），不再发出任何伪造请求，也不再依赖
cookie / token / requests。

契约（``PROVIDER_SPEC.md`` 铁律）保持不变：
  ask() -> dict{text, thinking, message_id, references, queries, ...}
  search() / upload() / available() / verify() / models() / probe() / classify()

⚠️ 能力范围（诚实声明）：浏览器后端目前只驱动「普通对话 + 联网搜索」。
   生成类能力（t2i / t2v / slides / deep_research）**未实现**，故 CAPABILITY_MAP
   为空、不对外声明；要加需要在 backend 里驱动「选择模式」下拉（待做）。
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

name = "qwen"
# chat/search：已实测。attach/vision：见 _upload_files（页面内真实上传）。
capabilities = {"chat", "search"}
default_model = "default"
CAPABILITY_MAP: dict = {}

HERE = pathlib.Path(__file__).resolve().parent
BACKEND = pathlib.Path(os.environ.get(
    "QWEN_BACKEND", str(HERE / "qwen_safe_backend" / "backend.py"),
))

try:
    from fp_core.platform_utils import get_data_dir  # type: ignore

    DATA = pathlib.Path(get_data_dir())
except Exception:  # noqa: BLE001
    DATA = pathlib.Path.home() / ".local" / "share" / "fp"

QWEN_DIR = DATA / "webai" / "qwen"
_PROFILE = QWEN_DIR / "profile"

# 会话新鲜度：new_session() 登记的 sid，首次 ask 时让后端开新对话
_FRESH: set[str] = set()


# ── 凭据/状态 ────────────────────────────────────────────────────


def load_token() -> str:
    """新通道不读 token（登录态在浏览器 profile 里）。保留签名兼容旧调用方。"""
    try:
        return (QWEN_DIR / "token").read_text().strip()
    except Exception:  # noqa: BLE001
        return ""


def load_cookie() -> str:
    """同上：仅作兼容/审计读取，实际请求不再使用。"""
    try:
        return (QWEN_DIR / "cookie").read_text().strip()
    except Exception:  # noqa: BLE001
        return ""


def available():
    if not BACKEND.exists():
        return False, f"安全后端不存在: {BACKEND}"
    if not _PROFILE.exists():
        return False, (
            "Qwen 浏览器登录态缺失。请运行登录脚本重新登录：\n"
            "    python3 <fp数据目录>/public/plugins/webai/lib/qwen_login.py"
        )
    return True, ""


# ── 会话 ─────────────────────────────────────────────────────────


def new_session(model: str = "", capability: str = "", **dialect) -> str:
    """会话 = 浏览器里的一个对话。真实新建发生在本会话的第一次 ask（new_chat）。"""
    if capability:
        raise RuntimeError(
            f"qwen 浏览器后端暂不支持能力 {capability!r}（仅 chat/search）；"
            "CAPABILITY_MAP 为空，正常路径不会走到这里"
        )
    sid = f"qw-{uuid.uuid4().hex[:16]}"
    _FRESH.add(sid)
    return sid


# ── 后端调用 ─────────────────────────────────────────────────────


def _run_backend(args: list[str], wait: int) -> dict:
    """调用安全后端 CLI，返回其 JSON。QWEN_QUIET=1 保证 stdout 纯净。"""
    env = dict(os.environ)
    env["QWEN_QUIET"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, str(BACKEND), *args],
            capture_output=True, text=True, timeout=wait, env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("qwen 安全后端调用超时（浏览器侧卡住）") from None
    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()[-400:]
        raise RuntimeError(f"qwen 安全后端无输出(exit={proc.returncode}): {err}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        i = out.find("{")
        if i >= 0:
            try:
                return json.loads(out[i:])
            except json.JSONDecodeError:
                pass
        raise RuntimeError(f"qwen 安全后端输出无法解析: {out[:200]}")


# ── 发消息 ───────────────────────────────────────────────────────


def ask(
    session_id: str,
    text: str,
    *,
    parent_message_id=None,
    model: str = "",
    think: bool = False,
    search: bool = False,
    files: list | None = None,
    retries: int = 1,
    retry_delay: int = 15,
    **kwargs,
) -> dict:
    """一轮问答。浏览器后端与 API 版的区别：无 phase 流、无 message_id、
    无 queries（页面不暴露）—— 拿不到就留空，不猜。"""
    if model and model not in (default_model, "qwen3.7-plus", ""):
        raise RuntimeError(
            f"qwen 不支持 model={model!r}：浏览器后端不切页面模型（用页面当前选中的"
            "模型），可用清单见 webai.models('qwen')"
        )

    # 附件：upload() 返回延迟引用（文件在页面内真实上传），这里抽出本地路径透传。
    paths: list = []
    if files:
        for f in files:
            p = f if isinstance(f, str) else (
                (f or {}).get("path") or (f or {}).get("local") or "")
            if p:
                paths.append(p)

    timeout = 300
    args = ["ask", text, "--timeout", str(timeout)]
    if search:
        args.append("--search")
    if paths:
        args += ["--files", *paths]
    if session_id in _FRESH:
        _FRESH.discard(session_id)
        args.append("--new-chat")

    last_err = None
    for attempt in range(max(1, retries)):
        res = _run_backend(args, wait=timeout + 300)
        if res.get("ok"):
            answer = res.get("answer") or ""
            out = {
                "text": answer,
                "thinking": res.get("thinking") or "",
                "message_id": f"qw-{int(time.time() * 1000)}",
                "references": res.get("references") or [],
                "queries": [],
                "model": default_model,
                "phases": {"answer": answer},
            }
            if res.get("waf_retries"):
                out["extra"] = {"waf_challenges": res["waf_retries"]}
            return out
        err = str(res.get("err") or "未知错误")
        last_err = RuntimeError(err)
        low = err.lower()
        transient = ("超时" in err) or ("timeout" in low) or ("daemon" in low)
        if not transient or attempt >= retries - 1:
            break
        time.sleep(retry_delay)
    raise last_err if last_err else RuntimeError("qwen 安全后端未知错误")


def search(query: str, *, think: bool = False, model: str = "",
           files: list | None = None, **kwargs) -> dict:
    sid = new_session(model)
    r = ask(sid, query, model=model, think=think, search=True, files=files)
    # 与旧契约一致：原样带上 ask() 的所有键，只补 session_id。
    out = dict(r)
    out.setdefault("session_id", sid)
    return out


def upload(path: str) -> dict:
    """延迟引用：真实上传发生在页面内（input[type=file] → 前端自己上传），
    这里只做校验并返回本地引用，ask() 收到后经 set_input_files 真正上传。"""
    p = pathlib.Path(path).expanduser().resolve()
    if not p.is_file():
        raise RuntimeError(f"文件不存在: {p}")
    if p.stat().st_size > 100 * 1024 * 1024:
        raise RuntimeError(f"超过 100MB 上限: {p.name}")
    return {"kind": "local", "path": str(p), "name": p.name, "deferred": True}


# ── 可选钩子（PROVIDER_SPEC.md §3/§4/§6）────────────────────────


def models() -> list:
    """可选模型：浏览器后端不切页面模型，也就没有「可用模型清单」可言。

    返回 []（诚实：拿不到就说没有），而不是把网页版看到的名字硬编进去——
    硬编会随页面改版变成谎话。要切模型请在页面自己的下拉里选，后端沿用该选择。
    """
    return []


def probe(capability: str = "") -> dict:
    """§4：报告本家能力。已实测的写 supported=True，未实现的写 False + 理由。"""
    return {
        "chat": {"supported": True, "via": "browser"},
        "search": {"supported": True, "via": "browser"},
        "attach": {"supported": False,
                   "note": "页面内上传已接线但未实测验收，暂不对外声明"},
        "vision": {"supported": False,
                   "note": "同上：需先实测页面内图片上传链路"},
    }


def classify(text: str = "", status: int = 0, raw=None, exc=None, **_):
    """错误归类。本家方言：风控人机验证 → auth（不重试，提示过验证）；
    浏览器/daemon 卡住 → transient。其余返回空串，由 __init__ 落到通用词表。"""
    blob = (text or "") + " " + (str(exc) if exc is not None else "")
    low = blob.lower()
    if "风控" in blob or "人机验证" in blob or "rgv587" in low or "x5secdata" in low:
        return "auth"
    if "登录态缺失" in blob or "profile" in low:
        return "auth"
    if "超时" in blob or "timeout" in low:
        return "transient"
    return ""


def verify() -> tuple:
    """凭据自检（不启动浏览器、不联网）：profile 在 = 已登录。

    ⚠️ 诚实说明：真正的活体验证必须开浏览器；此处与 deepseek 安全后端同构，
    只做「门在不在」的检查，避免 --check 顺手拉起一个 chromium。
    """
    if not BACKEND.exists():
        return "unknown", f"安全后端不存在: {BACKEND}"
    if not _PROFILE.exists():
        return "dead", (
            "浏览器登录态缺失（profile 不存在）——请跑："
            "python3 <fp数据目录>/public/plugins/webai/lib/qwen_login.py"
        )
    return "ok", ""
