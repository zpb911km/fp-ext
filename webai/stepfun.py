"""
StepFun provider (chat.stepfun.com) —— 阶跃星辰「阶跃AI」
=========================================================

协议要点（都是实测出来的，踩坑记录见文件末尾）
--------------------------------------------
* 表面是 gRPC 路径（`/api/agent/capy.agent.v1.AgentService/ChatStream`），
  实际是 **Connect 协议的 JSON 编码**：body 就是 JSON，不需要 protobuf。
* 鉴权 = **cookie**（`Oasis-Token` 等），但必须同时带一组 `oasis-*` 头，
  否则服务端回 401 `oasis-token is embezzled`（不是绑定，纯粹是缺头）。
* ChatStream 的请求/响应都是 **Connect 信封流**：
      [1 字节 flags][4 字节大端长度][JSON payload]
    flags: 0x00=数据, 0x02=流结束
* 服务端自持上下文：请求体**只带 chatSessionId**，不带 parentMessageId。

凭据：<数据目录>/stepfun/cookies.json（由 stepfun_login.py 生成）
"""

import json
import mimetypes
import os
import pathlib

import requests

name = "stepfun"
capabilities = {"chat", "search", "vision"}
default_model = "step-auto"

BASE = "https://chat.stepfun.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

SVC = "/api/agent/capy.agent.v1.AgentService"
_PASSPORT = "/passport/proto.api.passport.v1.PassportService"

_HTTP_TIMEOUT = 30
_STREAM_TIMEOUT = 300

# 缺了这几个头 → 401 "oasis-token is embezzled"
_OASIS = {
    "oasis-appid": "10200",
    "oasis-platform": "web",
    "oasis-language": "zh",
    "canary": "false",
    "connect-protocol-version": "1",
}


def _data_dir() -> pathlib.Path:
    try:
        from fp_core.platform_utils import get_data_dir
        return pathlib.Path(get_data_dir())
    except Exception:  # noqa: BLE001
        return pathlib.Path.home() / ".local" / "share" / "fp"


COOKIE_FILE = _data_dir() / "stepfun" / "cookies.json"


# ── 凭据 ────────────────────────────────────────────────────────

def load_cookies() -> dict:
    """读 cookie（登录脚本落盘的 JSON: {name: value}）"""
    import os
    env = os.environ.get("STEPFUN_COOKIE", "").strip()
    if env.startswith("{"):
        try:
            return json.loads(env)
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(COOKIE_FILE.read_text())
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return {}


def available():
    ck = load_cookies()
    if not ck.get("Oasis-Token"):
        return False, ("未找到 StepFun 凭据。请先登录：\n"
                       "    python3 <fp数据目录>/public/webai/stepfun_login.py")
    return True, ""


# ── HTTP ────────────────────────────────────────────────────────

