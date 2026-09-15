"""
DeepSeek provider (chat.deepseek.com)
======================================

chat / search / vision 同样共用一个 completion 端点：
    chat    parent_message_id = 上轮 message_id，服务端自持上下文
    search  search_enabled=True，SSE 里 response/search_results 给结构化引用
    vision  先传 /api/v0/file/upload_file，再把 file_id 放进 ref_file_ids

难点是 PoW：**每次请求**都要带 x-ds-pow-response，由 sha3_wasm_bg.wasm 求解
（DeepSeekHashV1，难度约 1e5，耗时 ~0.03s）。
    ⚠️ 挑战是一次性的，且 target_path 随端点而变：
       对话 → /api/v0/chat/completion
       上传 → /api/v0/file/upload_file
    ⚠️ 不能见 "FINISHED" 就断流 —— response/search_status 也会回 FINISHED，
       开搜索时会提前退出、正文全丢；只认 response/status。

可选钩子（PROVIDER_SPEC.md）：CAPABILITY_MAP={}（无生成类能力）、models()=[]（无公开枚举
接口，诚实返回空）、probe() 一律 supported=None、classify() 认本家方言 rate_limit_reached→quota；
无异步任务故不实现 poll()。SSE 解析把出现过的每个通道收进 phases —— 未知 phase/event 不丢。

凭据：~/.deepseek_token（来自浏览器 localStorage userToken.value）。
刷新：运行 deepseek_login.py 走一次 Playwright 登录。
"""

import base64
import json
import os
import pathlib
import struct
import threading
import time

import requests

name = "deepseek"
capabilities = {"chat", "search", "vision"}
default_model = "default"

# 能力名 → 请求方言（PROVIDER_SPEC.md §1）。
# DeepSeek 没有生成类能力（t2i / t2v / slides / web_dev …），请求体里也没有
# chat_type / assistant_id 这类方言参数 —— 用户能拨的只有 think / search 两个开关。
# 空表是诚实的答案：不编造不存在的方言。
CAPABILITY_MAP: dict = {}

DST = "https://chat.deepseek.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

TOKEN_FILE = pathlib.Path(os.path.expanduser("~/.deepseek_token"))
CACHE_DIR = pathlib.Path(os.path.expanduser("~/.local/share/fp/deepseek"))
WASM_FILE = CACHE_DIR / "sha3_wasm_bg.wasm"
WASM_URL = "https://fe-static.deepseek.com/chat/static/sha3_wasm_bg.7b9ca65ddd.wasm"

_LOCK = threading.RLock()
_HTTP_TIMEOUT = 20
_STREAM_TIMEOUT = 300

_POW_CHAT = "/api/v0/chat/completion"
_POW_UPLOAD = "/api/v0/file/upload_file"


# ── 凭据 ────────────────────────────────────────────────────────

def load_token() -> str:
    """读 token。未登录时 localStorage 里是 {"value":null} 游客占位符，必须校验长度。"""
    tok = os.environ.get("DEEPSEEK_TOKEN", "").strip()
    if not tok:
        try:
            tok = TOKEN_FILE.read_text().strip()
        except (FileNotFoundError, PermissionError):
            return ""
    if tok.startswith("{"):
        try:
            tok = (json.loads(tok) or {}).get("value") or ""
        except json.JSONDecodeError:
            return ""
    return tok if isinstance(tok, str) and len(tok) > 40 else ""


def available():
    if not load_token():
        return False, ("未找到有效 DeepSeek token。请运行登录脚本重新登录：\n"
                       "    python3 <fp数据目录>/public/webai/deepseek_login.py")
    try:
        import wasmtime  # noqa: F401
    except ImportError:
        return False, "缺少 wasmtime（PoW 求解依赖）: pip install wasmtime"
    return True, ""


# ── PoW 求解器 ──────────────────────────────────────────────────

