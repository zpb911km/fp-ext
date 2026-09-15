"""
Qwen provider (chat.qwen.ai)
=============================

三种能力共用同一个端点，只是标志位不同：
    chat    POST /api/v2/chat/completions      续接靠 chat_id + parent_id
    search  同上，auto_search=True，从 web_search phase 抽结构化引用
    vision  先把文件传到 OSS（getstsToken + oss2），再挂到 message.files

续接机制：parent_id = 上一轮 assistant 的 response_id（SSE 每帧顶层字段），
服务端自持上下文，客户端无需回传历史。

生成类能力（t2i / t2v / slides / web_dev / deep_research …）与普通聊天**同一个端点**，
靠 `chat_type` / `sub_chat_type` 区分。⚠️ 能力是**会话级属性**（`chats/new` 时就定了），
不是每条消息能切 —— 见 PROVIDER_SPEC.md §7 与 DESIGN.md §4.5。

凭据：<数据目录>/webai/qwen/cookie 或环境变量 QWEN_COOKIE（与 ask_llm / vision 共用）。
"""

import json
import mimetypes
import os
import re
import sys
import time
import uuid

import requests

try:                                    # provider → core 是允许的方向（core.py 顶部声明）
    from . import core as _core
except Exception:                       # noqa: BLE001  被当独立模块加载时降级
    _core = None

name = "qwen"
capabilities = {"chat", "search", "vision"}
default_model = "qwen3.7-plus"

API_BASE = "https://chat.qwen.ai"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_COOKIE_FILE = os.path.expanduser("~/.local/share/fp/webai/qwen/cookie")
_HTTP_TIMEOUT = 30
_STREAM_TIMEOUT = 300

# 流重试：**只在"一个字都没收到"时**重发（见 ask()）。带着半截回答重发会污染会话。
_STREAM_ATTEMPTS = 2
_STREAM_RETRY_BACKOFF = 1.2

_NET_ERRORS = (requests.exceptions.RequestException, OSError)



# ── 能力方言（PROVIDER_SPEC.md §1）──────────────────────────────
# 能力名（用平台自己的叫法） → 建会话时要带的参数。
# 这一层只负责"如实描述本家方言"，不做统一。能力在 chats/new 时确定 → 会话级。
CAPABILITY_MAP = {
    "t2t": {"chat_type": "t2t", "sub_chat_type": "t2t"},
    "t2i": {"chat_type": "t2i", "sub_chat_type": "t2i"},
    "t2v": {"chat_type": "t2v", "sub_chat_type": "t2v"},
    "slides": {"chat_type": "slides", "sub_chat_type": "slides"},
    # 实测：菜单「网页开发」→ chat_type=artifacts（注意不是 web_dev）+ sub_chat_type=web_dev
    "web_dev": {"chat_type": "artifacts", "sub_chat_type": "web_dev"},
    # 实测：菜单「深入研究」→ chat_type=deep_research + sub_chat_type=deep_thinking
    "deep_research": {"chat_type": "deep_research", "sub_chat_type": "deep_thinking"},
    "search": {"chat_type": "t2t", "sub_chat_type": "t2t"},
}

# 服务端用 `stream: false` + 轮询 `/task/status` 的能力（实测只有 t2v）。
# 这是服务端给的**显式信号**，不用猜。
_ASYNC_CHAT_TYPES = {"t2v"}

# 交互式能力：会**主动追问**（首轮不出产物）。见 § needs_input。
_INTERACTIVE_CHAT_TYPES = {"slides", "deep_research"}

# 会话 → 方言。能力是会话级属性，但 `ask()` 签名不能为它加参数（铁律 §0.1），
# 所以建会话时把方言记在这里，发消息时取回。会话没了就退回默认 t2t —— 优雅降级。
_SESSION_CAPS: dict = {}


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
        return False, "未找到 Qwen cookie：<数据目录>/webai/qwen/cookie 或环境变量 QWEN_COOKIE"
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


