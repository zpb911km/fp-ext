#!/usr/bin/env python3
"""
MiMo provider (aistudio.xiaomimimo.com) —— 小米 MiMo AI Studio
=========================================================

协议要点（静态逆向 main.js 得出，详见 <数据目录>/webai/mimo/PROTOCOL.md）：
--------------------------------------------
* 对话走 **HTTP SSE**（fetch 流式），不是 WebSocket。页面上的 wss 是 claw 网关/liveKit，
  与主对话无关。
* ultra speed 模型（``mimo-v2.6-pro-ultraspeed-studio``）走 **fastchat 通道**：
  前端判定 ``model === ultraspeedModelCode`` 后所有路径加 ``/fastchat`` 前缀，
  即 ``POST /fastchat/open-apis/bot/chat``。本 provider 默认即 ultra speed，恒走该路径。
* 鉴权 = **cookie**（小米 SSO serviceToken 系），credentials: same-origin。
  401/302 = 登录过期；451/461 = 封禁。
* 请求体特点：``conversationId`` / ``msgId`` 均为**客户端生成**（crypto.randomUUID），
  服务端不强制先建会话；多轮靠 ``previousDialogueId``（上一轮的 dialogId，
  由 SSE ``dialogId`` 事件下发）+ 服务端按 conversationId 自持上下文。
* SSE 事件白名单（未知事件前端直接丢弃，我们也丢但留档 extra）：
  message(增量content) / dialogId / finish / usage / web_search / error /
  sensitive_query / sensitive_title / doc / tip_ratio / tip_truncate。
  data 一律是 JSON，``{"content": "<文本>"}`` 形状。

凭据：<数据目录>/webai/mimo/cookies.json（由 mimo_login.py 落盘）
"""

import hashlib
import json
import mimetypes
import pathlib
import re
import time
import uuid

import requests

try:                                    # provider → core 是允许的方向（core.py 顶部声明）
    from . import core as _core
except Exception:                       # noqa: BLE001  被当独立模块加载时降级
    _core = None

name = "mimo"
# search 有实据（webSearchStatus + SSE web_search 事件）；
# vision 需要走 genUploadInfo 上传，未实测 → 不声明（诚实优先，hint 非承诺）。
capabilities = {"chat", "search", "attach", "vision"}   # attach/vision: v1.4.0 实测通过（image 直读；file 自动切非 fastchat 通道）
default_model = "mimo-v2.6-pro-ultraspeed-studio"   # ultra speed（2026-09-25 用户指定）

# 能力 → 方言：mimo 的能力开关都在 modelConfig 里由 ask() 直接处理，无独立方言可声明。
CAPABILITY_MAP: dict = {}

BASE = "https://aistudio.xiaomimimo.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# ultra speed 恒走 fastchat 通道（本 provider 即为 ultra speed 而生）
_SVC = "/fastchat/open-apis/bot/chat"
# 文档附件专用通道：实测 fastchat(ultra) 收下 file 载荷但模型读不到
# （"附件未收到"）；非 fastchat + 非 ultra 模型全部读出（E1 实测 PASS），
# 与前端抓包一致（/#/c 附文档时 completions 走非 fastchat、model=mimo-v2.6-pro）。
_SVC_DOC = "/open-apis/bot/chat"
_DOC_MODEL = "mimo-v2.6-pro"
_USER = "/open-apis/user/mi/get"

_HTTP_TIMEOUT = 30
_STREAM_TIMEOUT = 300

# SSE 白名单（与前端一致）；不在表里的事件只留档不消费
_SSE_EVENTS = ("finish", "usage", "message", "dialogId", "web_search", "error",
               "sensitive_query", "sensitive_title", "doc", "tip_ratio", "tip_truncate")


def _data_dir() -> pathlib.Path:
    try:
        from fp_core.platform_utils import get_data_dir
        return pathlib.Path(get_data_dir())
    except Exception:                       # noqa: BLE001
        return pathlib.Path.home() / ".local" / "share" / "fp"


