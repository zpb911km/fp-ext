"""
GLM provider (chatglm.cn) —— 智谱清言
======================================

协议要点（全部实测，踩坑记录见文件末尾）
--------------------------------------
* 端点：`POST /chatglm/backend-api/assistant/stream`，SSE（`data: {json}` 行）。
* 鉴权：`Authorization: Bearer <chatglm_token>`（cookie 里取的 token 直接当 Bearer 用），
  另外**必须**带 `x-app-platform` / `x-app-version` / `x-device-id` / `user-agent`，
  少一个就 40001/40002 —— 不是鉴权失败，是"缺头"，报错信息完全看不出。
* **签名**：`x-sign = md5(f"{x-timestamp}-{x-nonce}-{SALT}")`，SALT 硬编码在前端 JS 里。
  且 `x-timestamp` 不是裸 `Date.now()` —— 前端把倒数第二位替换成
  `(各位数字之和 - 倒数第二位) % 10`，服务端会校验这个位。
* 会话：**没有建会话 API**。传 `conversation_id: ""`，服务端在首帧返回真实 id。
  自造 conversation_id 会被静默丢弃（HTTP 200 但 0 字节流）—— 这是本次最坑的一处。
* 消息内容：中间帧是 **delta**，末帧是 `part.status="finish"` 的**全量快照**，
  两段拼接逻辑不同，混用会文字重复或截断。
* 联网搜索：`meta_data.tool_result_extra.search_results`（含 url/title/snippet/favicon）；
  检索词在 `content[].tool_calls.arguments`（JSON 字符串里的 `search_query[].q`）。

凭据：<数据目录>/glm/cookies.json（由 glm_login.py 生成；含 cookie 与 device_id）
"""

import hashlib
import json
import mimetypes
import os
import pathlib
import time
import uuid

import requests

name = "glm"
capabilities = {"chat", "search", "vision"}
default_model = "glm-5.3-flash"

BASE = "https://chatglm.cn"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

ASSISTANT_ID = "65940acff94777010aa6b796"      # 智谱清言默认助手
STREAM_URL = f"{BASE}/chatglm/backend-api/assistant/stream"
# ⚠️ 聊天传图走 productivity-api，**不是** backend-api/assistant/file/upload
#    （后者是知识库上传，带 assistant_id 会回"权限不足"）
UPLOAD_URL = f"{BASE}/chatglm/productivity-api/file/chat_upload"

# 前端硬编码的签名盐（见 main.*.js: sign: l()(`${o}-${a}-8a1317...`)）
SIGN_SALT = "8a1317a7468aa3ad86e997d08f3f31cb"

# new_session() 的占位符：GLM 无建会话 API，真实 conversation_id 由首条消息返回。
# ask() 会把它换成 ""，并在返回里带出真实 session_id，调用方应据此更新。
NEW = "new"

_STREAM_TIMEOUT = 300


def _data_dir() -> pathlib.Path:
    try:
        from fp_core.platform_utils import get_data_dir
        return pathlib.Path(get_data_dir())
    except Exception:  # noqa: BLE001
        return pathlib.Path.home() / ".local" / "share" / "fp"


CRED_FILE = _data_dir() / "glm" / "cookies.json"


# ── 凭据 ────────────────────────────────────────────────────────

def load_creds() -> dict:
    """{cookies: {...}, device_id: "..."}"""
    try:
        d = json.loads(CRED_FILE.read_text())
        if isinstance(d, dict) and "cookies" in d:
            return d
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        pass
    return {}


def _token() -> str:
    return (load_creds().get("cookies") or {}).get("chatglm_token", "")


def _device_id() -> str:
    return load_creds().get("device_id") or uuid.uuid4().hex


def available():
    if not _token():
        return False, ("未找到 GLM 凭据。请先登录：\n"
                       "    python3 <fp数据目录>/public/webai/glm_login.py")
    return True, ""


class AuthError(RuntimeError):
    pass


# ── 签名 ────────────────────────────────────────────────────────

def sign_headers() -> tuple:
    """生成 (x-timestamp, x-nonce, x-sign)。

    timestamp 的"校验位"变换照抄前端：
        ts = raw[:-2] + str((sum(各位数字) - 倒数第二位) % 10) + raw[-1]
    服务端会校验，直接传裸时间戳会被拒（40014）。
    """
    raw = str(int(time.time() * 1000))
    d = [int(c) for c in raw]
    ts = raw[:-2] + str((sum(d) - d[-2]) % 10) + raw[-1]
    nonce = uuid.uuid4().hex
    sign = hashlib.md5(f"{ts}-{nonce}-{SIGN_SALT}".encode()).hexdigest()
    return ts, nonce, sign