# ── 错误收口：HTTP / WAF / 解析失败 → 一律带归类 ─────────────────
#
# 为什么必须收口（实测 2026-09）：WAF 挑战页是 **HTTP 200 + text/html**，
# 于是 `r.json()` 抛 `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`
# —— 一句与真实原因毫无关系的乱码。`new_session()` 以前正是如此：用户看到的是
# 莫名其妙的"断了"，而不是"被 WAF 拦了"。
#
# 另一件事必须说清：**WAF 与登录态无关**。实测消融（cookie 全删风控指纹
# tfstk/isg/ssxmod_itna/acw_tc 照样 200 通过；只留 UA 则被拦；UA 换成
# python-requests 被拦、curl 放行）。判别维度是**请求头指纹**，不是凭据。
# 所以这里的文案刻意**不提 cookie 失效** —— 否则会把排查引向重新登录（白忙）。
_WAF_MSG = (
    "请求被 Aliyun WAF 判定为自动化（与登录态无关）。"
    "判别依据是请求头指纹 / UA，不是 cookie —— 实测风控 cookie 全删也照样通过。"
    "重登无用；请检查 _headers() 是否被改动、UA 是否被替换成库 UA"
    "（python-requests 被点名拦截，curl 反而放行）。"
)


def _new_error(kind: str, message: str, *, raw=None) -> Exception:
    if _core is not None:
        return _core.WebAIError(kind, message, provider="qwen", raw=raw)
    return RuntimeError(f"{kind}: {message}")


def _no_retry(err: Exception) -> Exception:
    """打上"别让上层再重发"的标记（`__init__._wrap_heal` 认得它）。

    两种情形必须打：
    * **ask() 内部已经重发过** —— 而且是**复用同一份请求体**重发的，
      比上层重建一个新 fid 去重发安全得多（少一次"会话里多一条相同提问"的机会）；
    * **WAF 挑战** —— 那是请求指纹配置问题，不是抖动。重发既不会变好，
      还会连续喂给风控引擎、反而抬高风险分。
    """
    try:
        err.no_retry = True          # type: ignore[attr-defined]
    except Exception:                # noqa: BLE001
        pass
    return err


def _is_waf(body: str) -> bool:
    """WAF 挑战页 / 任意 HTML 页 —— 它们都不可能是本接口的正常响应。"""
    head = (body or "")[:2048].lstrip().lower()
    return ("aliyun_waf" in head
            or head.startswith("<!doctype")
            or head.startswith("<html"))


