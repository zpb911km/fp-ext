"""
Qwen provider (chat.qwen.ai)
=============================

三种能力共用同一个端点，只是标志位不同：
    chat    POST /api/v2/chat/completions      续接靠 chat_id + parent_id
    search  同上，auto_search=True，从 web_search phase 抽结构化引用
    vision  先把文件传到 OSS（getstsToken + oss2），再挂到 message.files

续接机制：parent_id = 上一轮 assistant 的 response_id（SSE 每帧顶层字段），
服务端自持上下文，客户端无需回传历史。

凭据：~/.qwen_cookie 或环境变量 QWEN_COOKIE（与 ask_llm / vision 共用）。
"""

import json
import mimetypes
import os
import time
import uuid

import requests

name = "qwen"
capabilities = {"chat", "search", "vision"}
default_model = "qwen3.7-plus"

API_BASE = "https://chat.qwen.ai"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_COOKIE_FILE = os.path.expanduser("~/.qwen_cookie")
_HTTP_TIMEOUT = 30
_STREAM_TIMEOUT = 300


# ── 凭据 / 请求头 ───────────────────────────────────────────────

def load_cookie() -> str:
    c = os.environ.get("QWEN_COOKIE", "")
    if c:
        return c
    try:
        with open(_COOKIE_FILE) as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError):
        return ""


def available():
    if not load_cookie():
        return False, "未找到 Qwen cookie：~/.qwen_cookie 或环境变量 QWEN_COOKIE"
    return True, ""


def _headers() -> dict:
    """完整浏览器头 —— 缺项会被 WAF 拦成 HTML 挑战页"""
    return {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": API_BASE,
        "Referer": f"{API_BASE}/",
        "Version": "0.2.63",
        "source": "web",
        "X-Request-Id": str(uuid.uuid4()),
        "Timezone": time.strftime("%a %b %d %Y %H:%M:%S GMT+0800"),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "sec-ch-ua": '"Chromium";v="140", "Not(A:Brand";v="24"',
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-site": "same-origin",
    }


def _session() -> requests.Session:
    s = requests.Session()
    for item in load_cookie().split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            s.cookies.set(k, v)
    return s


# ── 会话 ────────────────────────────────────────────────────────

def new_session(model: str = "") -> str:
    model = model or default_model
    r = _session().post(
        f"{API_BASE}/api/v2/chats/new", headers=_headers(),
        json={"model": model}, timeout=_HTTP_TIMEOUT,
    )
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"建会话失败: {j}")
    return j["data"]["id"]


# ── 文件上传（OSS）──────────────────────────────────────────────

def upload(path: str) -> dict:
    """上传文件到 Qwen OSS，返回可直接挂到 message.files 的结构"""
    try:
        import oss2
    except ImportError:
        raise RuntimeError("缺少 oss2（Qwen 文件上传依赖）: pip install oss2")

    with open(path, "rb") as f:
        data = f.read()
    filename = os.path.basename(path)
    filetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    top = filetype.split("/")[0] if "/" in filetype else "file"
    file_class = {"image": "vision", "video": "video", "audio": "audio"}.get(top, "file")
    file_show_type = {"image": "image", "video": "video", "audio": "audio"}.get(top, "file")

    sess = _session()
    r = sess.post(
        f"{API_BASE}/api/v1/files/getstsToken", headers=_headers(),
        json={"filename": filename, "filesize": str(len(data)), "filetype": filetype},
        timeout=_HTTP_TIMEOUT,
    )
    r.raise_for_status()
    sts = r.json()

    auth = oss2.StsAuth(sts["access_key_id"], sts["access_key_secret"], sts["security_token"])
    bucket = oss2.Bucket(auth, f"https://{sts['endpoint']}", sts["bucketname"])
    res = bucket.put_object(sts["file_path"], data)
    if res.status != 200:
        raise OSError(f"OSS 上传失败: HTTP {res.status}")

    return {
        "id": sts["file_id"],
        "name": filename,
        "file_type": filetype,
        "type": file_show_type,
        "file_class": file_class,
        "size": len(data),
        "url": sts["file_url"],
        "file": {
            "id": sts["file_id"],
            "filename": filename,
            "size": len(data),
            "type": filetype,
            "meta": {"name": filename, "size": len(data), "content_type": filetype},
        },
    }


# ── SSE 解析 ────────────────────────────────────────────────────

