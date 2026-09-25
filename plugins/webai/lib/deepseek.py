"""DeepSeek provider —— 安全后端版（真实浏览器通道）。

历史：旧实现直接伪造 chat.deepseek.com 的 API/PoW 请求，导致账号被风控封禁
（详见 ~/.local/share/fp/private/memory/webai_provider_rule.md）。本文件是替换后端：
所有问答经由隔离的安全后端（真实 Chrome + Xvfb 懒启动 daemon，见 backend.py），
不再发出任何伪造请求，也不再依赖 token/wasmtime/requests。本目录即安全后端本体：

契约（PROVIDER_SPEC.md 铁律）保持不变：
  ask() -> dict{text, thinking, message_id, references, queries, ...}
  search() / upload() / available() / verify() / models() / probe() / classify()
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

name = "deepseek"
# attach/vision：均经页面内真实上传（input[type=file] → 前端 PoW + /file/upload_file，
# 出站 completion 自动带 ref_file_ids）。双实测背书：
#   .md 文档 → 模型逐字读出内容；.png 图片 → 正确描述"紫色背景上的绿色三角形"。
capabilities = {"chat", "search", "attach", "vision"}
default_model = "default"
CAPABILITY_MAP: dict = {}

# 安全后端入口（可用 DS_BACKEND 覆盖）——随插件 lib 内嵌，路径相对本模块
BACKEND = pathlib.Path(os.environ.get(
    "DS_BACKEND",
    str(pathlib.Path(__file__).resolve().parent
        / "deepseek_safe_backend" / "backend.py"),
))
_CHROME = pathlib.Path.home() / ".local/opt/google-chrome/opt/google/chrome/chrome"
_PROFILE = pathlib.Path.home() / ".local/share/fp/webai/deepseek/profile"

# 会话新鲜度：new_session() 登记的 sid，首次 ask 时让后端开新对话
_FRESH: set[str] = set()


# ── 凭据/状态 ────────────────────────────────────────────────────

def load_token() -> str:
    """新通道不使用 token（登录态在浏览器 profile 里）。保留签名兼容旧调用方。"""
    return ""


def available():
    if not BACKEND.exists():
        return False, f"安全后端不存在: {BACKEND}"
    if not _PROFILE.exists():
        return False, ("DeepSeek 浏览器登录态缺失。请运行登录脚本重新登录：\n"
                       "    python3 <fp数据目录>/public/plugins/webai/lib/deepseek_login.py")
    if not _CHROME.exists():
        return False, f"找不到真 Chrome: {_CHROME}"
    return True, ""


# ── 会话 ─────────────────────────────────────────────────────────

def new_session(model: str = "", capability: str = "", **dialect) -> str:
    sid = f"ds-{uuid.uuid4().hex[:16]}"
    _FRESH.add(sid)
    return sid


# ── 发消息 ───────────────────────────────────────────────────────

def _run_backend(args: list[str], wait: int) -> dict:
    """调用安全后端 CLI，返回其 JSON。DS_QUIET=1 保证 stdout 纯净。"""
    env = dict(os.environ)
    env["DS_QUIET"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, str(BACKEND), *args],
            capture_output=True, text=True, timeout=wait, env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("安全后端调用超时") from None
    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()[-400:]
        raise RuntimeError(f"安全后端无输出(exit={proc.returncode}): {err}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # 兜底：定位第一个 JSON 对象
        i = out.find("{")
        if i >= 0:
            try:
                return json.loads(out[i:])
            except json.JSONDecodeError:
                pass
        raise RuntimeError(f"安全后端输出无法解析: {out[:200]}")


def ask(
    session_id: str,
    text: str,
    *,
    parent_message_id=None,
    model: str = "",
    think: bool = False,
    search: bool = False,
    files: list | None = None,
    retries: int = 2,
    retry_delay: int = 20,
) -> dict:
    # 附件：upload() 返回延迟引用（文件在页面内上传），这里抽出本地路径透传给后端。
    # 互斥规则（前端 v9 同款）：文件在场 → 搜索禁用。
    paths: list = []
    warnings: list = []
    if files:
        for f in files:
            p = f if isinstance(f, str) else (
                (f or {}).get("path") or (f or {}).get("local") or "")
            if p:
                paths.append(p)
        if search and paths:
            search = False
            warnings.append("deepseek 文件与智能搜索互斥：已关闭搜索（前端同款规则）")
    if model and model != default_model:
        # 不静默忽略：页面 model_type 只有 default 开放（enabled&&switchable），
        # 后端 ask 链路也不切换页面模型 —— 传别的 id 必然答非所问，宁可拒绝。
        raise RuntimeError(
            f"deepseek 不支持 model={model!r}：网页版当前仅开放 "
            f"{default_model!r}（快速模式），可选清单见 webai.models('deepseek')"
        )
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
        res = _run_backend(args, wait=timeout + 180)
        if res.get("ok"):
            answer = res.get("answer") or ""
            blocks = res.get("thinking_blocks") or []
            thinking = "\n".join(
                (b.get("head") or "").strip() for b in blocks if b.get("head")
            )
            out = {
                "text": answer,
                "thinking": thinking,
                "message_id": f"ds-{int(time.time() * 1000)}",
                "references": res.get("references") or [],
                "queries": [],
                "model": default_model,
                "phases": {"answer": answer},
            }
            if thinking:
                out["phases"]["thinking"] = thinking
            if res.get("search_blocked_by_files") and search:
                warnings.append("deepseek 文件与智能搜索互斥：已关闭搜索")
            if warnings:
                out["warnings"] = warnings
            return out
        err = str(res.get("err") or "未知错误")
        last_err = RuntimeError(err)
        low = err.lower()
        transient = ("frequent" in low) or ("节奏保护" in err) or ("超时" in err)
        if not transient or attempt >= retries - 1:
            break
        time.sleep(retry_delay)
    raise last_err if last_err else RuntimeError("安全后端未知错误")


def search(query: str, *, think: bool = False, model: str = "",
           files: list | None = None) -> dict:
    sid = new_session(model)
    r = ask(sid, query, model=model, think=think, search=True, files=files)
    # 与旧契约一致：原样带上 ask() 的所有键，只补 session_id。
    out = dict(r)
    out.setdefault("session_id", sid)
    return out


def upload(path: str) -> dict:
    """延迟引用：deepseek 的真实上传发生在页面内（PoW 必须由前端计算，
    历史伪造请求曾导致封号），这里只做校验并返回本地引用，
    ask() 收到后经 set_input_files 在安全后端真正上传。"""
    p = pathlib.Path(path).expanduser().resolve()
    if not p.is_file():
        raise RuntimeError(f"文件不存在: {p}")
    if p.stat().st_size > 100 * 1024 * 1024:
        raise RuntimeError(f"超过 100MB 上限: {p.name}")
    return {"kind": "local", "path": str(p), "name": p.name, "deferred": True}


# ── 可选钩子（PROVIDER_SPEC.md §3/§4/§6）────────────────────────

def models() -> list:
    """枚举可选模型。权威来源：安全后端 `models` 命令读**页面自身写下的**
    `model_configs`（localStorage `__ds_remote_feature_store_model`，缓存缺失时
    才让页面自己 fetch `client/settings`）——全程真实浏览器上下文，零伪造请求。

    返回规范形状 `[{"id","name","capabilities","ctx","default"}]`（PROVIDER_SPEC §0.2）。
    只列前端自己会放出的项（`enabled && switchable`，与 JS `h()` 的筛选一致）；
    任何异常 → `[]`（诚实：拿不到就说没有）。
    """
    try:
        res = _run_backend(["models"], wait=170)
    except Exception:                       # noqa: BLE001
        return []
    if not res.get("ok"):
        return []
    cfgs = res.get("configs")
    items = (cfgs or {}).get("value") if isinstance(cfgs, dict) else cfgs
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = it.get("model_type")
        if not mid:
            continue
        if not (it.get("enabled") and it.get("switchable")):
            continue                        # 前端不会放出的项不列（如 expert/vision 未开放）
        caps = {"chat"}
        if it.get("think_feature") is not None:
            caps.add("thinking")            # 页面有深度思考开关（实测）
        if it.get("search_feature") is not None:
            caps.add("search")              # 页面有智能搜索开关
        d = {"id": mid,
             "name": it.get("name") or mid,
             "capabilities": caps,
             "ctx": 0}                      # 网页版未暴露 context window，不拿字符上限冒充
        if it.get("is_default"):
            d["default"] = True
        out.append(d)
    return out


def probe(capability: str = "") -> dict:
    return {
        "supported": None,
        "via": "none",
        "note": "无公开枚举接口；能力只能靠实发一次从报错推断。",
    }


def classify(text: str = "", status: int = 0, raw=None, exc=None, **_):
    """错误归类。本家方言：限流(rate_limit_reached/frequent)、后端节奏保护 → quota；
    登录态缺失 → auth。其余返回空串，由 __init__ 落到通用词表。"""
    blob = (text or "") + " " + (str(exc) if exc is not None else "")
    if isinstance(raw, dict):
        blob += " " + str(raw.get("finish_reason") or "")
    low = blob.lower()
    if "rate_limit" in low or "frequent" in low or "节奏保护" in blob or "配额" in blob:
        return "quota"
    if "登录态缺失" in blob or "login" in low and "profile" in low:
        return "auth"
    return ""


def verify() -> tuple:
    """凭据自检（不启动浏览器、不联网）：profile 在 = 已登录。"""
    if not BACKEND.exists():
        return "unknown", f"安全后端不存在: {BACKEND}"
    if not _CHROME.exists():
        return "unknown", f"真 Chrome 不存在: {_CHROME}"
    if not _PROFILE.exists() or not any(_PROFILE.iterdir()):
        return "dead", "浏览器登录 profile 缺失（未登录）"
    return "ok", ""