def _snippet(s, n: int = 300) -> str:
    s = " ".join((s or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def _raise_for(r, body: str) -> None:
    """HTTP 层收口：WAF 挑战 / 非 2xx。不返回，只在出错时抛。"""
    if _is_waf(body):
        # 与登录态无关（见 _WAF_MSG）；重发无益 → 标记 no_retry
        raise _no_retry(_new_error("transient", _WAF_MSG, raw=_snippet(body, 400)))
    if r.status_code >= 400:
        kind = _core.classify_text(body, status=r.status_code).value if _core else ""
        raise _new_error(
            kind or "transient",
            f"HTTP {r.status_code}：{_snippet(body, 300)}",
            raw=_snippet(body, 400),
        )


def _json(r, what: str) -> dict:
    """`r.json()` 的安全版：WAF / 非 JSON 响应不再冒泡成 JSONDecodeError。"""
    text = r.text
    _raise_for(r, text)
    try:
        return r.json()
    except ValueError:          # requests 的 JSONDecodeError 也继承自 ValueError
        raise _new_error("transient",
                         f"{what}：响应不是 JSON（{_snippet(text)}）", raw=_snippet(text, 400))


def _collect_stream(r) -> tuple:
    """收 SSE 行 → `(raw, 是否见到 [DONE], 中断异常|None)`。

    ⚠️ **实测（2026-09，联网）：qwen 的流根本不发 `data: [DONE]`。**
    它靠 `Transfer-Encoding: chunked` 正常收尾（末帧带 `usage.total_tokens`，
    但 `usage` 从第 2 帧起就一直在，**当不了结束标记**）。所以"没见到 [DONE]"
    **不能**当作被截断 —— 那会把每一次正常回答都误报成 truncated。

    可靠的截断信号只有一个：**传输层报错**。分块编码下服务端若没写完就断开，
    urllib3 会抛 `IncompleteRead` → `ChunkedEncodingError`。
    反过来，服务端**主动**提前结束（风控掐断 / 用户点停止）在协议层没有任何标记，
    客户端分不出来 —— 这是本方法的诚实边界，别假装能检测。

    断流时已收到的内容**照收不误**（铁律：不丢东西），由调用方决定怎么办。
    """
    buf: list = []
    try:
        for line in r.iter_lines(decode_unicode=True):
            if line is None:
                continue
            buf.append(line)
            if line.strip() == "data: [DONE]":
                return "\n".join(buf) + "\n", True, None
    except _NET_ERRORS as e:
        return "\n".join(buf) + "\n", False, e
    return "\n".join(buf) + "\n", False, None


# ── 会话 ────────────────────────────────────────────────────────

def new_session(model: str = "", capability: str = "", **dialect) -> str:
    """建会话。`capability` + 多余额外关键字由 `**dialect` 吃掉（§7）。

    `__init__.new_session()` 会按 `CAPABILITY_MAP` 把能力展开成 dialect 传进来
    （如 `capability="web_dev"` → `chat_type="artifacts", sub_chat_type="web_dev"`）。
    也允许调用方直接传 `chat_type=...` 等。**不支持的能力一律忽略，绝不抛异常。**
    """
    model = model or default_model

    body = {"model": model}
    # 直接调用时可能只给 capability；map 里有就展开，没有就忽略。
    params = dict(CAPABILITY_MAP.get(capability or "", {}) or {})
    params.update({k: v for k, v in dialect.items() if v not in (None, "")})
    for k, v in params.items():
        body.setdefault(k, v)

    r = _session().post(
        f"{API_BASE}/api/v2/chats/new", headers=_headers(),
        json=body, timeout=_HTTP_TIMEOUT,
    )
    j = _json(r, "建会话")
    if not j.get("success"):
        # 服务端把错误码放在 body 里（如 {"code":"Unauthorized",...}）→ 归类后再抛，
        # 让 AUTH 仍然能触发上层凭据自愈（老行为：靠 RuntimeError 文案被词表捞到）。
        blob = json.dumps(j, ensure_ascii=False)
        kind = _core.classify_text(blob).value if _core else ""
        raise _new_error(kind or "unknown",
                         f"建会话失败：{_snippet(blob, 200)}", raw=j)

    sid = j["data"]["id"]
    ct = body.get("chat_type", "")
    sct = body.get("sub_chat_type", "")
    if ct or sct:
        _SESSION_CAPS[sid] = {"chat_type": ct or sct, "sub_chat_type": sct or ct}
    return sid


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
    sts = _json(r, "取 OSS STS")

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


# ── 产物识别（§2 assets）────────────────────────────────────────
# 铁律：**未知的东西不能丢**。所以这里只做"认出已知的 + 保守地兜未知的"，
# 绝不拿白名单去筛。识别不出的产物仍会留在 phases 里，不会静默消失。

_URL_RE = re.compile(r"https?://[^\s\"'<>`\\\)\]]+")


def _extract_urls(text: str) -> list:
    """从一段文本里抠 URL（Qwen 的产物 URL 是混在 content 文本里的）。"""
    if not text or not isinstance(text, str):
        return []
    out = []
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;:!?、。，）)]}\"'`*")
        if u and u not in out:
            out.append(u)
    return out


def _jsonish(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)


def _guess_kind_from_url(url: str) -> str:
    u = (url or "").lower().split("?", 1)[0].split("#", 1)[0]
    for ext, kind in (
        (".png", "image"), (".jpg", "image"), (".jpeg", "image"), (".webp", "image"),
        (".gif", "image"), (".bmp", "image"), (".svg", "image"),
        (".mp4", "video"), (".mov", "video"), (".webm", "video"), (".mkv", "video"),
        (".pdf", "pdf"), (".pptx", "pptx"), (".ppt", "pptx"),
        (".docx", "doc"), (".doc", "doc"), (".xlsx", "sheet"), (".csv", "sheet"),
    ):
        if u.endswith(ext):
            return kind
    return "file"


def _kind_for_chat_type(chat_type: str, url: str = "") -> str:
    ct = (chat_type or "").lower()
    if "video" in ct or ct == "t2v":
        return "video"
    if "image" in ct or ct == "t2i" or "draw" in ct:
        return "image"
    if "slide" in ct or "ppt" in ct:
        return "pptx"
    return _guess_kind_from_url(url)


