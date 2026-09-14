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

def _parse_sse(raw: str) -> dict:
    """粘性路径流：p 一旦出现就沿用。

    首块正文内嵌在第一个 response 快照的 content 里，不走 delta 流 ——
    只收 delta 会每轮丢开头。
    """
    buf, think = [], []
    msg_id, status, path, err, search_results = None, None, None, None, None

    for line in raw.split("\n"):
        if not line.startswith("data: "):
            continue
        try:
            obj = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        if obj.get("type") == "error":
            err = obj.get("content") or err
            if "rate_limit" in str(obj.get("finish_reason") or "").lower():
                raise RuntimeError(f"限流: {err} （稍等 20-30s 再试）")
            continue

        if "p" in obj:
            path = obj["p"]
            if path == "response/search_results":
                sr = obj.get("v")
                if isinstance(sr, list):
                    search_results = sr

        v = obj.get("v")
        if isinstance(v, dict) and "response" in v:
            ro = v["response"] or {}
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

    return {
        "text": "".join(buf),
        "thinking": "".join(think),
        "message_id": msg_id,
        "references": [{"url": d.get("url", ""), "title": d.get("title", ""),
                        "snippet": d.get("snippet", "")} for d in (search_results or [])],
        "queries": [],
    }


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
                    if '"response/status"' in line and '"FINISHED"' in line:
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
    return {
        "text": r["text"],
        "references": r["references"],
        "queries": r["queries"],
        "session_id": sid,
        "message_id": r["message_id"],
    }