class _PowSolver:
    """sha3_wasm_bg.wasm 的封装（wasm 实例非线程安全，全局复用 + 加锁）"""

    def __init__(self, wasm_path: pathlib.Path):
        import wasmtime

        eng = wasmtime.Engine()
        mod = wasmtime.Module.from_file(eng, str(wasm_path))
        self.store = wasmtime.Store(eng)
        inst = wasmtime.Instance(self.store, mod, [])
        self.ex = inst.exports(self.store)
        self.mem = self.ex["memory"]

    def _put_str(self, s: str):
        """字符串必须经 malloc 写入 —— 自己挑地址会落进 wasm 栈区被自身栈帧覆盖"""
        b = s.encode()
        ptr = self.ex["__wbindgen_export_0"](self.store, len(b), 1)
        self.mem.write(self.store, b, ptr)
        return ptr, len(b)

    def solve(self, challenge: str, salt: str, expire_at: int, difficulty: float) -> int:
        prefix = f"{salt}_{expire_at}_"
        pc, lc = self._put_str(challenge)
        pp, lp = self._put_str(prefix)
        asp = self.ex["__wbindgen_add_to_stack_pointer"]
        ret = asp(self.store, -16)  # retptr 必须取自 wasm 栈
        try:
            self.ex["wasm_solve"](self.store, ret, pc, lc, pp, lp, float(difficulty))
            disc = struct.unpack("<i", bytes(self.mem.read(self.store, ret, ret + 4)))[0]
            val = struct.unpack("<d", bytes(self.mem.read(self.store, ret + 8, ret + 16)))[0]
        finally:
            asp(self.store, 16)
        if disc == 0:
            raise RuntimeError("PoW 无解（challenge 可能已过期）")
        return int(val)


_solver = None


def _ensure_wasm() -> pathlib.Path:
    """wasm 缺失时从官方静态资源下载（26KB，零 imports 纯函数）"""
    if WASM_FILE.exists() and WASM_FILE.stat().st_size > 1000:
        return WASM_FILE
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    r = requests.get(WASM_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    if len(r.content) < 1000:
        raise RuntimeError(f"wasm 下载异常（{len(r.content)} 字节）")
    tmp = WASM_FILE.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    tmp.replace(WASM_FILE)
    return WASM_FILE


def _get_solver() -> _PowSolver:
    global _solver
    with _LOCK:
        if _solver is None:
            _solver = _PowSolver(_ensure_wasm())
        return _solver


# ── HTTP ────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    """DeepSeek 也有 WAF —— 裸请求会被拦成 429"""
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Authorization": f"Bearer {token}",
        "Origin": DST,
        "Referer": f"{DST}/",
        "Content-Type": "application/json",
        "sec-ch-ua": '"Chromium";v="140", "Not(A:Brand";v="24"',
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-site": "same-origin",
    }