def _headers() -> dict:
    ts, nonce, sign = sign_headers()
    return {
        "authorization": f"Bearer {_token()}",
        "x-nonce": nonce,
        "x-timestamp": ts,
        "x-sign": sign,
        "x-request-id": uuid.uuid4().hex,
        "x-device-id": _device_id(),
        "x-app-platform": "pc",
        "x-app-version": "0.0.1",
        "x-lang": "zh",
        "accept": "text/event-stream",
        "content-type": "application/json",
        "user-agent": UA,
    }


def _session() -> requests.Session:
    s = requests.Session()
    s.cookies.update(load_creds().get("cookies") or {})
    return s


# ── 会话 ────────────────────────────────────────────────────────

def new_session(model: str = "") -> str:
    """GLM 没有建会话接口 —— 返回占位符，真实 id 由首次 ask() 带出。"""
    return NEW


# ── 发消息 ──────────────────────────────────────────────────────

def ask(session_id: str, text: str, *, parent_message_id=None, model: str = "",
        think: bool = False, search: bool = False, files=None) -> dict:
    """发消息收完整流。parent_message_id 忽略（服务端自持上下文）。

    返回 {text, thinking, message_id, references, queries, session_id}
    —— `session_id` 是服务端分配的真实 conversation_id，调用方应回写。
    """
    conv = "" if (not session_id or session_id == NEW) else session_id
    body = {
        "assistant_id": ASSISTANT_ID,
        "conversation_id": conv,
        "project_id": "",
        "chat_type": "user_chat",
        "meta_data": {
            "is_test": False,
            "input_question_type": "xxxx",
            "channel": "",
            "draft_id": "",
            "chat_mode": "deep_thinking" if think else "normal",
            "selected_model": model or default_model,
            "is_networking": bool(search),
            "quote_log_id": "",
            "platform": "pc",
        },
        "messages": [{"role": "user", "content": _content(text, files)}],
    }
    with _session().post(STREAM_URL, headers=_headers(), json=body,
                         stream=True, timeout=_STREAM_TIMEOUT) as r:
        if r.status_code != 200:
            raise RuntimeError(f"GLM HTTP {r.status_code}: {r.text[:300]}")
        raw = b""
        for chunk in r.iter_content(8192):
            if chunk:
                raw += chunk
    if not raw:
        raise AuthError("GLM 返回空流（token 可能过期，请重跑 glm_login.py）")
    return _parse(raw)


def _content(text: str, files=None) -> list:
    """图片内容的 schema（实测抓包得来，与直觉差别很大）：

        {"type": "image", "image": [{file_name, file_id, image_url, file_size, order, width, height}]}

    注意 `image` 是**数组**，且 `image_url` 是**字符串**（不是 {"url": ...} 对象）。
    猜过的 image_url 对象式、file 类型式全部被服务端静默忽略（模型回"看不到图片"）。
    """
    out = [{"type": "text", "text": text}]
    imgs = []
    for i, f in enumerate(files or []):
        d = f if isinstance(f, dict) else {"ref": f}
        imgs.append({
            "file_name": d.get("name") or "image.png",
            "file_id": d.get("ref", ""),
            "image_url": d.get("url", ""),
            "file_size": d.get("size", 0),
            "order": i,
            "width": 0,
            "height": 0,
        })
    if imgs:
        out.append({"type": "image", "image": imgs})
    return out


def _snap_append(acc: list, chunk: str):
    """delta 归并：若新块是"累积快照"（以当前累积串开头）则替换，否则追加。"""
    if not chunk:
        return
    cur = "".join(acc)
    if cur and chunk.startswith(cur):
        acc.clear()
        acc.append(chunk)
    else:
        acc.append(chunk)