COOKIE_FILE = _data_dir() / "webai" / "mimo" / "cookies.json"


class AuthError(RuntimeError):
    pass


# ── 凭据 ────────────────────────────────────────────────────────

def load_cookies() -> dict:
    """读 cookie（登录脚本落盘的 JSON: {name: value}）"""
    try:
        return json.loads(COOKIE_FILE.read_text())
    except Exception:                       # noqa: BLE001
        return {}


def save_cookies(ck: dict) -> bool:
    """原子落盘 —— provider 每次请求前都读这个文件，直接写会留“半个 JSON”窗口。"""
    try:
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = COOKIE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(ck, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(COOKIE_FILE)
        return True
    except Exception:                       # noqa: BLE001
        return False


def available():
    ck = load_cookies()
    if not ck:
        return False, ("未找到 MiMo 凭据。请先登录：\n"
                       "    python3 <fp数据目录>/public/plugins/webai/lib/mimo_login.py")
    return True, ""


# ── HTTP ────────────────────────────────────────────────────────
#
# 【实测 2026-09-25 抓包】真实请求与静态逆向的差异，已按抓包对齐：
#   1. query 必带 `xiaomichatbot_ph=<cookie值去引号>` —— 服务端校验它与
#      serviceToken 的绑定，缺失/不匹配 → 401（首轮 401 就是这么来的）。
#   2. body **不带** null 占位键：previousDialogueId/sceneType/params 不存在时省略。
#   3. msgId/conversationId = 32 位 hex（uuid4().hex），不是带横线的 UUID。
#   4. 头：`x-timezone`（不是 x-timeZone）、`accept-language: system`。

def _ph() -> str:
    """query 参数 xiaomichatbot_ph = cookie 同名值去引号（前端 Ri(wl) 同款）。"""
    v = load_cookies().get("xiaomichatbot_ph") or ""
    return v.strip('"')


def _headers(ctype: str = "application/json") -> dict:
    return {
        "user-agent": UA,
        "accept": "*/*",
        "accept-language": "system",
        "content-type": ctype,
        "origin": BASE,
        "referer": f"{BASE}/",
        "x-timezone": "Asia/Shanghai",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def _session() -> requests.Session:
    s = requests.Session()
    s.cookies.update(load_cookies())
    return s


def _check(r: requests.Response):
    if r.status_code in (401, 302):
        raise AuthError(f"MiMo 鉴权失败（HTTP {r.status_code}，cookie 可能过期，"
                        f"请重跑 mimo_login.py）")
    if r.status_code in (451, 461):
        raise AuthError(f"MiMo 账号被封（HTTP {r.status_code}）")
    return r


# ── 会话 ────────────────────────────────────────────────────────
#
# conversationId / msgId 客户端生成（与前端一致，见 PROTOCOL.md）。
# 模块级 _LAST_DIALOG 记住每个会话上一轮的 dialogId → 下轮 previousDialogueId。
# 进程内记忆即可：session_id 由调用方持有，跨进程时 previousDialogueId 缺省为
# null，服务端仍按 conversationId 自持上下文（前端同款行为）。

_LAST_DIALOG: dict = {}


def new_session(model: str = "", capability: str = "", **dialect) -> str:
    """建会话 = 生成 conversationId（客户端生成，前端同款，32 位 hex）。

    capability / dialect 一律忽略（CAPABILITY_MAP 为空，绝不抛异常）。
    """
    return uuid.uuid4().hex


# ── 发消息 ──────────────────────────────────────────────────────

def _media_type(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or ""
    for pfx, kind in (("image/", "image"), ("video/", "video"), ("audio/", "audio")):
        if mime.startswith(pfx):
            return kind
    return "file"


def _unwrap(j, want: str) -> dict:
    """API 响应解包：{code, data:{...}} 或裸 {...}；code 非 0/200 报错。"""
    if not isinstance(j, dict):
        raise RuntimeError(f"mimo upload: 非 JSON 响应: {str(j)[:200]}")
    if isinstance(j.get(want), dict):
        return j[want]
    if want in j:
        return j
    code = j.get("code")
    if code not in (0, 200, None):
        raise RuntimeError(f"mimo upload: code={code} {json.dumps(j, ensure_ascii=False)[:300]}")
    data = j.get("data")
    return data if isinstance(data, dict) else j


def upload(path: str) -> dict:
    """上传文件到 MiMo 资源服务，返回可直接入出站 multiMedias 的 media dict。

    三步链路（main.js 静态逆向 R=upload / L=upload2Parse，逐字对齐）：
      1. POST /open-apis/resource/genUploadInfo {fileName, fileContentMd5}（md5 必带）
         → {uploadUrl, resourceUrl, objectName}
      2. PUT  uploadUrl —— 签名 URL 跨域直传，octet-stream + content-md5，**不带 cookie**
      3. POST /open-apis/resource/parse?fileUrl&objectName&model —— 仅文档，**必须成功**：
         data.tokenUsage 是出站 multiMedias 的必需字段（实测缺失 → "服务器繁忙"）
    出站时 ask() 只剥 resourceSource 与本模块私有键（_ 前缀）。
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise RuntimeError(f"文件不存在: {path}")
    data = p.read_bytes()
    if not data:
        raise RuntimeError(f"空文件: {path}")
    md5 = hashlib.md5(data).hexdigest()
    mt = _media_type(str(p))
    ph = _ph()
    phs = f"?xiaomichatbot_ph={requests.utils.quote(ph, safe='')}" if ph else ""

    # 1. genUploadInfo
    url = f"{BASE}/open-apis/resource/genUploadInfo{phs}"
    r = _session().post(url, headers=_headers(),
                        data=json.dumps({"fileName": p.name, "fileContentMd5": md5}),
                        timeout=_HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"genUploadInfo HTTP {r.status_code}: {r.text[:300]}")
    info = _unwrap(r.json(), "uploadUrl")
    upload_url, resource_url = info.get("uploadUrl"), info.get("resourceUrl")
    object_name = info.get("objectName")
    if not (upload_url and resource_url and object_name):
        raise RuntimeError(f"genUploadInfo 字段缺失: {json.dumps(info, ensure_ascii=False)[:300]}")

    # 2. 签名 URL 直传（跨域，不复用 Session cookie）
    r2 = requests.put(upload_url, data=data,
                      headers={"Content-Type": "application/octet-stream",
                               "content-md5": md5},
                      timeout=120)
    if r2.status_code not in (200, 204):
        raise RuntimeError(f"PUT uploadUrl HTTP {r2.status_code}: {r2.text[:300]}")

    # 3. 文档解析 —— **必须成功**：data.tokenUsage 与 data.id 都是出站必需字段
    #    （wire 抓包：tokenUsage 缺失 → "服务器繁忙"；url 字段 = parse 的 data.id）
    #    6002=空文件，上游已拦空文件。
    token_usage = None
    parse_id = None
    if mt == "file":
        purl = requests.utils.quote(resource_url, safe="")
        onm = requests.utils.quote(object_name, safe="")
        mm = requests.utils.quote(default_model, safe="")
        sep = "&" if phs else "?"
        rp = _session().post(
            f"{BASE}/open-apis/resource/parse{phs}{sep}"
            f"fileUrl={purl}&objectName={onm}&model={mm}",
            headers=_headers(), data=json.dumps({}), timeout=_HTTP_TIMEOUT)
        if rp.status_code != 200:
            raise RuntimeError(f"resource/parse HTTP {rp.status_code}: {rp.text[:300]}")
        j = rp.json()
        data_p = j.get("data") if isinstance(j, dict) else None
        tok = (data_p or {}).get("tokenUsage")
        if tok is None:
            raise RuntimeError(
                f"resource/parse 失败（无法取得 tokenUsage）: "
                f"{json.dumps(j, ensure_ascii=False)[:300]}")
        token_usage = int(tok)
        parse_id = (data_p or {}).get("id")

    media = {"name": p.name, "size": len(data), "mediaType": mt,
             "fileUrl": resource_url, "objectName": object_name,
             "status": "completed"}
    if mt == "file":
        # 前端 wire：文档还带空的压缩/音轨 URL 与 parse id
        media["compressedVideoUrl"] = ""
        media["audioTrackUrl"] = ""
        if parse_id:
            media["url"] = parse_id
    if token_usage is not None:
        media["tokenUsage"] = token_usage
    media["_path"] = str(p)                 # 私有键：ask() 外发前剥离
    return media


def ask(session_id: str, text: str, *, parent_message_id=None, model: str = "",
        think: bool = False, search: bool = False, files=None) -> dict:
    """发消息并收完整 SSE 流。

    返回 dict：
      老 key: text / thinking / message_id / references / queries
      新 key: model / phases / extra
        * phases: 每个 SSE 事件类型 → 累计文本（无文本记 ""）
        * extra : 事件全量留档 + usage + finish 原始数据（未知的东西不能丢）
    """
    medias: list = []
    if files:
        for f in files:
            if not (isinstance(f, dict) and f.get("fileUrl") and f.get("objectName")):
                raise RuntimeError(
                    "mimo ask: files 须为 upload() 返回的 media dict"
                    f"（收到 {type(f).__name__}）")
            # 前端出站只剥 resourceSource；我们的私有键（_path 等）不外发
            medias.append({k: v for k, v in f.items()
                           if not k.startswith("_") and k != "resourceSource"})

    # 通道决策：文档附件 → 非 fastchat + 非 ultra（见 _SVC_DOC 注释）
    has_file = any(x.get("mediaType") == "file" for x in medias)
    resolved = model or default_model
    if has_file and resolved == default_model:
        resolved = _DOC_MODEL                  # ultra 不读文档，降到文档可用模型
    svc = _SVC_DOC if has_file else _SVC

    mid = uuid.uuid4().hex
    prev = parent_message_id or _LAST_DIALOG.get(session_id)
    body = {
        "msgId": mid,
        "conversationId": session_id,
        "query": text,
        "isEditedQuery": False,
        # 首轮省略 previousDialogueId/sceneType/params —— 抓包实测：不存在即省略，
        # 不发 null 占位键（发了不报错，但保持与前端字节级一致最稳）。
        "modelConfig": {
            "enableThinking": bool(think),
            # 前端规则：webSearchEnabled && 0===u.length —— 有文件时搜索自动关
            "webSearchStatus": "enabled" if (search and not medias) else "disabled",
            "model": resolved,
        },
        "multiMedias": medias,
    }
    if prev:
        body["previousDialogueId"] = prev

    url = f"{BASE}{svc}"
    ph = _ph()
    if ph:
        url += f"?xiaomichatbot_ph={requests.utils.quote(ph, safe='')}"

    with _session().post(url, headers=_headers(), data=json.dumps(body),
                         stream=True, timeout=_STREAM_TIMEOUT) as r:
        _check(r)
        if r.status_code != 200:
            raise RuntimeError(f"MiMo chat HTTP {r.status_code}: {r.text[:300]}")
        raw = r.content
    return _parse(raw, session_id)


def _parse(raw: bytes, session_id: str = "") -> dict:
    """SSE → dict。格式：``event: X\\ndata: {...json...}\\n\\n``（标准 SSE）。"""
    text_parts: list = []
    phases: dict = {}
    refs: list = []
    queries: list = []
    dialog_id = ""
    usage = None
    finish = None
    error_msg = ""
    sensitive = False

    def _dispatch(event: str, data: str):
        nonlocal dialog_id, usage, finish, error_msg, sensitive
        obj = None
        try:
            obj = json.loads(data) if data else None
        except Exception:                   # noqa: BLE001  非 JSON data 原样留档
            obj = data
        content = ""
        if isinstance(obj, dict):
            content = obj.get("content") or ""
        elif isinstance(obj, str):
            content = obj
        phases.setdefault(event, "")
        if content:
            phases[event] += content
        if event == "message" and content:
            text_parts.append(content)
        elif event == "dialogId" and content:
            dialog_id = content
        elif event == "usage":
            usage = obj
        elif event == "finish":
            finish = obj
        elif event == "error":
            error_msg = content or data
        elif event == "sensitive_query" or event == "sensitive_title":
            sensitive = True
        elif event == "web_search" and isinstance(obj, dict):
            # 结果字段名未实测（推测 hits/results）——整包留档，尽量抽 url/title。
            refs.append(obj)
            for key in ("results", "hits", "items", "list"):
                arr = obj.get(key)
                if isinstance(arr, list):
                    for it in arr:
                        if isinstance(it, dict) and (it.get("url") or it.get("title")):
                            queries.append({"url": it.get("url", ""),
                                            "title": it.get("title", ""),
                                            "snippet": it.get("snippet", "")})
                    break

    # 逐行解析 SSE（服务端偶发 \r\n，统一剥掉）
    event_name = ""
    data_lines: list = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.rstrip("\r")
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "":
            if event_name and data_lines:
                _dispatch(event_name, "\n".join(data_lines))
            event_name, data_lines = "", []
    if event_name and data_lines:           # 尾部无空行的兜底
        _dispatch(event_name, "\n".join(data_lines))

    if session_id and dialog_id:
        _LAST_DIALOG[session_id] = dialog_id

    if sensitive:
        raise RuntimeError(f"MiMo 敏感内容拦截：{phases.get('sensitive_query') or phases.get('sensitive_title')}")
    if error_msg and not text_parts:
        raise RuntimeError(f"MiMo SSE error: {error_msg[:300]}")

    # 【实测 2026-09-25，hex 取证】message 事件用**字面标签**包裹思考段，
    # 标签后紧跟一个 NUL（U+0000）再接增量文本：
    #     content = TAG_OPEN + NUL + <思考增量>
    #     content = TAG_CLOSE + NUL + <正文增量>
    # 标签常量用字符串拼接构造 —— 源码里写连续字面量会被输出通道过滤替换。
    # 前端同款处理：replaceAll(TAG_OPEN, "&lt;think&gt;") 后 details 折叠。
    T_OPEN = "<" + "think" + ">"
    T_CLOSE = "</" + "think" + ">"
    full = "".join(text_parts).replace("\x00", "")

    thinking_parts = []

    def _split(m):
        thinking_parts.append(m.group(1))
        return ""
    text_only = re.sub(re.escape(T_OPEN) + "(.*?)" + re.escape(T_CLOSE),
                       _split, full, flags=re.S)
    if T_OPEN in text_only:                 # 流截断只剩开标签 → 残段归 thinking
        head, _, rest = text_only.partition(T_OPEN)
        thinking_parts.append(head + rest)
        text_only = ""
    text_only = text_only.replace(T_OPEN, "").replace(T_CLOSE, "")

    return {
        "text": text_only,
        "thinking": "".join(thinking_parts),
        "message_id": dialog_id or mid_fallback(),
        "references": refs,
        "queries": queries,
        "model": default_model,
        "phases": phases,
        "extra": {"usage": usage, "finish": finish, "raw_events": sum(1 for _ in phases)},
    }


def mid_fallback() -> str:
    return str(uuid.uuid4())


# ── 可选钩子 ────────────────────────────────────────────────────

def search(query: str, *, think: bool = False, model: str = "",
           files: list | None = None) -> dict:
    """联网搜索问答 = ask(search=True)（webSearchStatus=enabled + SSE web_search 事件）。

    与 stepfun/glm 同款契约：原样带上 ask() 的**所有**键（含将来新增的），
    只补 session_id —— 手写固定键集会静默丢掉新增信息（"未知的东西不能丢"）。
    """
    sid = new_session(model)
    r = ask(sid, query, model=model, think=think, search=True, files=files)
    out = dict(r)
    out.setdefault("session_id", sid)
    return out


def models() -> list:
    """GET /open-apis/bot/config → 模型清单；失败诚实返回空。

    返回规范形状 `[{"id","name","capabilities","ctx","default"}]`（PROVIDER_SPEC §0.2）：
    ``model`` 是**请求要用的 id**，``name`` 只是展示名（如 `MiMo-V2.6-Pro-UltraSpeed`），
    二者不同 —— 早先漏了 ``id``，会把展示名当 id 列给调用方。
    """
    try:
        r = _session().get(f"{BASE}/open-apis/bot/config", headers=_headers(),
                           timeout=_HTTP_TIMEOUT)
        _check(r)
        j = r.json()
        out = []

        def walk(o):
            if isinstance(o, dict):
                m = o.get("model")
                if isinstance(m, str) and m.startswith("mimo-"):
                    out.append({"model": m, "name": o.get("name") or m})
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(j.get("data", j))
        # 去重保序
        seen, uniq = set(), []
        for it in out:
            if it["model"] not in seen:
                seen.add(it["model"])
                it["id"] = it["model"]          # 规范 id（请求体用的就是它）
                it["capabilities"] = set()
                it["ctx"] = 0
                if it["model"] == default_model:
                    it["default"] = True         # ultra speed 是当前默认
                uniq.append(it)
        return uniq
    except Exception:                       # noqa: BLE001
        return []


def probe(capability: str = "") -> dict:
    if capability == "chat":
        return {"supported": True, "via": "sse", "note": "主对话链路（已逆向+实测）"}
    if capability == "search":
        return {"supported": True, "via": "modelConfig.webSearchStatus",
                "note": "webSearchStatus=enabled + SSE web_search 事件"}
    return {"supported": None, "via": "none",
            "note": "mimo 其余能力（vision/上传）未实测，不猜。"}


# MiMo 方言（实测逐步补充）
_AUTH_DIALECT = (
    "登录已失效", "未登录", "鉴权失败", "cookie 失效", "cookie失效",
    "请先登录", "login required", "unauthorized",
)
# 审核拦截 = 模型/服务端回避，不是技术故障 → content_policy
_POLICY_DIALECT = ("sensitive_query", "sensitive_title", "内容违规", "敏感内容")


def classify(text: str = "", status: int = 0, raw=None, exc=None, **_) -> str:
    if isinstance(exc, AuthError):
        return "auth"
    if status in (401, 302, 451, 461):
        return "auth"
    blob = text or ""
    low = blob.lower()
    if any(h.lower() in low for h in _AUTH_DIALECT):
        return "auth"
    if any(h in blob for h in _POLICY_DIALECT):
        return "content_policy"
    if _core is not None:
        return _core.classify_text(blob, status=status).value
    return ""      # 拿不到 core → 交上层 __init__.classify 兜底


def verify() -> tuple:
    """轻量探测：GET /open-apis/user/mi/get（只读）→ ("ok","") / ("dead",why) / ("unknown",why)。

    非 AUTH 类失败一律 unknown → 不触发自动登录（宁可漏刷，不可误刷）。
    """
    try:
        r = _session().get(f"{BASE}{_USER}", headers=_headers(), timeout=_HTTP_TIMEOUT)
        if r.status_code in (401, 302, 451, 461):
            return "dead", f"HTTP {r.status_code}"
        if r.status_code != 200:
            return "unknown", f"HTTP {r.status_code}"
        j = r.json()
        code = j.get("code")
        if code not in (0, 200, None):
            txt = json.dumps(j, ensure_ascii=False)[:200]
            if classify(text=txt) == "auth":
                return "dead", txt
            return "unknown", txt
        return "ok", ""
    except AuthError as e:
        return "dead", str(e)
    except Exception as e:                   # noqa: BLE001
        return "unknown", f"{type(e).__name__}: {e}"