def _assets_from_extra(extra) -> list:
    """从 extra 里搬运产物。**形状各家/各能力不同，只做保守搬运，不建模。**

    实测 slides 的产物在 `extra.slides.slide_pages[]`（PNG）+ `pdf_url`（PDF）；
    不同抓包可能是平的（直接在 extra 下），两种都认。
    """
    out = []
    if not isinstance(extra, dict):
        return out
    holder = extra.get("slides") if isinstance(extra.get("slides"), dict) else extra

    pages = None
    for key in ("slide_pages", "pages", "images", "image_list", "files"):
        v = holder.get(key) if isinstance(holder, dict) else None
        if isinstance(v, list) and v:
            pages = v
            break
    if pages is None and isinstance(extra.get("slide_pages"), list):
        pages = extra["slide_pages"]

    for pg in (pages or []):
        a = None
        if isinstance(pg, str) and pg.startswith("http"):
            a = {"kind": "image", "url": pg}
        elif isinstance(pg, dict):
            u = (pg.get("image_url") or pg.get("url") or pg.get("png_url")
                 or pg.get("src") or pg.get("download_url") or pg.get("file_url"))
            if isinstance(u, str) and u.startswith("http"):
                kind = _guess_kind_from_url(u)
                a = {"kind": kind if kind != "file" else "image", "url": u}
                for k in ("width", "height", "w", "h"):
                    if pg.get(k) is not None:
                        a[k] = pg[k]
        if a:
            out.append(a)

    pdf = None
    for src in (holder, extra):
        if isinstance(src, dict):
            pdf = src.get("pdf_url") or src.get("pdf") or src.get("pdf_file")
            if pdf:
                break
    if isinstance(pdf, str) and pdf.startswith("http"):
        out.append({"kind": "pdf", "url": pdf})
    elif isinstance(pdf, dict):
        u = pdf.get("url") or pdf.get("download_url") or pdf.get("file_url")
        if isinstance(u, str) and u.startswith("http"):
            out.append({"kind": "pdf", "url": u})
    return out


# ── SSE 解析 ────────────────────────────────────────────────────

