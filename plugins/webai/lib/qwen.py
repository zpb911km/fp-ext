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

⚠️ 能力范围（2026-10-19 逆向页面后补全）：浏览器后端驱动「模型下拉（左上角
   ``.wms-trigger``）+ 加号菜单（``div.mode-select-open``：生成图像/创建视频/
   网页搜索/深入研究/网页开发/幻灯片/上传附件）+ 思考档下拉（自动/思考/快速）」。
   CAPABILITY_MAP 的方言值是 ``mode=<加号菜单条目 id>``，由 backend 翻译成页面文本。
   逐项实测通过的能力才进 ``capabilities``（见 probe() 的 supported 判定）。
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
# 只声明逐项实测通过的能力（实测记录见 qwen_safe_backend/README.md 与 probe()）。
# 2026-10-19 活体验收：chat / search / vision / attach 全部通过。
capabilities = {"chat", "search", "vision", "attach"}
default_model = "Qwen3.7-Plus"   # 页面左上角默认选中项（旧 API id qwen3.7-plus 模糊等价）
# 能力名 → 方言：浏览器通道的方言是加号菜单条目（backend PLUS_MODES 翻译）。
# 对齐旧 API 通道：t2i/t2v/slides/web_dev/deep_research ↔ chat_type。
CAPABILITY_MAP = {
    "t2i": {"mode": "t2i"},
    "t2v": {"mode": "t2v"},
    "slides": {"mode": "slides"},
    "web_dev": {"mode": "web_dev"},
    "deep_research": {"mode": "deep_research"},
    # 旧 API 通道没有的三个页面模式（藏在加号菜单「更多」二级里）
    "preview": {"mode": "preview"},
    "study": {"mode": "study"},
    "travel": {"mode": "travel"},
    "search": {"mode": "search"},  # 页面上「网页搜索」是模式（不是开关）
}

# 会话级模式（能力是会话属性：new_session 定, ask 沿用）
_SESSION_MODE: dict[str, str] = {}
# 生成类模式同步等待上限（浏览器通道无异步任务队列, 只能等页面）
_MODE_TIMEOUT = {"t2v": 900, "slides": 900, "deep_research": 1200,
                 "t2i": 420, "web_dev": 600, "preview": 420,
                 "study": 600, "travel": 600}

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