def _headers(ctype: str = "application/json", referer: str | None = None) -> dict:
    h = {
        "user-agent": UA,
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": ctype,
        "origin": BASE,
        "referer": referer or f"{BASE}/chats/new",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    h.update(_OASIS)
    return h


def _session() -> requests.Session:
    s = requests.Session()
    s.cookies.update(load_cookies())
    return s


class AuthError(RuntimeError):
    pass


def _check(r: requests.Response):
    if r.status_code == 401:
        raise AuthError("StepFun 鉴权失败（cookie 可能过期，请重新运行 stepfun_login.py）")
    return r


# ── Connect 信封 ────────────────────────────────────────────────

def encode_envelope(obj: dict) -> bytes:
    p = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    return b"\x00" + len(p).to_bytes(4, "big") + p


def iter_envelopes(raw: bytes):
    """切分 Connect 信封流 → [(flags, payload_dict)]。损坏则停止（不猜）。"""
    i = 0
    while i + 5 <= len(raw):
        flags = raw[i]
        ln = int.from_bytes(raw[i + 1:i + 5], "big")
        if i + 5 + ln > len(raw):
            break
        try:
            yield flags, json.loads(raw[i + 5:i + 5 + ln])
        except (ValueError, UnicodeDecodeError):
            yield flags, None
        i += 5 + ln


# ── 会话 ────────────────────────────────────────────────────────

def new_session(model: str = "") -> str:
    r = _session().post(f"{BASE}{SVC}/CreateChatSession",
                        headers=_headers(), json={}, timeout=_HTTP_TIMEOUT)
    _check(r)
    if r.status_code != 200:
        raise RuntimeError(f"建会话失败 HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["chatSession"]["chatSessionId"]


# ── 发消息 ──────────────────────────────────────────────────────

def ask(session_id: str, text: str, *, parent_message_id=None, model: str = "",
        think: bool = False, search: bool = False, files=None) -> dict:
    """发消息并收完整流。parent_message_id 被忽略（服务端自持上下文）。

    返回 {text, thinking, message_id, references, queries}
    """
    body = {
        "message": {
            "chatSessionId": session_id,
            "content": {"userMessage": {"qa": {"content": text}}},
        },
        # ⚠️ config 是 message 的**兄弟**，不是子节点。放进 message 里会被静默忽略，
        #    表现为 enableSearch / enableReasoning 全部失效（不报错，最难查的那种）。
        "config": {
            "model": model or default_model,
            "enableReasoning": bool(think),
            "enableSearch": bool(search),
        },
    }
    if files:
        body["message"]["content"]["userMessage"]["qa"]["attachments"] = _attachments(files)

    sess = _session()
    with sess.post(f"{BASE}{SVC}/ChatStream",
                   headers=_headers("application/connect+json", f"{BASE}/chats/{session_id}"),
                   data=encode_envelope(body), stream=True, timeout=_STREAM_TIMEOUT) as r:
        _check(r)
        if r.status_code != 200:
            raise RuntimeError(f"ChatStream HTTP {r.status_code}: {r.text[:300]}")
        raw = b""
        for chunk in r.iter_content(8192):
            if chunk:
                raw += chunk
    return _parse(raw)


def _parse(raw: bytes) -> dict:
    text, think, refs, queries = [], [], [], []
    msg_id = None
    for flags, o in iter_envelopes(raw):
        if flags == 2 or not o:  # 0x02 = end-of-stream
            continue
        ev = (o.get("data") or {}).get("event") or {}
        if "startEvent" in ev:
            msg_id = ev["startEvent"].get("messageId") or msg_id
        elif "reasoningEvent" in ev:
            think.append(ev["reasoningEvent"].get("text") or "")
        elif "textEvent" in ev:
            text.append(ev["textEvent"].get("text") or "")
        elif "messageEvent" in ev:
            m = ev["messageEvent"].get("message") or {}
            msg_id = m.get("messageId") or msg_id
        elif "pipelineEvent" in ev:
            pe = ev["pipelineEvent"]
            es = pe.get("eventSearch") or {}
            for r0 in es.get("results") or es.get("searchResults") or []:
                if isinstance(r0, dict) and (r0.get("url") or r0.get("title")):
                    refs.append({"url": r0.get("url", ""), "title": r0.get("title", ""),
                                 "snippet": r0.get("snippet") or r0.get("content") or ""})
            # 注意：SEARCH 的 pipelineEvent.title 是「来源」，不是检索词 —— 别当 query 用
    return {
        "text": "".join(text),
        "thinking": "".join(think),
        "message_id": msg_id,
        "references": refs,
        "queries": queries,
    }


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


# ── 上传 ────────────────────────────────────────────────────────

def _attachments(files) -> list:
    """把 upload() 的产物转成 ChatStream 要的 resource 结构"""
    out = []
    for f in files:
        d = f if isinstance(f, dict) else {"ref": f}
        rid = d["ref"]
        out.append({"resource": {"image": {
            "rid": rid,
            "url": d.get("url", ""),
            "meta": d.get("meta") or {},
            "mimeType": d.get("mime") or "image/png",
        }, "rid": rid}})
    return out


def upload(path: str) -> dict:
    """上传图片 → {ref: rid, name, size, url, meta, mime}。ref 放进 ask(files=[...])。

    端点 /api/resource/image，multipart 三件套：file / scene_id=image / mime_type。
    """
    fn = os.path.basename(path)
    data = open(path, "rb").read()
    mime = mimetypes.guess_type(fn)[0] or "application/octet-stream"
    h = _headers()
    h.pop("content-type", None)          # 交给 requests 生成 multipart 边界
    r = _session().post(f"{BASE}/api/resource/image", headers=h,
                        files={"file": (fn, data, mime)},
                        data={"scene_id": "image", "mime_type": mime}, timeout=120)
    _check(r)
    if r.status_code != 200:
        raise RuntimeError(f"上传失败 HTTP {r.status_code}: {r.text[:200]}")
    js = r.json()
    if not js.get("rid"):
        raise RuntimeError(f"上传返回异常: {str(js)[:200]}")
    return {"ref": js["rid"], "name": fn, "size": len(data),
            "url": js.get("url", ""), "meta": js.get("meta") or {}, "mime": mime}


# ═════════════════════════════════════════════════════════════════
# 踩坑记录（实测）
# ═════════════════════════════════════════════════════════════════
# 1. 401 "oasis-token is embezzled" —— 不是 token 绑定，是缺 oasis-appid /
#    oasis-platform / oasis-language / canary 这几个头。补上即可纯 requests 调用。
#
# 2. Connect 信封里的 **长度前缀是任意二进制字节**。若用 Playwright 的
#    resp.text() / resp.body() 取 body，Chromium 会先按 UTF-8 解码，
#    非法字节被替换成 U+FFFD(EF BF BD) → 后续所有信封错位。
#    ⇒ 必须自己从 socket 拿原始字节（requests 的 iter_content 天然满足）。
#
# 3. 聊天不走 WebSocket。页面上确实有一条 wss://.../botapi/wss/connection
#    连接并带 protobuf 帧，但那只是心跳/部署状态通道 —— 别被带偏。
#
# 4. 请求体只带 chatSessionId，服务端自持上下文，没有 parentMessageId。
#
# 5. **`config` 必须是 `message` 的兄弟节点**。塞进 message 里不报错，
#    但 enableSearch / enableReasoning 会被静默忽略 —— 表现为"模型说它不能联网"。
#    这个 bug 靠 UI/纯 Python 请求体对比才揪出来。
#
# 6. 搜索引用在 `pipelineEvent.eventSearch.results`（action=EVENT_ACTION_END），
#    字段 url/title/snippet/site/faviconUrl。pipeline 的 title 是「来源」不是检索词。