def _parse_sse(raw: str) -> dict:
    """解析一个流。

    ⚠️ 最重要的一条（PROVIDER_SPEC.md §2）：**不准用 phase 白名单筛流**。
    流里每一个带 `phase` 的 delta 都记进 `phases`，未知 phase 也照收不误
    —— 已经因为"只认 answer / thinking 就丢掉 else"三次把产物静默丢掉了。

    - text    仍只取 `phase=="answer"`（老行为一字不变）
    - thinking 取 phase 含 "thinking" 的（老行为）
    - web_search typing→queries，finished→docs（老行为）
    - assets  从 image_gen / video_gen / slides / 未知 phase 的 extra 里捞（新增）
    - extra   所有 delta 的 extra 原样透传（新增）
    - 顶层 response_id 即下一轮的 parent_id
    """
    text, think, resp_id = "", "", None
    queries, docs = [], []
    phases: dict = {}
    extra_all: dict = {}
    assets: list = []
    asset_keys: set = set()
    model = ""
    needs_input = False
    seen = set()

    def _add_asset(a):
        if not a:
            return
        key = a.get("url") or (a.get("content") or "")[:64]
        if key and key in asset_keys:
            return
        if key:
            asset_keys.add(key)
        assets.append(a)

    def _merge_extra(e):
        if not isinstance(e, dict):
            return
        for k, v in e.items():
            if isinstance(v, dict) and isinstance(extra_all.get(k), dict):
                extra_all[k].update(v)
            else:
                extra_all[k] = v

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
        if not model and d.get("model"):
            model = d["model"]

        for ch in d.get("choices", []):
            delta = ch.get("delta", {}) or {}
            phase = str(delta.get("phase") or "")
            raw_c = delta.get("content")
            if raw_c is None:
                c = ""
            elif isinstance(raw_c, str):
                c = raw_c
            else:
                c = _jsonish(raw_c)
            de = delta.get("extra")
            _merge_extra(de)
            if not model and isinstance(de, dict) and de.get("model"):
                model = de["model"]

            # 交互式追问信号（保守）：草案预案 / 显式的等待输入标志。
            if isinstance(de, dict) and any(
                de.get(k) for k in ("draft_plan", "need_confirm", "need_input",
                                    "waiting_input", "require_input")
            ):
                needs_input = True
            st_low = str(delta.get("status") or "").lower()
            if st_low in ("waiting_input", "waiting_user", "needs_input",
                          "user_input", "require_input"):
                needs_input = True

            # ① 所有 phase 都收（未知也不丢）—— 最高优先级。
            if phase:
                phases.setdefault(phase, "")
                if c:
                    phases[phase] += c

            # ② web_search：老逻辑，只是不再 continue 掉后面的 phase 记账
            if phase == "web_search":
                if st_low == "typing":
                    args = (delta.get("function_call") or {}).get("arguments") or ""
                    if args:
                        try:
                            qs = json.loads(args).get("queries", [])
                            if qs:
                                queries = qs
                        except (json.JSONDecodeError, TypeError):
                            pass
                elif st_low == "finished":
                    tr = de.get("tool_result") if isinstance(de, dict) else None
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

            low = phase.lower()
            if phase == "answer":
                text += c
                continue
            if "thinking" in low:
                think += c
                continue

            # ③ 产物提取（新增）。认得准的用精确规则，认不准的保守兜底。
            #    无 phase 的帧（老代码也不处理）不参与产物提取，避免把正文 URL 误判成产物。
            if not phase:
                continue
            if "image" in low or low == "t2i":
                for u in (_extract_urls(c) or _extract_urls(_jsonish(de))):
                    _add_asset({"kind": "image", "url": u})
                for a in _assets_from_extra(de):
                    _add_asset(a)
            elif "video" in low or low == "t2v":
                for u in (_extract_urls(c) or _extract_urls(_jsonish(de))):
                    _add_asset({"kind": "video", "url": u})
                for a in _assets_from_extra(de):
                    _add_asset(a)
            elif "slide" in low or "pdf" in low or "ppt" in low:
                for a in _assets_from_extra(de):
                    _add_asset(a)
                for u in (_extract_urls(c) or _extract_urls(_jsonish(de))):
                    _add_asset({"kind": _guess_kind_from_url(u), "url": u})
            else:
                # 未知 phase：**不丢**。保守地只从 extra 里捞（避免把正文 URL 当产物）。
                for a in _assets_from_extra(de):
                    _add_asset(a)
                if isinstance(de, dict):
                    for u in _extract_urls(_jsonish(de)):
                        _add_asset({"kind": _guess_kind_from_url(u), "url": u})

    out = {
        "text": text,
        "thinking": think,
        "message_id": resp_id,
        "references": docs,
        "queries": queries,
        "phases": phases,
        "assets": assets,
        "extra": extra_all,
    }
    if model:
        out["model"] = model
    if needs_input:
        out["status"] = "needs_input"
    return out


# ── 发消息 ──────────────────────────────────────────────────────