def new_session(model: str = "", capability: str = "", mode: str = "",
                **dialect) -> str:
    """会话 = 浏览器里的一个对话。真实新建发生在本会话的第一次 ask（new_chat）。

    capability 方言由 ``__init__.new_session`` 从 CAPABILITY_MAP 展开传入
    （``mode`` = 加号菜单条目 id）；多余的键一律容忍。
    """
    sid = f"qw-{uuid.uuid4().hex[:16]}"
    _FRESH.add(sid)
    # 能力必须点名支持：CAPABILITY_MAP 里没有的绝不静默降级（__init__ 层会兜住异常）
    if capability and capability not in CAPABILITY_MAP:
        raise RuntimeError(
            f"qwen 浏览器后端不支持能力 {capability!r}"
            f"（已支持: {sorted(CAPABILITY_MAP)}）")
    m = mode or (dialect.get("mode") or "")
    if m:
        _SESSION_MODE[sid] = m
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
    think: (bool | str) = False,
    search: bool = False,
    files: list | None = None,
    mode: str = "",
    retries: int = 1,
    retry_delay: int = 15,
    **kwargs,
) -> dict:
    """一轮问答。浏览器后端与 API 版的区别：无 phase 流、无 message_id、
    无 queries（页面不暴露）—— 拿不到就留空，不猜。

    model：页面左上角下拉选（页面名或旧 API id，模糊匹配）；
    think：思考档切「思考」；mode/会话模式：加号菜单选生成模式
    （t2i/t2v/slides/web_dev/deep_research）。
    """
    mode = mode or _SESSION_MODE.get(session_id, "")

    # 附件：upload() 返回延迟引用（文件在页面内真实上传），这里抽出本地路径透传。
    paths: list = []
    if files:
        for f in files:
            p = f if isinstance(f, str) else (
                (f or {}).get("path") or (f or {}).get("local") or "")
            if p:
                paths.append(p)

    timeout = _MODE_TIMEOUT.get(mode, 300)
    args = ["ask", text, "--timeout", str(timeout)]
    if search:
        args.append("--search")
    if think:
        # bool → thinking；字符串 → 直接给档名（thinking/auto/fast）
        args += ["--think", think if isinstance(think, str) else "thinking"]
    if mode:
        args += ["--mode", mode]
    if model:
        args += ["--model", model]
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
                "model": res.get("model") or default_model,
                "phases": {"answer": answer},
                # 页面侧真实状态（浏览器通道独有）：模式芯片、子配置取值
                "mode": res.get("mode") or "",
                "configs": res.get("configs") or [],
            }
            # 生成物 → assets（core.coerce_assets 认 kind+url 字典）
            assets = [{"kind": "image", "url": u} for u in res.get("images") or []]
            assets += [{"kind": "video", "url": u} for u in res.get("videos") or []]
            assets += [{"kind": "file", "url": u} for u in res.get("files_out") or []]
            if assets:
                out["assets"] = assets
            # 附件卡片（网页开发/幻灯片等产物在页面上只是一张卡，没有直链）
            att = res.get("attachments") or []
            if att:
                out["attachments"] = att
            # slides / deep_research 是**交互式**：首轮只出大纲，同一 session 再发
            # 一次「确认」才生成（对齐旧 API 的 _INTERACTIVE_CHAT_TYPES 语义）
            if (res.get("mode") in ("幻灯片", "深入研究")) and not att:
                out["needs_input"] = True
            extra = {}
            if res.get("waf_retries"):
                extra["waf_challenges"] = res["waf_retries"]
            if res.get("mode"):
                extra["mode"] = res["mode"]
            if res.get("configs"):
                extra["configs"] = res["configs"]
            if res.get("pending"):
                # 异步产物（t2v）：提交成功、视频还在生成 → 用 assets() 轮询同一对话
                extra["pending"] = True
                extra["chat_url"] = res.get("url") or ""
            if extra:
                out["extra"] = extra
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


def models(force: bool = False) -> list:
    """模型清单：从页面左上角下拉**真实读取**（一级 3 个 + 「展开更多模型」二级 4 个）。

    异常/读不到 → []（诚实）。返回 [{"id","name","capabilities","ctx","default","tier"}]。
    ⚠️ 会拉起浏览器会话（daemon 不在就冷启动），调用方注意成本。
    """
    try:
        res = _run_backend(["models"] + (["--force"] if force else []), wait=240)
    except Exception:  # noqa: BLE001
        return []
    if not res.get("ok"):
        return []
    return [{"id": m.get("name", ""), "name": m.get("name", ""),
             "capabilities": set(m.get("capabilities") or []),
             "ctx": int(m.get("ctx") or 0), "default": bool(m.get("default")),
             "desc": m.get("desc", ""), "tier": int(m.get("tier") or 1)}
            for m in res.get("models") or [] if m.get("name")]


def pin_model(name: str) -> dict:
    """把模型「设为默认」（页面上 .wms-list__pin-action）。

    ⚠️ 这是**改账号设置**的动作，只在明确需要时调用；已钉住返回 {"pinned": "already"}。
    """
    res = _run_backend(["pin", name], wait=240)
    if not res.get("ok"):
        raise RuntimeError(str(res.get("err") or "设为默认失败"))
    return {"pinned": res.get("pinned", "")}


def configs() -> dict:
    """读当前模式的子配置（比例/分辨率/研究深度…）与全部可选值。

    返回 {"mode": "生成图像", "thinking": "自动",
          "configs": [{"index":0,"value":"Qwen-Image 2.0","options":[...]}, ...]}
    """
    res = _run_backend(["configs"], wait=240)
    if not res.get("ok"):
        raise RuntimeError(str(res.get("err") or "读取子配置失败"))
    return {"mode": res.get("mode", ""), "thinking": res.get("thinking", ""),
            "configs": res.get("configs") or []}


