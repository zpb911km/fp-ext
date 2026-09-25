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
capabilities = {"chat", "search", "vision", "attach"}
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

# 生成类助手（PROVIDER_SPEC.md §1）：生图走**和聊天完全相同的端点**，
# 靠 assistant_id 区分能力（GLM 没有 chat_type 方言）。
IMAGE_ASSISTANT_ID = "65a232c082ff90a2ad2f15e2"   # AI 画图（生图）
VIDEO_ASSISTANT_ID = "668d03b2e99d661ed3c32516"   # AI 生视频（视频助手，fixed_assistant 清单在册）

# ⚠️ 修正旧结论：**GLM 的视频确实走独立端点**（DESIGN §2 原写"video-api 是误认"，错）。
# 实测（2026-09，联网 Playwright 抓包）：前端点「AI生视频」后**根本不发 assistant/stream**，
# 而是 POST `video-api/v1/chat` 拿 chat_id，再轮询 `video-api/v1/chat/status/<chat_id>`。
# assistant_id 在 video-api 里不用 —— 保留仅为与 fixed_assistant 清单对得上号。
VIDEO_API = f"{BASE}/chatglm/video-api/v1"

# 能力名 → 本家方言。`video: True` 是"这条会话走 video-api"的开关（ask() 读它分流）。
CAPABILITY_MAP: dict = {
    "chat":      {"assistant_id": ASSISTANT_ID},
    "image_gen": {"assistant_id": IMAGE_ASSISTANT_ID},
    "video_gen": {"video": True, "assistant_id": VIDEO_ASSISTANT_ID},
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
_DIALECT_KEYS = ("assistant_id", "meta_data", "cogview", "video", "video_params")

_STREAM_TIMEOUT = 300


def _data_dir() -> pathlib.Path:
    try:
        from fp_core.platform_utils import get_data_dir
        return pathlib.Path(get_data_dir())
    except Exception:  # noqa: BLE001
        return pathlib.Path.home() / ".local" / "share" / "fp"


CRED_FILE = _data_dir() / "webai" / "glm" / "cookies.json"


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
                       "    python3 <fp数据目录>/public/plugins/webai/lib/glm_login.py")
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
    # ⚠️ 两者必须**合并**，不能"给了显式方言就丢掉 capability"（旧逻辑
    #    `if not d and capability` 正是如此）：`new_session(capability="video_gen",
    #    video_params={…})` 会因此丢掉 `video: True` → ask() 悄悄退回
    #    assistant/stream，请求打到**错误的端点**（静默走错，比报错难查得多）。
    #    显式方言优先级更高（覆盖能力默认值）。
    if capability:
        cap = {k: v for k, v in (CAPABILITY_MAP.get(capability) or {}).items()
               if k in _DIALECT_KEYS and v not in (None, "")}
        if cap:
            cap.update(d)
            d = cap
    if d:
        token = f"{NEW}:{uuid.uuid4().hex[:12]}"
        _remember_dialect(token, d)
        return token
    return NEW


# ── 生视频（独立端点族，实测 2026-09） ────────────────────────────
#
# 前端「AI生视频」的真实流量（Playwright 抓包，非推测）：
#   POST {VIDEO_API}/chat            → {"result":{"chat_id", "conversation_id", "status":"init"}}
#   GET  {VIDEO_API}/chat/status/<id> → {"result":{"status":"processing","plan":"44%",...}}
#                                      → {"result":{"status":"finished","video_url":"…mp4"}}
# 与聊天/生图**不同**：这里没有 assistant/stream，也不认 assistant_id。
# 故 ask() 见到方言 `video: True` 就分流到这里，返回 job；产物由 poll() 收。

# 前端实际提交的 base_parameter_extra（抄自抓包，逐字段对齐）。
# duration 单位秒（默认 5s）；resolution 为枚举非分辨率数字；fps=0 表示"由服务端定"。
_VIDEO_DEFAULTS = {
    "generation_pattern": 1,      # 1 = 文生视频（text2video）
    "resolution": 2,
    "fps": 0,
    "duration": 1,                # 前端字段名就叫 duration，实测产出 5s
    "generation_ai_audio": 0,
    "generation_ratio_width": 16,
    "generation_ratio_height": 9,
    "activity_type": 0,
    "label_watermark": 0,
    "generation_type": "",
}