def _ask_async(session_id: str, body: dict, model: str) -> dict:
    """异步能力（实测 t2v）：`stream:false`，提交后立刻返回 task_id，产物去 `poll()` 拿。

    ⚠️ 提交响应的确切结构未在本地抓包中确认（凭据会过期，未联网验证）。
    这里对 `data` / 顶层做了尽量宽的字段兼容，拿不准的一律留空而不是猜。
    """
    r = _session().post(
        f"{API_BASE}/api/v2/chat/completions?chat_id={session_id}",
        headers=_headers(), json=body, timeout=_HTTP_TIMEOUT,
    )
    j = _json(r, "异步提交")
    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, dict):
        data = j if isinstance(j, dict) else {}

    content = data.get("content") or ""
    task_id = (data.get("task_id") or data.get("id") or data.get("message_id")
               or data.get("response_id") or "")
    out = {
        "text": "",
        "thinking": "",
        "message_id": str(data.get("message_id") or data.get("response_id") or ""),
        "references": [],
        "queries": [],
        "model": model,
        "task_id": str(task_id or ""),
        "task_status": str(data.get("task_status") or data.get("status") or "pending"),
        "status": "job" if task_id else "",
    }
    if isinstance(content, str) and content.startswith("http"):
        out["assets"] = [{"kind": _kind_for_chat_type("t2v", content), "url": content}]
    return out


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

    # ⚠️ 实测：Qwen 只在思考模式下才真正触发联网搜索。
    #    think=False + auto_search=True 会被无视，模型直接回"我无法访问实时信息"，
    #    且返回 0 条引用 —— 静默失效，很难察觉。所以开搜索时强制打开思考。
    if search:
        think = True

    # 能力是**会话级属性**（chats/new 时定的）→ 取回方言决定本条消息的 chat_type。
    # 未注册 / 普通聊天保持 "t2t"，老行为一字不变。
    caps = _SESSION_CAPS.get(session_id) or {}
    chat_type = caps.get("chat_type") or "t2t"
    sub_chat_type = caps.get("sub_chat_type") or chat_type
    is_async = chat_type in _ASYNC_CHAT_TYPES

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
        "chat_type": chat_type,
        "feature_config": {
            "thinking_enabled": bool(think),
            "output_schema": "phase",
            "research_mode": "normal",
            "auto_thinking": bool(think),
            "thinking_mode": "Auto" if think else "Fast",
            "thinking_format": "summary",
            "auto_search": bool(search),
        },
        "extra": {"meta": {"subChatType": sub_chat_type}},
        "sub_chat_type": sub_chat_type,
    }
    body = {
        "stream": not is_async,
        "version": "2.1",
        "incremental_output": True,
        "chat_id": session_id,
        "chat_mode": "normal",
        "model": model,
        "parent_id": parent_message_id,
        "messages": [msg],
        "timestamp": int(time.time()),
    }

    if is_async:
        return _ask_async(session_id, body, model)

    url = f"{API_BASE}/api/v2/chat/completions?chat_id={session_id}"
    hdrs = {
        **_headers(),
        "Accept": "application/json, text/event-stream",
        "X-Accel-Buffering": "no",
    }
    # 请求体**固化**后再发：重试时复用同一份字节（含同一个 fid / timestamp），
    # 让服务端有机会把它认成同一条消息，而不是往会话里插两条。
    payload = json.dumps(body, ensure_ascii=False).encode()

    # `saw_done` 只作记录用：qwen 本来就不发 [DONE]，它当不了完整性判据（见 _collect_stream）
    raw, _saw_done, dropped = "", False, None
    for attempt in range(_STREAM_ATTEMPTS):
        if attempt:
            time.sleep(_STREAM_RETRY_BACKOFF)
        r = _session().post(url, headers=hdrs, data=payload,
                            stream=True, timeout=_STREAM_TIMEOUT)
        try:
            if r.status_code >= 400:
                _raise_for(r, r.text[:2048])        # 非 2xx：带 body 归类后抛
            raw, _saw_done, dropped = _collect_stream(r)
        finally:
            r.close()
        _raise_for(r, raw)                          # WAF 挑战页（HTTP 200 + HTML）
        # 只有"一个字都没收到"才重发 —— 半截回答不能被覆盖（宁留半截，不重复提问）
        if raw.strip():
            break
        if dropped is not None:
            print(f"[webai] qwen 流中断（{type(dropped).__name__}: {dropped}），"
                  f"未收到任何内容，重发", file=sys.stderr)
    else:
        raise _no_retry(_new_error("transient",
                                   "流未正常结束且没收到任何内容（连接被中断？）",
                                   raw=raw[:400]))

    head = raw.split("\n")[0] if raw else ""
    if '"ret"' in head and "FAIL" in head:
        try:
            err = json.loads(head[6:] if head.startswith("data: ") else head)
            ret = err.get("ret", ["未知错误"])
            raise _new_error("unknown",
                             f"API 拒绝：{ret[1] if len(ret) > 1 else ret[0]}", raw=ret)
        except json.JSONDecodeError:
            pass

    out = _parse_sse(raw)
    if dropped is not None:
        # 传输层中断：半截回答**照样返回**（铁律：不丢东西），但必须打标记。
        # 静默把"被截断"当成"完整"，正是"上下文莫名错位"的根源。
        out["truncated"] = True
        out["truncated_reason"] = f"{type(dropped).__name__}: {dropped}"
        print(f"[webai] qwen 流中断（已收 {len(raw)} 字节）→ 标记 truncated："
              f"{out['truncated_reason']}", file=sys.stderr)
    return out


# ── 便捷：一次性联网搜索 ────────────────────────────────────────