def probe(capability: str = "") -> dict:
    """§4：报告本家能力。**只把活体验收过的写 supported=True**（2026-10-19 实测）。"""
    caps = {
        "chat": {"supported": True, "via": "browser"},
        "search": {"supported": True, "via": "browser:mode",
                   "note": "页面上「网页搜索」是模式（不是开关）；ask(search=True) 已接，"
                           "返回 citation 主机名 + 正文 URL 作 references"},
        "vision": {"supported": True, "via": "browser:upload",
                   "note": "页面内真实上传（加号→上传附件→filechooser）后提问，已实测答对图内容"},
        "attach": {"supported": True, "via": "browser:upload",
                   "note": "同上；上传成功后芯片计数校验，不等芯片齐就报错不盲发"},
        "model_select": {"supported": True, "via": "browser:dom",
                         "note": "左上角 .wms-trigger 下拉是**多级**：一级 3 个 + "
                                 "「展开更多模型」hover 出的二级 4 个（共 7 个）；"
                                 "models() 全读，ask(model=) 两级都能选"},
        "model_pin": {"supported": True, "via": "browser:dom",
                      "note": "条目内「设为默认」= 账号级默认模型；pin_model() 显式调用才动"},
        "thinking": {"supported": True, "via": "browser:dom",
                     "note": "输入区「自动/思考/快速」三档；**页面级**设置，新对话不重置；"
                             "思考正文要点开「已经完成思考」卡片才在 DOM 里"},
        "t2i": {"supported": True, "via": "browser:mode",
                "note": "「生成图像」模式；产物是 cdn 图片 URL（assets）。"
                        "子配置：模型(Qwen-Image 3.0/2.0) + 比例(7 档)"},
        "slides": {"supported": True, "via": "browser:mode", "interactive": True,
                   "note": "「幻灯片」**交互式**：首轮只出大纲(needs_input=True)，"
                           "同一 session 再发「确认」才产出附件卡 + 预览图"},
        "web_dev": {"supported": False, "via": "browser:mode", "partial": True,
                    "note": "「网页开发」模式能选能跑，产物在页面上只是一张附件卡"
                            "（无直链、无 iframe srcdoc、点预览无反应），正文取不到 → "
                            "暂不对外声明"},
        "deep_research": {"supported": False, "via": "browser:mode", "partial": True,
                          "note": "「深入研究」模式与深度子配置(正常/高级)已接线，"
                                  "一次完整长任务未验收 → 暂不声明"},
        "t2v": {"supported": True, "via": "browser:mode", "async": True,
                "note": "「创建视频」模式 + 模型子配置（HappyHorse 1.1/1.0，单条 30 积分）。"
                        "**异步**：提交即回「生成好后通知你」，实测 ~11 分钟后视频才落进对话"
                        "→ ask() 返回 extra.pending=True，再用 assets() 轮询同一条对话"},
        "preview": {"supported": False, "via": "browser:mode", "partial": True,
                    "note": "「预览模式」（加号菜单→更多）可选中，产物形态未验收"},
        "study": {"supported": False, "via": "browser:mode", "partial": True,
                  "note": "「学习」（加号菜单→更多）可选中，产物形态未验收"},
        "travel": {"supported": False, "via": "browser:mode", "partial": True,
                   "note": "「旅行规划师」（加号菜单→更多）可选中，产物形态未验收"},
    }
    if capability:
        return caps.get(capability, {"supported": None, "note": "未探测该能力"})
    return caps


def assets() -> dict:
    """读**当前对话**最后一条回答的产物（不发消息，风控压力≈0）。

    用途：t2v 是异步的（提交只回「生成好后通知你」，实测 ~11 分钟后视频才落进对话），
    提交后隔一会儿轮询本函数直到 `videos` 非空。
    返回 {"url","mode","answer","images","videos","files","attachments"}。
    """
    res = _run_backend(["assets"], wait=240)
    if not res.get("ok"):
        raise RuntimeError(str(res.get("err") or "读取产物失败"))
    return {k: res.get(k) for k in
            ("url", "mode", "answer", "images", "videos", "files", "attachments")}


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