def _parse_sse(raw: str) -> dict:
    """一个流里同时含 answer phase 与 web_search phase。

    - phase=="answer"      → delta.content 顺序拼接
    - phase=="web_search"  → typing 阶段拿 queries，finished 阶段拿 docs（结构化引用）
    顶层 response_id 即下一轮的 parent_id。
    """
    text, think, resp_id = "", "", None
    queries, docs = [], []
    seen = set()

    for line in raw.split("\n"):
        if not line.startswith("data: "):
            continue
        ds = line[6:].strip()
        if ds == "[DONE]":
            continue
        try:
            d = json.loads(ds)
        except json.JSONDecodeError:
            continue

        if d.get("response_id"):
            resp_id = d["response_id"]

        for ch in d.get("choices", []):
            delta = ch.get("delta", {})
            phase = delta.get("phase", "")

            if phase == "web_search":
                status = delta.get("status", "")
                if status == "typing":
                    args = (delta.get("function_call") or {}).get("arguments") or ""
                    if args:
                        try:
                            qs = json.loads(args).get("queries", [])
                            if qs:
                                queries = qs
                        except (json.JSONDecodeError, TypeError):
                            pass
                elif status == "finished":
                    extra = delta.get("extra") or {}
                    tr = extra.get("tool_result") if isinstance(extra, dict) else None
                    for doc in (tr or {}).get("docs", []) if isinstance(tr, dict) else []:
                        u = doc.get("url", "")
                        if u and u not in seen:
                            seen.add(u)
                            docs.append({
                                "url": u,
                                "title": doc.get("title", ""),
                                "snippet": doc.get("snippet", ""),
                            })
                continue

            if phase == "answer":
                c = delta.get("content", "")
                if c:
                    text += c
            elif "thinking" in str(delta.get("phase", "")):
                c = delta.get("content", "")
                if c:
                    think += c

    return {
        "text": text,
        "thinking": think,
        "message_id": resp_id,
        "references": docs,
        "queries": queries,
    }


# ── 发消息 ──────────────────────────────────────────────────────

def ask(
    session_id: str,
    text: str,
    *,
    parent_message_id: str | None = None,
    model: str = "",
    think: bool = False,
    search: bool = False,
    files: list | None = None,
) -> dict:
    model = model or default_model
    msg = {
        "fid": str(uuid.uuid4()),
        "parentId": parent_message_id,
        "childrenIds": [],
        "role": "user",
        "content": text,
        "user_action": "chat",
        "files": files or [],
        "timestamp": int(time.time()),
        "models": [model],
        "chat_type": "t2t",
        "feature_config": {
            "thinking_enabled": bool(think),
            "output_schema": "phase",
            "research_mode": "normal",
            "auto_thinking": bool(think),
            "thinking_mode": "Auto" if think else "Fast",
            "thinking_format": "summary",
            "auto_search": bool(search),
        },
        "extra": {"meta": {"subChatType": "t2t"}},
        "sub_chat_type": "t2t",
    }
    body = {
        "stream": True,
        "version": "2.1",
        "incremental_output": True,
        "chat_id": session_id,
        "chat_mode": "normal",
        "model": model,
        "parent_id": parent_message_id,
        "messages": [msg],
        "timestamp": int(time.time()),
    }

    r = _session().post(
        f"{API_BASE}/api/v2/chat/completions?chat_id={session_id}",
        headers={
            **_headers(),
            "Accept": "application/json, text/event-stream",
            "X-Accel-Buffering": "no",
        },
        json=body, stream=True, timeout=_STREAM_TIMEOUT,
    )
    r.raise_for_status()

    raw = ""
    for line in r.iter_lines(decode_unicode=True):
        if line is None:
            continue
        raw += line + "\n"
        if line.strip() == "data: [DONE]":
            break

    if raw.lstrip().startswith("<!doctype") or "aliyun_waf" in raw[:2000]:
        raise RuntimeError("被 WAF 拦截（cookie 可能失效，见记忆 qwen_cookie_refresh）")
    head = raw.split("\n")[0] if raw else ""
    if '"ret"' in head and "FAIL" in head:
        try:
            err = json.loads(head[6:] if head.startswith("data: ") else head)
            ret = err.get("ret", ["未知错误"])
            raise RuntimeError(f"API 拒绝: {ret[1] if len(ret) > 1 else ret[0]}")
        except json.JSONDecodeError:
            pass

    return _parse_sse(raw)


# ── 便捷：一次性联网搜索 ────────────────────────────────────────

def search(query: str, *, think: bool = False, model: str = "") -> dict:
    """新会话 + 开搜索，返回 {text, references, queries}"""
    sid = new_session(model)
    r = ask(sid, query, model=model, think=think, search=True)
    return {
        "text": r["text"],
        "references": r["references"],
        "queries": r["queries"],
        "session_id": sid,
        "message_id": r["message_id"],
    }
