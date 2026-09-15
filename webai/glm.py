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
* 生成类能力（生图 / 生视频）与聊天**共用同一端点**，靠 `assistant_id` 区分能力；
  产物在 `content[].type=="image"` → `image[].image_url`（一次 4 张，到达顺序乱）。

可选钩子（PROVIDER_SPEC.md）：`CAPABILITY_MAP` 用 **assistant_id** 声明能力方言；
`models()` 走 `available_models` 配置、`probe()` 走 `operation-api` 助手清单；
`classify()` 认方言 `40014→auth` / 「权限不足」→`unsupported`（其余返回 ""，回落通用词表）。
GLM **全同步**，故不实现 `poll()`。SSE 解析把每个 content type（含未知）收进 `phases`，
原始 `meta_data` / 未知字段收进 `extra` —— 一律不丢。

凭据：<数据目录>/glm/cookies.json（由 glm_login.py 生成；含 cookie 与 device_id）
"""

import hashlib
import json
import mimetypes
import os
import pathlib
import re
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

# 生成类助手（PROVIDER_SPEC.md §1）：生成走**和聊天完全相同的端点**，
# 靠 assistant_id 区分能力（GLM 没有 chat_type 方言）。
IMAGE_ASSISTANT_ID = "65a232c082ff90a2ad2f15e2"   # AI 画图（生图）
VIDEO_ASSISTANT_ID = "668d03b2e99d661ed3c32516"   # AI 生视频（未联网实测，见 DESIGN §8）

# 能力名 → 本家方言。GLM 的是 assistant_id，不是 chat_type —— 这一层只"如实描述方言"。
# 生图附加参数另走请求体 `meta_data.cogview`（aspect_ratio/style/scene/rm_label_watermark），
# 由调用方经 `new_session(..., cogview={...})` 透传。
CAPABILITY_MAP: dict = {
    "chat":      {"assistant_id": ASSISTANT_ID},
    "image_gen": {"assistant_id": IMAGE_ASSISTANT_ID},
    "video_gen": {"assistant_id": VIDEO_ASSISTANT_ID},
}

# 能力 / 模型清单（PROVIDER_SPEC.md §3/§4），都是只读 GET。
OPERATION_API = f"{BASE}/chatglm/operation-api"
AGENT_API = f"{BASE}/chatglm/agent-api"
ASSISTANT_LIST_URL = f"{OPERATION_API}/assistant_top/v4/recent_list"
OPERATION_DATA_URL = f"{OPERATION_API}/config/operation_data"       # ?tag=…
MODELS_URL = f"{AGENT_API}/operation/detail"                        # ?tag=available_models

_HTTP_TIMEOUT = 30

# new_session() 的占位符：GLM 无建会话 API，真实 conversation_id 由首条消息返回。
# ask() 会把它换成 ""，并在返回里带出真实 session_id，调用方应据此更新。
NEW = "new"

# 会话 / 占位符 → 方言（assistant_id 等）。GLM 的能力是**请求级**的（assistant_id 在 body 里），
# 而它没有"建会话"这一步，所以 new_session() 把方言暂存在这里，由紧接着的 ask() 取回；
# ask() 拿到真实 conversation_id 后回填，使后续续接仍走同一助手（否则退回默认助手）。
_SESSION_DIALECT: dict = {}
_MAX_SESSION_DIALECT = 512
# 本家能消费的方言键；其余关键字一律忽略（PROVIDER_SPEC.md §7「绝不抛异常」）。
_DIALECT_KEYS = ("assistant_id", "meta_data", "cogview")

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

def _remember_dialect(sid: str, dialect: dict) -> None:
    if not sid or not dialect:
        return
    if len(_SESSION_DIALECT) >= _MAX_SESSION_DIALECT:
        for k in list(_SESSION_DIALECT)[: _MAX_SESSION_DIALECT // 2]:
            _SESSION_DIALECT.pop(k, None)
    _SESSION_DIALECT[str(sid)] = dict(dialect)


def _resolve_dialect(session_id) -> tuple:
    """→ (真实 conversation_id, 方言 dict)。占位符 → ("", 方言)。"""
    s = str(session_id or "")
    if not s or s == NEW:
        return "", {}
    if s.startswith(NEW + ":"):
        return "", dict(_SESSION_DIALECT.get(s) or {})
    return s, dict(_SESSION_DIALECT.get(s) or {})


def new_session(model: str = "", capability: str = "", **dialect) -> str:
    """GLM **没有**建会话接口 —— 一律返回占位符，真实 id 由首次 ask() 带出。

    为支持"能力方言"（PROVIDER_SPEC.md §7），这里把 `capability` / `**dialect` 暂存起来，
    供紧接着的 `ask()` 取回；`__init__.new_session()` 会按 `CAPABILITY_MAP` 把能力展开成
    关键字传进来（如 `capability="image_gen"` → `assistant_id="65a2…"`）。
    **不认识的关键字一律忽略，绝不抛异常。**
    """
    d = {k: v for k, v in dialect.items() if k in _DIALECT_KEYS and v not in (None, "")}
    if not d and capability:
        d = dict(CAPABILITY_MAP.get(capability) or {})
    if d:
        token = f"{NEW}:{uuid.uuid4().hex[:12]}"
        _remember_dialect(token, d)
        return token
    return NEW


# ── 发消息 ──────────────────────────────────────────────────────

def ask(session_id: str, text: str, *, parent_message_id=None, model: str = "",
        think: bool = False, search: bool = False, files=None) -> dict:
    """发消息收完整流。parent_message_id 忽略（服务端自持上下文）。

    返回 dict：
      老 key（**原样保留**）: text / thinking / message_id / references / queries
        —— `session_id` 是服务端分配的真实 conversation_id，调用方应回写。
      新 key（可选，PROVIDER_SPEC.md §2）: assets / phases / extra / model / status
        * assets: `content[].type=="image"` 的 4 张图（按 `order`/URL 序号稳定排序、去重）
        * phases: 本次出现过的**每一个** content type（含未知）→ 累计文本（无文本记 ""）
        * extra : 原始 meta_data + 未知 content type / 未知字段（一律不丢）
        * status: 仅当识别到"能力在追问确认"时填 "needs_input"
    """
    conv, dialect = _resolve_dialect(session_id)
    assistant_id = str(dialect.get("assistant_id") or ASSISTANT_ID)
    body = {
        "assistant_id": assistant_id,
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
    # 方言透传：生图参数走 meta_data.cogview（字段 aspect_ratio/style/scene/rm_label_watermark）。
    # ⚠️ 默认值未联网实测，故**不硬编** —— 只有调用方显式给了才带上（见 new_session）。
    extra_md = dialect.get("meta_data")
    if isinstance(extra_md, dict):
        body["meta_data"].update(extra_md)
    cog = dialect.get("cogview")
    if isinstance(cog, dict) and cog:
        body["meta_data"]["cogview"] = dict(cog)
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
    result = _parse(raw)
    # 回填方言：让后续续接（用真实 conversation_id）仍走同一个 assistant
    if dialect and result.get("session_id"):
        _remember_dialect(result["session_id"], dialect)
    return result


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


# 解析用常量 —— "未知不丢"（PROVIDER_SPEC.md §0.2）是本次的第一原则：
# 每个 content type（含未知）都进 phases，每个未知字段 / 未知 type 原样进 extra。
_KNOWN_CONTENT_KEYS = {"type", "text", "think", "image", "tool_calls"}
_KNOWN_PART_KEYS = {"id", "status", "content", "meta_data"}
_KNOWN_EVENT_KEYS = {"conversation_id", "id", "status", "parts", "model", "model_name"}
# 交互式追问信号（**保守**：拿不准不填，宁可漏报）。
_NEEDS_INPUT_STATUS = {"waiting_input", "waiting_user", "needs_input",
                       "user_input", "require_input", "ask_user"}
_NEEDS_INPUT_META = ("need_confirm", "need_input", "waiting_input",
                     "require_input", "interactive", "ask_user", "need_user_confirm")
_NEEDS_INPUT_TYPES = {"ask_user", "interactive", "need_input", "confirm_required"}
_MEDIA_URL_KEYS = ("video_url", "url", "file_url", "download_url", "src")
_IMG_ORDER_RE = re.compile(r"_(\d+)(?=\.[^./]*$|[?#]|$)")


def _img_order(img):
    """取图片序号：优先 `order` 字段，否则从 image_url 末尾的 `_<n>` 抠。

    实测 4 张图**到达顺序是乱的**（`_0 _3 _1 _2`），排序后产物顺序才稳定。
    """
    if not isinstance(img, dict):
        return None
    o = img.get("order")
    if isinstance(o, int):
        return o
    if isinstance(o, str) and o.strip().isdigit():
        return int(o.strip())
    u = str(img.get("image_url") or img.get("url") or img.get("file_url") or "")
    m = _IMG_ORDER_RE.findall(u)
    return int(m[-1]) if m else None


def _asset_from_image(img):
    """GLM 生图产物：`content[].type=="image"` → `image[].image_url`（字符串）。"""
    if not isinstance(img, dict):
        return None
    u = img.get("image_url") or img.get("url") or img.get("file_url")
    if not isinstance(u, str) or not u.strip():
        return None
    a = {"kind": "image", "url": u.strip()}
    if img.get("file_name"):
        a["name"] = img["file_name"]
    if img.get("file_size") is not None:
        a["size"] = img["file_size"]
    for k in ("width", "height"):
        if img.get(k) is not None:
            a[k] = img[k]
    if img.get("file_id"):
        a["meta"] = {"file_id": img["file_id"]}
    return a


def _walk_urls(obj):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _MEDIA_URL_KEYS and isinstance(v, str) and v.startswith("http"):
                out.append(v)
            elif isinstance(v, (dict, list)):
                out.extend(_walk_urls(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_walk_urls(v))
    return out


def _parse(raw: bytes) -> dict:
    """解析 GLM 流。

    ⚠️ 铁律（PROVIDER_SPEC.md §0.2/§2）：**不准用白名单筛流**。流里出现的每个
    content type 都记进 `phases`（未知照收），未知字段/未知 type 原样进 `extra`。

    ⚠️ 双态流（最关键，别破坏）：**中间帧是 delta，末帧是 `status=="finish"` 的全量快照**。
    正文/思考用 `_snap_append` + 末帧快照兜底 —— 只拼接会重复，只取末帧会丢思考过程。
    """
    acc_t, acc_k = [], []
    refs, queries = [], []
    seen_ref = set()
    msg_id = conv = None
    snap_t, snap_k = "", ""

    model = ""
    needs_input = False
    phases: dict = {}                 # content type → 累计文本（无文本记 ""）
    assets: list = []                 # 非图片产物（保序）
    image_items: list = []            # (排序键, asset) —— 图片，最后按序号稳定排序
    asset_keys: set = set()
    seq = 0
    extra: dict = {}

    def _merge_extra(e):
        if not isinstance(e, dict):
            return
        for k, v in e.items():
            if isinstance(v, dict) and isinstance(extra.get(k), dict):
                extra[k].update(v)
            else:
                extra[k] = v

    def _phase_acc(t, s):
        lst = phases.setdefault(t, [])
        if isinstance(lst, list):
            _snap_append(lst, s)

    def _add_asset(a, order=None):
        nonlocal seq
        if not a:
            return
        key = a.get("url") or (a.get("content") or "")[:64]
        if key:
            if key in asset_keys:
                return
            asset_keys.add(key)
        if order is None:
            assets.append(a)
        else:
            seq += 1
            image_items.append(((0, order, seq), a))

    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("conversation_id"):
            conv = ev["conversation_id"]
        if ev.get("id"):
            msg_id = ev["id"]
        if not model:
            m = ev.get("model") or ev.get("model_name")
            if isinstance(m, str) and m.strip():
                model = m.strip()
        # 未知顶层字段不丢
        for k, v in ev.items():
            if k not in _KNOWN_EVENT_KEYS:
                extra.setdefault("event_fields", {})[k] = v

        for p in ev.get("parts") or []:
            if not isinstance(p, dict):
                extra.setdefault("unparsed_parts", []).append(p)
                continue
            if p.get("id"):
                msg_id = msg_id or p["id"]
            is_fin = (p.get("status") == "finish") or (ev.get("status") == "finish")
            if str(p.get("status") or "").lower() in _NEEDS_INPUT_STATUS:
                needs_input = True
            for k, v in p.items():
                if k not in _KNOWN_PART_KEYS:
                    extra.setdefault("part_fields", {})[k] = v

            # 搜索引用（老逻辑，原样保留）
            md = p.get("meta_data") or {}
            _merge_extra(md)
            if not model:
                m = md.get("model") or md.get("model_name") or md.get("selected_model")
                if isinstance(m, str) and m.strip():
                    model = m.strip()
            for flag in _NEEDS_INPUT_META:
                if md.get(flag):
                    needs_input = True
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
                if not isinstance(c, dict):
                    extra.setdefault("unparsed_content", []).append({"type": "?", "raw": c})
                    continue
                t = str(c.get("type") or "")
                # 已知 content 里多出来的键 → 不丢
                unknown = {k: v for k, v in c.items() if k not in _KNOWN_CONTENT_KEYS}
                if unknown:
                    extra.setdefault("content_fields", {}).update(unknown)

                if t == "text":
                    s = c.get("text") or ""
                    if is_fin and len(s) > len(snap_t):
                        snap_t = s
                    _snap_append(acc_t, s)
                    _phase_acc(t, s)
                elif t == "think":
                    s = c.get("think") or ""
                    if is_fin and len(s) > len(snap_k):
                        snap_k = s
                    _snap_append(acc_k, s)
                    _phase_acc(t, s)
                elif t == "image":
                    phases.setdefault(t, [])
                    for img in c.get("image") or []:
                        _add_asset(_asset_from_image(img), _img_order(img))
                elif "video" in t.lower():
                    phases.setdefault(t, [])
                    for u in _walk_urls(c):
                        _add_asset({"kind": "video", "url": u})
                else:
                    # 未知 content type：**不丢** —— 记 phase + 原样留档
                    # （含 GLM 的末帧哨兵 `one_to_more_finish`，绝不当作正文）
                    phases.setdefault(t, [])
                    if t in _NEEDS_INPUT_TYPES:
                        needs_input = True
                    extra.setdefault("unparsed_content", []).append({"type": t, "raw": c})

                # 检索词：工具名不固定（实测见过 search / retrieve），
                # 参数键也随之变（search_query / retrieve），故按"值里是 {q:...} 列表"通配提取。
                tc = c.get("tool_calls") or {}
                if isinstance(tc, dict) and tc.get("arguments"):
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

    # 图片按序号稳定排序（乱序到达 → 稳定顺序），追加在其它产物之后
    image_items.sort(key=lambda kv: kv[0])
    for _, a in image_items:
        assets.append(a)

    acc_txt = "".join(acc_t)
    text = snap_t if len(snap_t) >= len(acc_txt) else acc_txt
    acc_kk = "".join(acc_k)
    thinking = snap_k if len(snap_k) >= len(acc_kk) else acc_kk

    out = {
        "text": text,
        "thinking": thinking,
        "message_id": msg_id,
        "references": refs,
        "queries": queries,
        "session_id": conv,
        "assets": assets,
        "phases": {k: ("".join(v) if isinstance(v, list) else v) for k, v in phases.items()},
        "extra": extra,
    }
    if model:
        out["model"] = model
    if needs_input:
        out["status"] = "needs_input"
    return out


# ── 可选钩子：模型枚举 / 能力探测 / 错误归类（PROVIDER_SPEC.md §3/§4/§6）──
# 原则：这些是"缺了自动降级"的可选实现 —— 一律**不往外抛异常**，拿不准就 []/None/""。

_MODEL_ID_KEYS = ("model", "model_id", "model_name", "id")
_MODEL_NAME_KEYS = ("name", "display_name", "label", "title", "cn_name")
_MODEL_MODE_KEYS = ("modes", "efforts", "levels", "reasoning", "reasoning_levels")
_MODEL_STRONG_ID_KEYS = ("model", "model_id", "model_name")
_MODEL_CAP_KEYS = ("capabilities", "vision", "search", "thinking",
                   "enable_thinking", "web_search", "reasoning")
_MODEL_CTX_KEYS = ("max_context_length", "context_length", "ctx", "max_input_tokens")
_ID24_RE = re.compile(r"^[0-9a-f]{24}$")


def _model_score(d) -> int:
    """单项的"像模型"程度。纯 `{id,name}`（如推理档 快速/深度/极致）得分很低，
    带 model / modes / capabilities / ctx 的才是模型 —— 用来把模型清单和它的子列表区分开。"""
    if not isinstance(d, dict):
        return 0
    s = 0
    if any(isinstance(d.get(k), str) and d.get(k).strip() for k in _MODEL_STRONG_ID_KEYS):
        s += 3
    if any(d.get(k) for k in _MODEL_MODE_KEYS):
        s += 2
    if any(k in d for k in _MODEL_CAP_KEYS):
        s += 2
    if any(isinstance(d.get(k), str) and d.get(k).strip() for k in _MODEL_NAME_KEYS):
        s += 1
    if any(d.get(k) is not None for k in _MODEL_CTX_KEYS):
        s += 1
    return s


def _find_model_list(obj) -> list:
    """在任意嵌套 JSON 里找"最像模型清单"的那个 list（返回 list[dict]）。

    available_models 的确切形状**未联网确认**，故不做形状假设：递归找 list，
    按**平均"像模型"分**选（不是"项数最多"—— 否则 `models[].modes[]` 这种子列表
    会因项数更多被误选）。同分取项数多者；找不到就 []（诚实：拿不到就说没有）。"""
    best: list = []
    best_key = (0.0, 0)

    def _scan(o):
        nonlocal best, best_key
        if isinstance(o, list):
            items = [it for it in o if isinstance(it, dict)]
            if items:
                scores = [_model_score(it) for it in items]
                if sum(1 for x in scores if x > 0) >= max(1, len(items) // 2):
                    key = (sum(scores) / len(scores), len(items))
                    if key > best_key:
                        best_key, best = key, items
            for v in o:
                _scan(v)
        elif isinstance(o, dict):
            for v in o.values():
                _scan(v)

    _scan(obj)
    return best


def _normalize_models(j) -> list:
    out, seen = [], set()
    for it in _find_model_list(j):
        mid = ""
        for k in _MODEL_ID_KEYS:
            if it.get(k):
                mid = str(it[k])
                break
        if not mid or mid in seen:
            continue
        seen.add(mid)
        caps = set()
        cap = it.get("capabilities")
        if isinstance(cap, dict):
            caps = {str(k) for k, v in cap.items() if v}
        elif isinstance(cap, (list, tuple, set)):
            caps = {str(x) for x in cap if x}
        elif isinstance(cap, str) and cap:
            caps = {cap}
        for flag in ("vision", "search", "thinking", "enable_thinking", "web_search", "reasoning"):
            if it.get(flag) is True:
                caps.add("thinking" if flag in ("enable_thinking", "reasoning") else flag)
        if any(it.get(k) for k in _MODEL_MODE_KEYS):
            caps.add("thinking")
        ctx = (it.get("max_context_length") or it.get("context_length")
               or it.get("ctx") or it.get("max_input_tokens") or 0)
        try:
            ctx = int(ctx)
        except (TypeError, ValueError):
            ctx = 0
        d = {
            "id": mid,
            "name": str(it.get("name") or it.get("display_name")
                        or it.get("label") or it.get("title") or mid),
            "capabilities": caps,
            "ctx": ctx,
        }
        if it.get("default") or it.get("is_default"):
            d["default"] = True
        # 推理档（快速/深度/极致）原样保留，不丢（PROVIDER_SPEC §0.2）
        for k in _MODEL_MODE_KEYS:
            if it.get(k):
                d[k] = it[k]
        out.append(d)
    return out


def models() -> list:
    """枚举可选模型（权威来源：`GET /agent-api/operation/detail?tag=available_models`）。

    返回 `[{"id","name","capabilities":set,"ctx":int,"default":bool}]`。
    任何异常 → `[]`（诚实：拿不到就说没有，别硬编可能过期的名字）。
    """
    try:
        r = _session().get(MODELS_URL, headers=_headers(),
                           params={"tag": "available_models"}, timeout=_HTTP_TIMEOUT)
        if r.status_code != 200:
            return []
        j = r.json()
    except Exception:  # noqa: BLE001
        return []
    return _normalize_models(j)


def _collect_assistant_ids(obj, out=None) -> set:
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_assistant_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_assistant_ids(v, out)
    elif isinstance(obj, str) and _ID24_RE.match(obj):
        out.add(obj)
    return out


def probe(capability: str = "") -> dict:
    """尽力探测某能力是否可用（权威来源：`GET /operation-api/assistant_top/v4/recent_list`）。

    **允许返回 `{"supported": None}` —— 不知道就说不知道。** 离线 / 凭据过期时一律 None。
    """
    out = {"supported": None, "via": "assistant_list", "note": ""}
    try:
        r = _session().get(ASSISTANT_LIST_URL, headers=_headers(), timeout=_HTTP_TIMEOUT)
        out["note"] = f"HTTP {r.status_code}"
        if r.status_code != 200:
            return out
        ids = _collect_assistant_ids(r.json())
    except Exception as e:  # noqa: BLE001
        out["note"] = f"{type(e).__name__}: {e}"[:120]
        return out

    if not ids:
        return out
    if not capability:
        out["note"] = f"助手清单 {len(ids)} 项"
        return out
    mapping = CAPABILITY_MAP.get(capability) or {}
    aid = str(mapping.get("assistant_id") or "")
    if aid:
        sup = aid in ids
        return {"supported": sup, "via": "assistant_list",
                "note": f"{capability} → assistant_id={aid}："
                        + ("在清单中" if sup else "不在清单中")}
    return {"supported": None, "via": "assistant_list",
            "note": f"未知能力名 {capability}：清单里没有直接证据，故不猜。"}


def classify(text: str = "", status: int = 0, raw=None, exc=None, **_) -> str:
    """GLM 方言优先，其余返回 `""`（假值）→ `__init__.classify()` 自动回落到通用词表。

    已知方言（实测，见 PROVIDER_SPEC.md §6）：
      * `40014`（x-sign 过期）→ `auth`
      * 「权限不足」→ `unsupported`

    返回 `""` 而不是 `"unknown"`：后者会**短路**掉 `core.classify_text()` 的通用中英文
    词表，反而更容易误判。非方言一律交给下层处理（本模块刻意不 import core，保持最小依赖）。
    """
    if isinstance(exc, AuthError):
        return "auth"
    blob = text or (str(exc) if exc is not None else "")
    if isinstance(raw, dict):
        for k in ("code", "error_code", "status", "ret", "error", "message", "msg"):
            v = raw.get(k)
            if isinstance(v, (str, int)):
                blob += f" {v}"
    low = blob.lower()
    if "40014" in low:
        return "auth"
    if "权限不足" in blob:
        return "unsupported"
    return ""


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
#
# 6. 生成类能力（生图 / 生视频）与聊天**共用同一端点**，靠 assistant_id 区分：
#    AI画图 `65a232c082ff90a2ad2f15e2` / AI生视频 `668d03b2e99d661ed3c32516`。
#    生图参数在 `meta_data.cogview`；产物在 `content[].type=="image"` → `image[].image_url`，
#    一次 4 张且**到达乱序**（`_0 _3 _1 _2`），故解析后按 order / URL 序号稳定排序并去重。
#    末帧哨兵 content type `one_to_more_finish` 只登记进 phases/extra，**绝不当正文**。
#
# 7. 可选钩子（PROVIDER_SPEC.md）：CAPABILITY_MAP 用 assistant_id（非 chat_type）；
#    models() 走 available_models 配置、probe() 走 recent_list 助手清单、classify() 只认
#    40014→auth / 「权限不足」→unsupported（其余回 "" 交通用词表）。GLM 全同步 → **不实现 poll()**。