def _pow_header(sess: requests.Session, token: str, target_path: str) -> str:
    r = sess.post(
        f"{DST}/api/v0/chat/create_pow_challenge",
        headers=_headers(token),
        json={"target_path": target_path},
        timeout=_HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"取 PoW 挑战失败: HTTP {r.status_code}")
    ch = r.json()["data"]["biz_data"]["challenge"]
    ans = _get_solver().solve(ch["challenge"], ch["salt"], ch["expire_at"], ch["difficulty"])
    payload = {
        "algorithm": ch["algorithm"],
        "challenge": ch["challenge"],
        "salt": ch["salt"],
        "answer": ans,  # 必须整数
        "signature": ch["signature"],
        "target_path": target_path,
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


# ── 会话 ────────────────────────────────────────────────────────

def new_session(model: str = "") -> str:
    """⚠️ 建会话绝不能带 pow 头（挑战一次性，带了 completion 就废）"""
    token = load_token()
    r = requests.Session().post(
        f"{DST}/api/v0/chat_session/create",
        headers=_headers(token), json={}, timeout=_HTTP_TIMEOUT,
    )
    if r.status_code == 401:
        raise RuntimeError("DeepSeek 鉴权失败（token 可能过期，请重新登录）")
    js = r.json()
    if js.get("code") != 0:
        raise RuntimeError(f"建会话失败: {js.get('msg') or js}")
    return js["data"]["biz_data"]["id"]


# ── 文件上传 ────────────────────────────────────────────────────

def upload(path: str) -> dict:
    """上传文件，返回 {ref: file_id, name, size}。ref 直接放进 ask(files=[...])。"""
    token = load_token()
    with open(path, "rb") as f:
        data = f.read()
    filename = os.path.basename(path)

    sess = requests.Session()
    pow_hdr = _pow_header(sess, token, _POW_UPLOAD)
    headers = {**_headers(token), "x-ds-pow-response": pow_hdr}
    headers.pop("Content-Type", None)  # 交给 requests 生成 multipart 边界
    headers.update({
        "x-file-size": str(len(data)),
        "x-model-type": default_model,
        "x-thinking-enabled": "0",
    })

    r = sess.post(
        f"{DST}/api/v0/file/upload_file",
        headers=headers,
        files={"file": (filename, data, "application/octet-stream")},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"上传失败 HTTP {r.status_code}: {r.text[:200]}")
    biz = (r.json().get("data") or {}).get("biz_data") or {}
    fid = biz.get("id")
    if not fid:
        raise RuntimeError(f"上传返回异常: {str(r.json())[:200]}")

    # 官方前端会轮询 fetch_files 等文件处理完成；这里轻量轮询，失败不阻断
    for _ in range(6):
        try:
            q = sess.get(f"{DST}/api/v0/file/fetch_files",
                         headers=_headers(token), params={"file_ids": fid}, timeout=15)
            files = ((q.json().get("data") or {}).get("biz_data") or {}).get("files") or []
            if files and str(files[0].get("status", "")).upper() in ("SUCCESS", "READY"):
                break
        except Exception:  # noqa: BLE001
            break
        time.sleep(1)

    return {"ref": fid, "name": filename, "size": len(data)}


# ── SSE 解析 ────────────────────────────────────────────────────

def _stream_finished(line: str) -> bool:
    """这一行是否是"主响应结束"信号。

    ⚠️ 服务端会回**多个** FINISHED：检索阶段的 `response/search_status` 也回 FINISHED。
    只认主路径 `response/status` —— 否则开搜索时会在检索阶段就提前断流、正文全丢。
    （`response/search_status` 不含子串 `response/status`，下面的判定天然把它们分开。）
    """
    return '"response/status"' in line and '"FINISHED"' in line


def _parse_sse(raw: str) -> dict:
    """解析 completion 的 SSE 流。

    粘性路径：``p`` 一旦出现就沿用（后续帧可能省略 ``p``）。
    首块正文内嵌在第一个 response 快照的 content 里，不走 delta 流 ——
    只收 delta 会每轮丢开头。

    返回**老键**（text / thinking / message_id / references / queries），
    并新增可选键：

        phases  流里出现过的每一个通道及累计内容。DeepSeek 的 phase 等价物是 SSE 的
                粘性路径 ``p``（如 ``response/content``）；``event:`` 名与帧 ``type``
                也一并留痕。**不做"已知 phase 白名单"筛流** —— 白名单会静默丢产物/新通道。
        model   服务端实际使用的 model_type（ready 帧给出）。
        extra   首个 response 快照原样留档（不加工）。

    正文（text）仍只取 ``response/content``（含首块快照 seed），thinking 取含
    ``thinking`` 的路径 —— **老行为一字未改**，新增的只是"不丢"。
    """
    buf, think = [], []
    msg_id, status, path, err, search_results, model = None, None, None, None, None, ""
    phases = {}   # 通道名 → 累计内容（未知通道同样留痕）
    extra = {}    # 首个 response 快照，原样

    def _mark(channel: str, content: str = "") -> None:
        """登记一个通道。未知通道也登记 —— 这是"不丢"的唯一保证。"""
        if channel:
            phases[channel] = phases.get(channel, "") + content

    for line in raw.split("\n"):
        # SSE 的 event 名也是流的一部分 —— 未知 event 同样不能丢
        if line.startswith("event:"):
            _mark(line[6:].strip())
            continue
        if not line.startswith("data: "):
            continue
        try:
            obj = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        if obj.get("type") == "error":
            err = obj.get("content") or err
            _mark("error", str(obj.get("content") or ""))
            if "rate_limit" in str(obj.get("finish_reason") or "").lower():
                raise RuntimeError(f"限流: {err} （稍等 20-30s 再试）")
            continue

        # ready 帧：response_message_id 与真实 model_type（老解析忽略了它）
        if "response_message_id" in obj or "model_type" in obj:
            msg_id = obj.get("response_message_id") or msg_id
            model = obj.get("model_type") or model

        # 帧自带的 type（非 error）也留痕 —— 未知类型不丢
        if obj.get("type"):
            _mark(str(obj["type"]))

        if "p" in obj:
            path = obj["p"]
            if path == "response/search_results":
                sr = obj.get("v")
                if isinstance(sr, list):
                    search_results = sr
            _mark(path)  # 出现即登记（v 未必是字符串）

        v = obj.get("v")

        # delta 帧：按（粘性）path 累积 —— 不做 phase 白名单
        if isinstance(v, str) and path:
            _mark(path, v)

        if isinstance(v, dict) and "response" in v:
            ro = v["response"] or {}
            if not extra:
                extra = ro  # 首个快照原样留档
            msg_id = ro.get("message_id") or msg_id
            status = ro.get("status") or status
            c0 = ro.get("content")
            if isinstance(c0, str) and c0 and not buf:
                buf.append(c0)  # 只 seed 首块
            t0 = ro.get("thinking_content")
            if isinstance(t0, str) and t0 and not think:
                think.append(t0)
        elif isinstance(v, str):
            if path and "thinking" in path:
                think.append(v)
            elif path and path.endswith("status"):
                status = v
            else:
                buf.append(v)

    if err and not buf:
        raise RuntimeError(f"DeepSeek 拒绝: {err}")

    out = {
        "text": "".join(buf),
        "thinking": "".join(think),
        "message_id": msg_id,
        "references": [{"url": d.get("url", ""), "title": d.get("title", ""),
                        "snippet": d.get("snippet", "")} for d in (search_results or [])],
        "queries": [],
        "phases": phases,
        "model": model,
    }
    if extra:
        out["extra"] = extra
    return out


# ── 发消息 ──────────────────────────────────────────────────────

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
    token = load_token()
    if not token:
        raise RuntimeError("未找到有效 DeepSeek token（见 available() 提示）")

    ref_ids = []
    for f in files or []:
        ref_ids.append(f["ref"] if isinstance(f, dict) else f)

    body = {
        "chat_session_id": session_id,
        "parent_message_id": parent_message_id,
        "model_type": model or default_model,
        "prompt": text,
        "ref_file_ids": ref_ids,
        "thinking_enabled": bool(think),
        "search_enabled": bool(search),
        "action": None,
        "preempt": False,
    }

    sess = requests.Session()
    last_err = None
    for attempt in range(retries):
        hdr = _pow_header(sess, token, _POW_CHAT)  # 每次请求都要新的 pow
        raw = ""
        try:
            with sess.post(
                f"{DST}/api/v0/chat/completion",
                headers={**_headers(token), "x-ds-pow-response": hdr},
                json=body, stream=True, timeout=_STREAM_TIMEOUT,
            ) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"completion HTTP {resp.status_code}")
                for line in resp.iter_lines(decode_unicode=True):
                    if line is None:
                        continue
                    raw += line + "\n"
                    # ⚠️ search_status 也会回 FINISHED，只认 response/status
                    #    （判定集中在 _stream_finished，便于离线自测）
                    if _stream_finished(line):
                        break
            return _parse_sse(raw)
        except RuntimeError as e:
            last_err = e
            low = str(e).lower()
            if "限流" not in str(e) and "rate" not in low and "frequent" not in low:
                raise
            if attempt < retries - 1:
                time.sleep(retry_delay)
    raise last_err if last_err else RuntimeError("未知错误")