def search(query: str, *, think: bool = False, model: str = "") -> dict:
    """新会话 + 开搜索，返回 {text, references, queries}"""
    sid = new_session(model)
    r = ask(sid, query, model=model, think=think, search=True)
    # 原样带上 ask() 的**所有**键（含将来新增的），只补 session_id。
    # 之前这里手写 5 个键 —— 新增的 assets/phases/model/extra 会被静默丢掉，
    # 正是"未知的东西不能丢"在 search() 边界上被违反。
    out = dict(r)
    out.setdefault("session_id", sid)
    return out


# ── 可选钩子（PROVIDER_SPEC.md §3–§6）───────────────────────────
# 原则：一律**不往外抛异常**（调用方不该为一个可选能力崩），拿不准就返回 [] / None。

def models() -> list:
    """枚举可选模型（权威来源：`GET /api/v2/models`）。

    返回 `[{"id","name","capabilities":set,"ctx":int,"default":bool}]`。
    任何异常 → `[]`（诚实：没有就说没有，别硬编名字）。
    """
    try:
        r = _session().get(f"{API_BASE}/api/v2/models", headers=_headers(),
                           timeout=_HTTP_TIMEOUT)
        if r.status_code != 200:
            return []
        j = r.json()
    except Exception:  # noqa: BLE001
        return []
    return _normalize_models(j)


def _normalize_models(j) -> list:
    data = j.get("data") if isinstance(j, dict) else j
    if isinstance(data, dict):
        items = (data.get("models") or data.get("list") or data.get("items")
                 or data.get("data") or [])
    elif isinstance(data, list):
        items = data
    else:
        items = []

    out = []
    for it in items:
        if isinstance(it, str):
            out.append({"id": it, "name": it, "capabilities": set(), "ctx": 0})
            continue
        if not isinstance(it, dict):
            continue
        mid = (it.get("id") or it.get("model") or it.get("model_id")
               or it.get("name") or "")
        if not mid:
            continue
        caps = set()
        cap = it.get("capabilities")
        if isinstance(cap, dict):
            caps = {str(k) for k, v in cap.items() if v}
        elif isinstance(cap, (list, tuple, set)):
            caps = {str(x) for x in cap if x}
        elif isinstance(cap, str) and cap:
            caps = {cap}
        # 有些接口把能力拍平成布尔开关
        for flag in ("vision", "search", "thinking", "enable_thinking", "function_call"):
            if it.get(flag) is True:
                caps.add("thinking" if flag in ("enable_thinking",) else flag)
        ctx = (it.get("max_context_length") or it.get("context_length")
               or it.get("ctx") or it.get("max_input_tokens") or 0)
        try:
            ctx = int(ctx)
        except (TypeError, ValueError):
            ctx = 0
        d = {"id": str(mid), "name": str(it.get("name") or it.get("display_name") or mid),
             "capabilities": caps, "ctx": ctx}
        if it.get("default") or it.get("is_default"):
            d["default"] = True
        out.append(d)
    return out


def probe(capability: str = "") -> dict:
    """尽力探测某能力是否可用（`GET /api/v2/configs/` → feature_file / limits / permissions）。

    **允许返回 `{"supported": None}` —— 不知道就说不知道。**
    """
    out = {"supported": None, "via": "config_api", "note": ""}
    try:
        r = _session().get(f"{API_BASE}/api/v2/configs/", headers=_headers(),
                           timeout=_HTTP_TIMEOUT)
        out["note"] = f"HTTP {r.status_code}"
        if r.status_code != 200:
            return out
        j = r.json()
    except Exception as e:  # noqa: BLE001
        out["note"] = f"{type(e).__name__}: {e}"[:120]
        return out

    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, dict):
        data = j if isinstance(j, dict) else {}
    feature = data.get("feature_file") or data.get("features") or {}
    limits = data.get("limits") or {}
    perms = data.get("permissions") or {}

    if capability:
        key = capability.lower().replace("-", "_")
        for cont, label in ((perms, "permissions"), (feature, "feature_file"), (limits, "limits")):
            if not isinstance(cont, dict):
                continue
            v = cont.get(capability)
            if v is None:
                for k in cont:
                    if str(k).lower().replace("-", "_") == key:
                        v = cont[k]
                        break
            if v is not None:
                if isinstance(v, dict):
                    sup = bool(v.get("enabled", v.get("available", v.get("supported", True))))
                else:
                    sup = bool(v)
                return {"supported": sup, "via": "config_api",
                        "note": f"{label}.{capability}={_jsonish(v)}"[:160]}
        return {"supported": None, "via": "config_api",
                "note": f"config 里没有 {capability}"}

    names = []
    for cont in (feature, limits, perms):
        if isinstance(cont, dict):
            names += [str(k) for k in cont]
    return {"supported": None, "via": "config_api",
            "note": ("config keys: " + ", ".join(sorted(set(names))))[:200]}