def _parse(raw: bytes) -> dict:
    acc_t, acc_k = [], []
    refs, queries = [], []
    seen_ref = set()
    msg_id = conv = None
    snap_t, snap_k = "", ""

    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        if ev.get("conversation_id"):
            conv = ev["conversation_id"]
        if ev.get("id"):
            msg_id = ev["id"]

        for p in ev.get("parts") or []:
            if p.get("id"):
                msg_id = msg_id or p["id"]
            is_fin = (p.get("status") == "finish") or (ev.get("status") == "finish")

            # 搜索引用
            md = p.get("meta_data") or {}
            for it in (md.get("tool_result_extra") or {}).get("search_results") or []:
                if not isinstance(it, dict):
                    continue
                u = it.get("url") or ""
                if u and u in seen_ref:
                    continue
                if u:
                    seen_ref.add(u)
                refs.append({
                    "url": u,
                    "title": it.get("title") or it.get("host_name") or "",
                    "snippet": it.get("snippet") or "",
                })

            for c in p.get("content") or []:
                t = c.get("type")
                if t == "text":
                    s = c.get("text") or ""
                    if is_fin:
                        if len(s) > len(snap_t):
                            snap_t = s
                    _snap_append(acc_t, s)
                elif t == "think":
                    s = c.get("think") or ""
                    if is_fin:
                        if len(s) > len(snap_k):
                            snap_k = s
                    _snap_append(acc_k, s)
                # 检索词：工具名不固定（实测见过 search / retrieve），
                # 参数键也随之变（search_query / retrieve），故按"值里是 {q:...} 列表"通配提取。
                tc = c.get("tool_calls") or {}
                if tc.get("arguments"):
                    try:
                        args = json.loads(tc["arguments"])
                    except (ValueError, TypeError):
                        args = None
                    if isinstance(args, dict):
                        for v in args.values():
                            if not isinstance(v, list):
                                continue
                            for item in v:
                                qq = (item.get("q") if isinstance(item, dict)
                                      else (item if isinstance(item, str) else None))
                                if qq and qq not in queries:
                                    queries.append(qq)

    acc_txt = "".join(acc_t)
    text = snap_t if len(snap_t) >= len(acc_txt) else acc_txt
    acc_kk = "".join(acc_k)
    thinking = snap_k if len(snap_k) >= len(acc_kk) else acc_kk
    return {
        "text": text,
        "thinking": thinking,
        "message_id": msg_id,
        "references": refs,
        "queries": queries,
        "session_id": conv,
    }


# ── 便捷：一次性联网搜索 ────────────────────────────────────────

def search(query: str, *, think: bool = False, model: str = "") -> dict:
    sid = new_session(model)
    r = ask(sid, query, model=model, think=think, search=True)
    return {
        "text": r["text"],
        "references": r["references"],
        "queries": r["queries"],
        "session_id": r.get("session_id") or sid,
        "message_id": r["message_id"],
    }


# ── 上传（图片）────────────────────────────────────────────────

def upload(path: str) -> dict:
    """上传图片 → {ref, name, size, url}，ref/url 放进 ask(files=[...])。"""
    fn = os.path.basename(path)
    data = open(path, "rb").read()
    mime = mimetypes.guess_type(fn)[0] or "image/png"
    h = _headers()
    h.pop("content-type", None)      # 交给 requests 生成 multipart 边界
    r = _session().post(
        UPLOAD_URL, headers=h,
        files={"file": (fn, data, mime)},
        data={"from": "chat", "assistant_id": ASSISTANT_ID},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"上传失败 HTTP {r.status_code}: {r.text[:200]}")
    js = r.json() or {}
    if js.get("status") not in (0, None) or not (js.get("result") or []):
        raise RuntimeError(f"上传被拒: {str(js)[:240]}")
    res = js["result"]
    item = res[0] if isinstance(res, list) and res else (res if isinstance(res, dict) else {})
    return {
        "ref": item.get("file_id") or item.get("id") or "",
        "url": item.get("file_url") or item.get("url") or "",
        "name": fn,
        "size": len(data),
    }


# ═════════════════════════════════════════════════════════════════
# 踩坑记录（实测）
# ═════════════════════════════════════════════════════════════════
# 1. **自造 conversation_id 会被静默丢弃**：HTTP 200，但响应体 0 字节。
#    必须传 ""，让服务端在首帧返回真实 conversation_id。
#    （本次最坑的一处 —— 200 却空，比报错难查得多。）
#
# 2. 签名是**时变**的：复用旧 x-sign → 40014；完全不带 → 40011。
#    算法：md5(f"{ts}-{nonce}-{SALT}")，SALT 硬编码在前端。
#    且 ts 有校验位变换（见 sign_headers 文档）。
#
# 3. 缺头错误码全是 4 位数字，**没有任何"缺少哪个头"的提示**：
#    去掉 sign 三件套 → 40011；去掉 device/platform/version → 40001；
#    去掉 user-agent → 40002。当年靠 delta 调试逐组排除才定位。
#
# 4. **末帧是全量快照，中间是 delta**。若一律拼接，正文会重复一倍；
#    若只取末帧，思考过程会丢。故两者都要（_snap_append + snap 兜底）。
#
# 5. `/chatglm/backend-api/assistant/file/upload` 带 assistant_id 会回「权限不足」
#    —— 那是**知识库**上传（JS 里带 knowledge_id），不是聊天传图。