# ── 便捷：一次性联网搜索 ────────────────────────────────────────

def search(query: str, *, think: bool = False, model: str = "") -> dict:
    sid = new_session(model)
    r = ask(sid, query, model=model, think=think, search=True)
    # 原样带上 ask() 的**所有**键（含将来新增的），只补 session_id。
    # 之前这里手写 5 个键 —— 新增的 assets/phases/model/extra 会被静默丢掉，
    # 正是"未知的东西不能丢"在 search() 边界上被违反。
    out = dict(r)
    out.setdefault("session_id", sid)
    return out


# ── 可选钩子（PROVIDER_SPEC.md §1/§3/§4/§6）──────────────────────
# 都是"缺了自动降级"的可选实现，不影响老契约 ask / search / upload。

def models() -> list:
    """DeepSeek 没有公开的模型枚举接口 —— 诚实返回空表。

    实测请求体里只有一个 ``model_type: "default"``；用户能拨的其实是思考(think) /
    联网(search) 两个**开关**，而不是"选模型"。硬编 "deepseek-chat" 之类的名字
    只会随时间过期，故返回 ``[]``（不是错误）—— 调用方不该把可选能力当契约用。
    """
    return []


def probe(capability: str = "") -> dict:
    """DeepSeek 没有可读的能力/模型配置接口，只能"实际发一次看报什么错"。

    一律返回 ``supported=None``（不知道就是不知道），不猜。``__init__.probe()``
    会照常收下这个结果。
    """
    return {
        "supported": None,
        "via": "none",
        "note": "无公开枚举接口；能力(chat/search/vision)只能靠实发一次从报错推断。",
    }