def _snip(s: str, n: int = 300) -> str:
    s = " ".join((s or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def _video_json(r, what: str) -> dict:
    """video-api 的响应收口（glm 无 `_core`，按本模块约定抛 RuntimeError/AuthError）。"""
    if r.status_code in (401, 403):
        raise AuthError(f"GLM {what}：HTTP {r.status_code}（token 可能过期，重跑 glm_login.py）"
                        f"：{_snip(r.text)}")
    if r.status_code >= 400:
        raise RuntimeError(f"GLM {what} HTTP {r.status_code}: {_snip(r.text)}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"GLM {what}：响应不是 JSON：{_snip(r.text)}")


def _video_err(j) -> None:
    """video-api 用 `{"status":0,"message":"success"}` 表示成功 —— 非 0 即失败。"""
    if not isinstance(j, dict):
        raise RuntimeError(f"GLM video-api 返回非 JSON：{str(j)[:200]}")
    st = j.get("status")
    if st in (0, "0", None):
        return
    msg = str(j.get("message") or j.get("msg") or "未知错误")
    kind = classify(text=msg, raw=j) or "unknown"
    raise RuntimeError(f"GLM video-api 拒绝（status={st}，{kind}）：{msg}")


def _video_ask(conv: str, text: str, params: dict) -> dict:
    """创建视频任务 → 返回 job。**同步部分只负责提交**，产物去 poll() 拿。"""
    extra = dict(_VIDEO_DEFAULTS)
    extra.update({k: v for k, v in (params or {}).items() if v not in (None, "")})
    extra["prompt"] = text
    body = {
        "prompt": text,
        "conversation_id": conv or "",
        "advanced_parameter_extra": {},
        "base_parameter_extra": extra,
    }
    r = _session().post(f"{VIDEO_API}/chat", headers=_headers(), json=body,
                        timeout=_HTTP_TIMEOUT)
    j = _video_json(r, "video-api 提交")
    _video_err(j)
    res = j.get("result") if isinstance(j.get("result"), dict) else {}
    chat_id = str(res.get("chat_id") or "")
    if not chat_id:
        # 拿不到 chat_id 就没有可轮询的对象 —— 诚实失败，绝不返回空 job
        # （qwen 就是栽在"空成功"上，见 qwen._raise_for_ret）。
        raise RuntimeError(f"GLM video-api 未返回 chat_id：{json.dumps(res, ensure_ascii=False)[:300]}")
    return {
        "text": "",
        "thinking": "",
        "message_id": "",
        "references": [],
        "queries": [],
        "session_id": str(res.get("conversation_id") or conv or ""),
        "task_id": chat_id,
        "task_status": str(res.get("status") or "init"),
        "status": "job",
        "kind": "text2video",
        "extra": {"result": res},
    }


def poll(job) -> dict:
    """轮询视频任务（PROVIDER_SPEC.md §5）。`job` 可以是 dict 或 Job。"""
    if isinstance(job, dict):
        task_id = str(job.get("task_id") or job.get("id") or "")
    else:
        task_id = str(getattr(job, "id", "") or "")
    if not task_id:
        raise RuntimeError("poll() 需要 task_id（GLM 的 chat_id）")

    r = _session().get(f"{VIDEO_API}/chat/status/{task_id}", headers=_headers(),
                       timeout=_HTTP_TIMEOUT)
    j = _video_json(r, "video-api 轮询")
    _video_err(j)
    res = j.get("result") if isinstance(j.get("result"), dict) else {}

    st = str(res.get("status") or "processing")       # init | processing | finished
    plan = str(res.get("plan") or "")                 # "44%" / "100%" / "视频生成成功"
    video_url = str(res.get("video_url") or "")

    progress = 0.0
    m = re.match(r"(\d+(?:\.\d+)?)\s*%", plan)
    if m:
        progress = min(float(m.group(1)) / 100.0, 1.0)

    out = {
        "task_id": task_id,
        "task_status": "success" if st == "finished" else st,
        "content": video_url,
        "progress": progress,
        "remaining_time": "",
        "message": str(res.get("msg") or plan or ""),
        "kind": "video",
    }
    if video_url:
        out["assets"] = [{"kind": "video", "url": video_url}]
    return out


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

    # ── 视频分流 ────────────────────────────────────────────────
    # GLM 的视频**不走** assistant/stream（DESIGN §2 旧结论已证伪）：前端点「AI生视频」
    # 后发的是 POST video-api/v1/chat → 拿 chat_id → 轮询 chat/status/<chat_id>。
    # 故这里返回一个 job（`task_id` 即 chat_id），产物由 `poll()` 收。
    if dialect.get("video"):
        return _video_ask(conv, text, dialect.get("video_params") or {})

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
    """消息 content 的 schema（实测得来，与直觉差别很大）：

    图片：{"type": "image", "image": [{file_name, file_id, image_url, file_size, order, width, height}]}
        `image` 是**数组**、`image_url` 是**字符串**（不是 {"url":...} 对象）。
        猜过的 image_url 对象式全部被服务端静默忽略（模型回"看不到图片"）。
    文件：{"type": "file", "file": [{file_id, file_url, file_name, file_size, order, url, maxReadPercent}]}
        2026-09 实测三路对照的唯一正解：
          * 非图片塞进 type:"image" → **空流**（服务端静默失败）；
          * 顶层 file_list + question_type 旁路 → 模型说"没收到文件"；
          * 与 image 平行的 type:"file" 项（逐字复刻前端 M() 的 file_list 分支）→ 正确读出 .log 内容。
        混图+文时 image 项与 file 项并列，order 沿用全局序号。

    判型走 mime/扩展名：upload() 的 ref 不带类型，文件名是唯一可靠线索。
    """
    out = [{"type": "text", "text": text}]
    imgs, docs = [], []
    for i, f in enumerate(files or []):
        d = f if isinstance(f, dict) else {"ref": f}
        if _is_image(d):
            imgs.append({
                "file_name": d.get("name") or "image.png",
                "file_id": d.get("ref", ""),
                "image_url": d.get("url", ""),
                "file_size": d.get("size", 0),
                "order": i,
                "width": 0,
                "height": 0,
            })
        else:
            docs.append({
                "file_id": d.get("ref", ""),
                "file_url": d.get("url", ""),
                "file_name": d.get("name") or "file",
                "file_size": d.get("size", 0),
                "order": i,
                "url": d.get("url", ""),
                "maxReadPercent": 1,     # 小文件全读；大文件由服务端按上下文截断
            })
    if imgs:
        out.append({"type": "image", "image": imgs})
    if docs:
        out.append({"type": "file", "file": docs})
    return out


def _is_image(d: dict) -> bool:
    """按 mime/扩展名判是不是图片 —— 无 mime（.log 等）时回落到扩展名白名单。"""
    name = str(d.get("name") or "")
    mime = mimetypes.guess_type(name)[0]
    if mime:
        return mime.startswith("image/")
    return name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"))


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

_MODEL_ID_KEYS = ("model", "selected_model", "model_id", "model_name", "id")
_MODEL_NAME_KEYS = ("name", "display_name", "label", "title", "cn_name")
_MODEL_MODE_KEYS = ("modes", "efforts", "levels", "reasoning", "reasoning_levels",
                    "reasoning_efforts")
_MODEL_STRONG_ID_KEYS = ("model", "selected_model", "model_id", "model_name")
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


# 助手清单端点：`operation-api/assistant_top/v4/recent_list` **已下线（404，2026-09 实测）**，
# 前端现用 `feed-api/…`；`fixed_assistant` tag 则是「AI画图 / 视频助手」这类固定入口的权威来源。
# 两个都留：任一可用即可判，全部失效才降级 None。
_ASSISTANT_SOURCES = (
    f"{BASE}/chatglm/feed-api/assistant_top/v4/recent_list",
    f"{OPERATION_API}/config/operation_data?tag=fixed_assistant",
    ASSISTANT_LIST_URL,          # 旧端点（现已 404），保留兜底
)


def probe(capability: str = "") -> dict:
    """尽力探测某能力是否可用。

    * 视频 → 直接打 `video-api/v1/chat/list`（**免费 GET**，能列出即证明端点可用）。
      这是权威探针：视频不走 assistant/stream，助手清单里有没有它都无关。
    * 其余 → 多源助手清单（见 `_ASSISTANT_SOURCES`）。

    **允许返回 `{"supported": None}` —— 不知道就说不知道。** 离线 / 凭据过期时一律 None。
    """
    out = {"supported": None, "via": "", "note": ""}

    # 视频：独立端点族，用它自己的列表接口做探针
    if capability == "video_gen":
        out["via"] = "video_api"
        try:
            r = _session().get(f"{VIDEO_API}/chat/list?page=1&page_size=1",
                               headers=_headers(), timeout=_HTTP_TIMEOUT)
            out["note"] = f"HTTP {r.status_code}"
            if r.status_code == 200:
                out["supported"] = True
                out["note"] += "（chat/list 可访问 → 端点在用）"
            elif r.status_code in (401, 403):
                out["supported"] = False
                out["note"] += "（凭据被拒 → 先跑 glm_login.py）"
        except Exception as e:  # noqa: BLE001
            out["note"] = f"{type(e).__name__}: {e}"[:120]
        return out

    ids: set = set()
    notes: list = []
    # ⚠️ 必须**合并所有源**而不是"第一个非空就 break"：
    #   feed-api 的 recent_list 只有「最近用过」的 3 个，fixed_assistant 才含 AI画图/视频助手。
    #   早退会把 image_gen 误报成不支持（实测踩过）。
    for url in _ASSISTANT_SOURCES:
        try:
            r = _session().get(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
            notes.append(f"{url.rsplit('/', 1)[-1][:40]}→{r.status_code}")
            if r.status_code == 200:
                before = len(ids)
                ids |= _collect_assistant_ids(r.json())
                if len(ids) > before:
                    out["via"] = "assistant_list"
        except Exception as e:  # noqa: BLE001
            notes.append(f"{type(e).__name__}"[:30])
    out["note"] = "; ".join(notes)
    if not ids:
        out["note"] = "助手清单全部失效：" + out["note"]
        return out
    if not capability:
        out["note"] = f"助手清单（多源合并）{len(ids)} 项"
        return out
    mapping = CAPABILITY_MAP.get(capability) or {}
    aid = str(mapping.get("assistant_id") or "")
    if aid:
        # 诚实边界：这些端点返回的是「最近/固定助手」**子集**，不是全量目录。
        # 所以"不在清单中"只能说明**没有证据**，不能证明不支持 → 给 None 而非 False。
        # （False 会误导调用方直接放弃一个实测可用的能力，比 None 危险得多。）
        if aid in ids:
            return {"supported": True, "via": "assistant_list",
                    "note": f"{capability} → assistant_id={aid}：在清单中"}
        return {"supported": None, "via": "assistant_list",
                "note": (f"{capability} → assistant_id={aid}：不在所查清单中，"
                         f"但清单仅含最近/固定助手（非全量），故不能断定不支持 → None")}
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

def search(query: str, *, think: bool = False, model: str = "",
           files: list | None = None) -> dict:
    sid = new_session(model)
    r = ask(sid, query, model=model, think=think, search=True, files=files)
    # 原样带上 ask() 的**所有**键（含将来新增的），只补 session_id。
    # 之前这里手写 5 个键 —— 新增的 assets/phases/model/extra 会被静默丢掉，
    # 正是"未知的东西不能丢"在 search() 边界上被违反。
    out = dict(r)
    out.setdefault("session_id", sid)
    return out


# ── 上传（图片）────────────────────────────────────────────────

def upload(path: str) -> dict:
    """上传文件 → {ref, name, size, url}，ref/url 放进 ask(files=[...])。

    端点是通用 multipart（mime 按扩展名猜，未知回落 image/png 且服务端照收）：
    实测 .log 上传成功且模型能读 —— 图片与否只影响 _content 用哪套 schema，
    不影响这里。
    """
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


# ── 凭据自检（login.py 的 --check / 静默刷新后校验用）─────────────

def verify() -> tuple:
    """轻量探测：凭据是否被服务端认可。→ ("ok","") / ("dead",why) / ("unknown",why)

    注意 **不能用 new_session()** —— GLM 没有建会话 API（返回占位符 "new"，不发请求）。
    这里用 ``available_models`` 配置接口（与 models() 同源）。
    ⚠️ 该端点是否需要鉴权未联网验证；拿不准一律 unknown（不触发自动登录）。
    """
    try:
        r = _session().get(MODELS_URL, headers=_headers(),
                           params={"tag": "available_models"}, timeout=20)
    except Exception as e:  # noqa: BLE001
        return "unknown", f"网络/请求异常：{type(e).__name__}: {e}"
    if r.status_code == 200:
        return "ok", ""
    if r.status_code in (401, 403):
        return "dead", f"HTTP {r.status_code}（凭据被拒）"
    return "unknown", f"HTTP {r.status_code}: {(r.text or '')[:160]}"