def classify(text: str = "", status: int = 0, raw=None, exc=None) -> str:
    """本家方言优先，认不出返回 `""` → `__init__.classify()` 会退回 `core` 的通用词表。

    ⚠️ 返回 `""` 而不是 `"unknown"`：后者会**短路掉**通用词表（`core.classify_text`
    已含中英文全量提示词），反而更容易误判。未知就交给下层。
    """
    blob = text or (str(exc) if exc is not None else "")
    if not blob and isinstance(raw, dict):
        ret = raw.get("ret")
        if isinstance(ret, (list, tuple)):
            blob = " ".join(str(x) for x in ret)
        elif ret:
            blob = str(ret)
        if not blob:
            blob = str(raw.get("message") or raw.get("error") or raw.get("msg") or "")
    low = blob.lower()
    for hint, kind in (
        # ⚠️ WAF 挑战**必须**先于通用词表判掉。挑战页里常带 "acw_sc__v2"/"acw_tc"
        # 之类字样，而 core 的 AUTH 词表含 "cookie expired"/"签名" —— 一旦落到
        # 通用词表就可能被归成 AUTH，把"请求指纹不对"误诊成"凭据过期"，
        # 于是白刷一次登录。实测 WAF 与凭据**完全无关**。
        ("aliyun_waf", "transient"),
        ("acw_sc", "transient"),
        ("该模型不可用", "unsupported"),   # 实测本家方言
        ("模型不可用", "unsupported"),
        ("模型不存在", "unsupported"),
        ("not available", "unsupported"),
    ):
        if hint.lower() in low:
            return kind
    return ""


def poll(job) -> dict:
    """轮询异步任务（实测只有 t2v）：`GET /api/v2/task/status/<task_id>`。

    返回 `{"task_id","task_status","content","remaining_time","message"}`。
    `core.coerce_job()` 会做状态名归一（success→done、processing→running）。
    """
    if isinstance(job, dict):
        task_id = str(job.get("id") or job.get("task_id") or "")
        chat_type = str(job.get("chat_type") or job.get("kind") or "")
    else:
        task_id = str(getattr(job, "id", "") or "")
        chat_type = str(getattr(job, "kind", "") or "")
        if not task_id:
            meta = getattr(job, "meta", None)
            if isinstance(meta, dict):
                task_id = str(meta.get("task_id") or "")
    if not task_id:
        raise RuntimeError("poll() 需要 task_id")

    r = _session().get(f"{API_BASE}/api/v2/task/status/{task_id}", headers=_headers(),
                       timeout=_HTTP_TIMEOUT)
    j = _json(r, "任务状态")
    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, dict):
        data = j if isinstance(j, dict) else {}

    content = data.get("content") or data.get("url") or data.get("file_url") or ""
    if not isinstance(content, str):
        content = ""
    ctype = data.get("chat_type") or chat_type
    out = {
        "task_id": task_id,
        "task_status": data.get("task_status") or data.get("status") or "running",
        "content": content,
        "remaining_time": data.get("remaining_time") or "",
        "message": data.get("message") or data.get("error") or "",
    }
    if content:
        out["assets"] = [{"kind": _kind_for_chat_type(ctype, content), "url": content}]
    return out


# ── 凭据自检（login.py 的 --check / 静默刷新后校验用）─────────────

def verify() -> tuple:
    """轻量探测：凭据是否被服务端认可。→ ("ok","") / ("dead",why) / ("unknown",why)

    ⚠️ **实测教训（2026-09）：只读 GET 当不了探针。**
    把 cookie 换成垃圾后实测 —— `/api/v2/models` 仍返回 200（游客可访问），
    verify() 于是永远报 ok，`--check` 形同虚设、自愈也永不触发。
    改用 `new_session()`（POST /api/v2/chats/new）：这是**真的需要鉴权**的调用。
    代价是服务端多一个空会话 —— 与正常走 search() 时的开销相同，可接受。
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