def classify(text: str = "", status: int = 0, raw=None, exc=None, **_):
    """错误归类。只覆盖本家方言；其余返回空串，由 ``__init__.classify()`` 落到通用词表。

    本家方言（实测）：限流。服务端回
        ``{"type":"error","content":"Messages too frequent. Try again later.",
           "finish_reason":"rate_limit_reached"}``
    ``rate_limit_reached`` 带下划线，通用词表里的 "rate limit" / "ratelimit" 都匹配不到
    → 会被误判成 UNKNOWN，导致调用方做错的重试决策。故在此归 ``quota``。

    非方言文本返回 ``""``（假值）：``__init__.classify()`` 见假值会自动落到
    ``core.classify_text()`` —— 通用词表只维护那一份，不在这里复制第二份。
    """
    blob = text or (str(exc) if exc is not None else "")
    if isinstance(raw, dict):
        blob += " " + str(raw.get("finish_reason") or "")
    low = blob.lower()
    if "rate_limit" in low or "frequent" in low:
        return "quota"
    return ""


# ── 凭据自检（login.py 的 --check / 静默刷新后校验用）─────────────

def verify() -> tuple:
    """轻量探测：凭据是否被服务端认可。→ ("ok","") / ("dead",why) / ("unknown",why)

    用 new_session() 做探针 —— 它是真实的鉴权调用（对比：qwen/glm 用只读 GET）。
    代价是会在服务端留下一个空会话，但比"探测端点猜错"可靠。
    非 AUTH 类的失败一律 unknown → 不触发自动登录（宁可漏刷，不可误刷）。
    """
    try:
        new_session()
    except Exception as e:  # noqa: BLE001
        try:
            kind = str(classify(exc=e, text=str(e)) or "")
        except Exception:  # noqa: BLE001
            kind = ""
        if "auth" in kind.lower():
            return "dead", f"{type(e).__name__}: {e}"
        return "unknown", f"{type(e).__name__}: {e}"
    return "ok", ""
