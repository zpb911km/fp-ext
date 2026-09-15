"""
StepFun provider (chat.stepfun.com) —— 阶跃星辰「阶跃AI」
=========================================================

协议要点（都是实测出来的，踩坑记录见文件末尾）
--------------------------------------------
* 表面是 gRPC 路径（`/api/agent/capy.agent.v1.AgentService/ChatStream`），
  实际是 **Connect 协议的 JSON 编码**：body 就是 JSON，不需要 protobuf。
* 鉴权 = **cookie**（`Oasis-Token` 等），但必须同时带一组 `oasis-*` 头，
  否则服务端回 401 `oasis-token is embezzled`（不是绑定，纯粹是缺头）。
* `Oasis-Token` = `accessJWT...refreshJWT`：**access 段只活 ~29 分钟**，
  到期是 401 `token is expired`（不是 cookie 坏了）。靠 `_PASSPORT/RefreshToken`
  静默续期（`refresh_credentials()`，见 PROVIDER_SPEC §11）—— 不做这步就是
  "每半小时必鉴权失败"。游客态 token 也是 200 返回的，必须校验 `activated`。
* ChatStream 的请求/响应都是 **Connect 信封流**：
      [1 字节 flags][4 字节大端长度][JSON payload]
    flags: 0x00=数据, 0x02=流结束
* 服务端自持上下文：请求体**只带 chatSessionId**，不带 parentMessageId。

凭据：<数据目录>/webai/stepfun/cookies.json（由 stepfun_login.py 生成，之后自动续期）
"""

import base64
import json
import mimetypes
import os
import pathlib
import sys
import time

import requests

try:                                    # provider → core 是允许的方向（core.py 顶部声明）
    from . import core as _core
except Exception:                       # noqa: BLE001  被当独立模块加载时降级
    _core = None

name = "stepfun"
capabilities = {"chat", "search", "vision"}
default_model = "step-auto"

# ── 新契约（可选钩子，见 PROVIDER_SPEC.md §1）────────────────────
# 无生成类能力：stepfun 的产物里没有「能力名 → 请求参数」的方言可声明，诚实留空。
CAPABILITY_MAP: dict = {}

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


COOKIE_FILE = _data_dir() / "webai" / "stepfun" / "cookies.json"


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
                       "    python3 <fp数据目录>/public/plugins/webai/lib/stepfun_login.py")
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


# ── 凭据续期（免浏览器，见 PROVIDER_SPEC §11）───────────────────
#
# 【实测】`Oasis-Token` = `accessJWT...refreshJWT`，**access 段只有 ~29 分钟寿命**
# （payload 里 exp-create_at ≈ 1800s）。过期后所有 API 一律回
#     401 {"code":"unauthenticated","message":"token is expired"}
# —— 这就是"stepfun 总是鉴权失败"的真身：不是 cookie 坏，是 access 段到期。
#
# 网页端每次进站都会调：
#     POST /passport/proto.api.passport.v1.PassportService/RefreshToken   body {}
# 用 refresh 段换新 access 段（响应里 `accessToken.raw` / `refreshToken.raw`，
# 同时 Set-Cookie `Oasis-Token`）。我们在 provider 内复刻这一步 → 静默续期，
# 不再需要每半小时开一次浏览器。

REFRESH_PATH = _PASSPORT + "/RefreshToken"
_REFRESH_SKEW = 90          # access 剩余寿命 < 90s 就提前换，省掉一次注定 401 的往返

# "可预期"的续期失败（网络/解析）→ 静默降级；其余异常 → 降级但打日志。
# ⚠️ 这两个类必须**在导入时**取好：若写成 `except (requests.RequestException, ...)`，
#    异常处理时才会去求值 `requests`，一旦它被替换/缺失，except 子句自己会抛
#    AttributeError 把原始异常盖掉（真实踩到过，见 tests §6g）。
_NET_ERRORS = (requests.RequestException, ValueError, KeyError, TypeError)


def _jwt_payload(token: str, idx: int = 0) -> dict:
    """解 Oasis-Token 第 idx 段（0=access, 1=refresh）的 JWT payload。解不出→{}（绝不抛）。"""
    try:
        seg = token.split("...")[idx].split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:  # noqa: BLE001
        return {}


def _access_exp(ck: dict) -> int:
    """access 段的 exp（epoch 秒）。拿不到 → 0（=不知道，交由 401 触发反应式续期）。"""
    try:
        return int(_jwt_payload(str(ck.get("Oasis-Token", ""))).get("exp") or 0)
    except Exception:  # noqa: BLE001
        return 0


def save_cookies(ck: dict) -> bool:
    """落盘 cookie（0600，**原子替换**）。失败只回 False —— 续期是尽力而为，不因写盘失败而炸。

    为什么原子：`load_cookies()` 在每次请求前都会读这个文件，而它被多个工具并发调用。
    直接 `write_text` 有一个"读到半个 JSON"的窗口 → JSONDecodeError → 被当成
    「未找到凭据」→ **又一个假的鉴权失败**。临时文件 + `replace()` 消除该窗口。
    """
    try:
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = COOKIE_FILE.with_name(COOKIE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(ck, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(COOKIE_FILE)
        return True
    except OSError:
        return False


def _set_cookie(r: requests.Response, name: str) -> str:
    """从响应头 `Set-Cookie` 取某个 cookie 的值。取不到 → ""。

    为什么不读 `Session.cookies`：`RequestsCookieJar.get()` 在**同名 cookie 有多个
    域/路径**时抛 `CookieConflictError`（Oasis-Webid 同时存在于 `.stepfun.com` 与
    `chat.stepfun.com`）。这里只要"服务端这次给我的值"，直接解析响应头最干净。
    """
    try:
        for c in (r.raw.headers.getlist("Set-Cookie") or []):
            head = c.split(";", 1)[0]
            if "=" in head:
                k, v = head.split("=", 1)
                if k.strip() == name:
                    return v
    except Exception:  # noqa: BLE001
        pass
    return ""


def refresh_credentials() -> bool:
    """用 refresh 段换新 access 段并落盘 → 是否成功。免浏览器、免登录脚本。

    ⚠️ 陷阱一（服务端）：凭据无效时**照样回 200**，但给的是一个**游客 token**
    （access 段 `activated: false`，拿去调用得到 403 `need sign in`）。
    所以不能只看状态码 —— 必须校验 access 段是激活态，否则游客态会覆盖掉
    好凭据（比不刷新更糟：本来只是过期，刷新后变成"权限不足"）。

    ⚠️ 陷阱二（我们自己的）：这里**绝不能静默吞掉非预期异常**。踩过一次：
    `s.cookies.get("Oasis-Webid")` 抛 `CookieConflictError`，被宽 except 吃掉
    → 续期"永远失败"→ 每次都退回开浏览器 → 症状与原病一模一样。
    所以：可预期的网络异常照旧回 False，**不认识的异常必须打到 stderr**。
    """
    ck = load_cookies()
    if not ck.get("Oasis-Token"):
        return False
    try:
        s = requests.Session()
        s.cookies.update(ck)
        r = s.post(f"{BASE}{REFRESH_PATH}", headers=_headers(), json={}, timeout=_HTTP_TIMEOUT)
        if r.status_code != 200:
            return False
        js = r.json()
        acc = ((js.get("accessToken") or {}).get("raw") or "")
        ref = ((js.get("refreshToken") or {}).get("raw") or "")
        if not acc or _jwt_payload(acc).get("activated") is False:   # ← 游客态，拒绝
            return False
        ck["Oasis-Token"] = acc + ("..." + ref if ref else "")
        wid = _set_cookie(r, "Oasis-Webid")                           # 顺带刷新设备指纹，有就收
        if wid:
            ck["Oasis-Webid"] = wid
        save_cookies(ck)
        return True
    except _NET_ERRORS as e:
        print(f"[webai] stepfun 续期失败（{type(e).__name__}: {e}）", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001  —— 不认识的异常：照样降级，但**必须留痕**
        print(f"[webai] stepfun 续期异常（{type(e).__name__}: {e}）—— 请当 bug 修",
              file=sys.stderr)
        return False


def _call(method: str, url: str, **kw) -> requests.Response:
    """发请求，带**本地凭据自愈**：临期先续，401 再续一次并重试。

    作用域刻意只落在"真实调用"（new_session / ask / upload）上，与 __init__ 的
    `_HealingProvider` 是**两级接力**：本地续期（~0.3s，绝大多数情况到此为止）
    → 仍失败才轮到它去开浏览器。这样 stepfun 的 30 分钟节奏不再打穿到工具层。
    """
    if (exp := _access_exp(load_cookies())) and exp - time.time() < _REFRESH_SKEW:
        refresh_credentials()
    r = getattr(_session(), method)(url, **kw)
    if r.status_code == 401 and refresh_credentials():
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass
        r = getattr(_session(), method)(url, **kw)
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

def new_session(model: str = "", capability: str = "", **dialect) -> str:
    """建会话。CAPABILITY_MAP 为空 → capability / dialect 一律忽略（绝不抛异常）。"""
    r = _call("post", f"{BASE}{SVC}/CreateChatSession",
              headers=_headers(), json={}, timeout=_HTTP_TIMEOUT)
    _check(r)
    if r.status_code != 200:
        raise RuntimeError(f"建会话失败 HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["chatSession"]["chatSessionId"]


# ── 发消息 ──────────────────────────────────────────────────────

def ask(session_id: str, text: str, *, parent_message_id=None, model: str = "",
        think: bool = False, search: bool = False, files=None) -> dict:
    """发消息并收完整流。parent_message_id 被忽略（服务端自持上下文）。

    返回 dict：
      老 key（原样保留）: text / thinking / message_id / references / queries
      新 key（可选，见 PROVIDER_SPEC §2）: assets / model / phases / extra
        * phases: 本次出现过的**每一个** event 类型（含未知）→ 累计文本（无文本记 ""）
        * extra : 原始事件全量留档 + 非 event 信封 + 未解析帧 —— 未知的东西不能丢
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

    with _call("post", f"{BASE}{SVC}/ChatStream",
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
    """解析 Connect 信封流 → 结果 dict。

    两条原则（PROVIDER_SPEC §2）：
      * 老 key（text/thinking/message_id/references/queries）行为**一字不变**；
      * **任何消息类型都不丢**：每个 event 名称都进 ``phases``，原始事件全量留档在
        ``extra["events"]``，非 event 信封进 ``extra["envelopes"]``，解析不出的帧进
        ``extra["unparsed_frames"]``。
        （注：这是"protobuf 帧解析处"在本家对应的落点 —— stepfun 实际走 Connect-JSON
        信封而非 protobuf，但"未知类型不能丢"的要求完全一致。）
    """
    text, think, refs = [], [], []
    queries: list = []
    msg_id = None
    model = ""
    phases: dict = {}          # event 名 → 累计文本（无文本的记 ""，表明"出现过"）
    raw_events: list = []      # 每个 event 原样留档（含未知类型）
    assets: list = []
    extra: dict = {}

    for i, (flags, o) in enumerate(iter_envelopes(raw)):
        if flags == 2:                      # 0x02 = end-of-stream trailer
            extra.setdefault("trailers", []).append(o)
            continue
        if o is None:                       # 有帧但 JSON 解不开 —— 只记存在，不猜内容
            extra.setdefault("unparsed_frames", []).append({"index": i, "flags": flags})
            continue
        data = o.get("data") if isinstance(o, dict) else None
        ev = (data or {}).get("event") if isinstance(data, dict) else None
        if not isinstance(ev, dict) or not ev:
            # 不是 event 信封（error / metadata / 未知结构）→ 原样留，绝不丢
            extra.setdefault("envelopes", []).append(o)
            continue

        for ev_name, body in ev.items():
            # ① 先登记 + 原样留档（认不认识都留）—— 未知消息类型不能丢
            phases.setdefault(ev_name, "")
            raw_events.append({"index": i, "event": ev_name, "body": body})
            if not isinstance(body, dict):
                continue

            # ② 已知语义解析（老行为保持不变）
            if ev_name == "startEvent":
                msg_id = body.get("messageId") or msg_id
            elif ev_name == "reasoningEvent":
                t = body.get("text") or ""
                think.append(t)
                phases[ev_name] += t
            elif ev_name == "textEvent":
                t = body.get("text") or ""
                text.append(t)
                phases[ev_name] += t
            elif ev_name == "messageEvent":
                m = body.get("message") or {}
                if isinstance(m, dict):
                    msg_id = m.get("messageId") or msg_id
                    model = m.get("model") or model
                    meta = m.get("meta")
                    trace = meta.get("traceId") if isinstance(meta, dict) else None
                    if trace:
                        extra.setdefault("trace_id", trace)
            elif ev_name == "pipelineEvent":
                _collect_refs(body, refs)
                # pipelineEvent.type ∈ REASONING / SEARCH —— 本家最像"phase"的东西
                ptype = body.get("type")
                if ptype:
                    phases.setdefault(f"pipeline:{ptype}", "")

            # ③ 产物：尽力提取（有就填；认不出绝不假造）
            assets.extend(_harvest_assets(body))

    if raw_events:
        extra.setdefault("events", raw_events)

    return {
        # 老 key —— 原样保留
        "text": "".join(text),
        "thinking": "".join(think),
        "message_id": msg_id,
        "references": refs,
        "queries": queries,
        # 新 key —— 有则更富，缺了不影响
        "assets": assets,
        "model": model,
        "phases": phases,
        "extra": extra,
    }


def _collect_refs(pe: dict, refs: list) -> None:
    """从 pipelineEvent 抽搜索引用（老逻辑原样保留）。

    注意：SEARCH 的 pipelineEvent.title 是「来源」，不是检索词 —— 别当 query 用。
    """
    es = pe.get("eventSearch") or {}
    for r0 in es.get("results") or es.get("searchResults") or []:
        if isinstance(r0, dict) and (r0.get("url") or r0.get("title")):
            refs.append({"url": r0.get("url", ""), "title": r0.get("title", ""),
                         "snippet": r0.get("snippet") or r0.get("content") or ""})


# 产物线索（best-effort）：stepfun 目前**未观测到**独立产物事件（SPEC 表：暂无），
# 下面只做保守提取 —— 只认「明确的产物键 + URL」，认不出绝不假造（宁缺勿假）。
# 已知的非产物 URL 键（如搜索结果的 favicon）显式排除。
_ASSET_KIND_BY_KEY = {
    "imageurl": "image", "image": "image", "images": "image",
    "videourl": "video", "video": "video", "videos": "video",
    "audiourl": "audio", "audio": "audio",
    "fileurl": "file", "file": "file", "files": "file", "resource": "file",
    "pdfurl": "pdf", "pdf": "pdf",
}
_SKIP_URL_KEYS = {"faviconurl", "avatar", "icon", "iconurl", "thumbnailurl"}


def _harvest_assets(body) -> list:
    """从事件体里尽力找出 URL 型产物（深度遍历，带 kind 上下文继承）。"""
    out: list = []
    stack = [(body, None)]
    while stack:
        node, hint = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if kl in _SKIP_URL_KEYS:
                    continue
                kind = _ASSET_KIND_BY_KEY.get(kl) or hint
                if isinstance(v, str):
                    if kind and v.startswith(("http://", "https://")):
                        out.append({"kind": kind, "url": v})
                elif isinstance(v, list):
                    stack.extend((it, kind) for it in v)
                elif isinstance(v, dict):
                    stack.append((v, kind))
        elif isinstance(node, list):
            stack.extend((it, hint) for it in node)
    return out


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
    r = _call("post", f"{BASE}/api/resource/image", headers=h,
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


# ── 新契约：模型枚举 / 能力探测 / 错误归类（PROVIDER_SPEC §3/§4/§6）──

def models() -> list:
    """枚举可选模型。stepfun **没有模型清单接口** → 诚实返回空（别硬编可能过期的名字）。"""
    return []


def probe(capability: str = "") -> dict:
    """尽力探测能力。stepfun 无配置/清单接口，只能在真实调用时观察 → 诚实返回 None。"""
    return {
        "supported": None,
        "via": "none",
        "note": "stepfun 无模型清单/能力探测接口；是否支持某能力只能真实调用观察，故不猜。",
    }


# stepfun 服务端文案方言（实测）
_AUTH_DIALECT = (
    "embezzled", "unauthenticated", "oasis-token", "鉴权失败",
    "cookie 失效", "cookie失效", "cookie 过期", "cookie过期",
    "登录已失效", "凭据失效",
)
# 「换个话题聊聊」= 模型自己回避了敏感内容，**不是技术缺陷** → content_policy
_POLICY_DIALECT = ("换个话题聊聊", "换个话题", "换一个话题", "聊点别的", "聊聊别的")


def classify(text: str = "", status: int = 0, raw=None, exc=None, **_) -> str:
    """stepfun 方言优先，其余交给 core.classify_text（通用中英文词表）。

    方言来历（实测）：
      * cookie / Oasis-Token 失效（401 `oasis-token is embezzled`）→ auth
      * 模型回「换个话题聊聊」→ 它自己回避了敏感内容（非技术故障）→ content_policy
    """
    if isinstance(exc, AuthError):
        return "auth"
    blob = text or ""
    low = blob.lower()
    if any(h.lower() in low for h in _AUTH_DIALECT):
        return "auth"
    if any(h in blob for h in _POLICY_DIALECT):
        return "content_policy"
    if _core is not None:
        return _core.classify_text(blob, status=status).value
    return ""      # 拿不到 core → 交上层 __init__.classify 兜底（假值即触发回退）


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

# 7. 【协议澄清】本实现走的是 **Connect-JSON 信封流（HTTP ChatStream）**，
#    不是 WebSocket + protobuf。页面上的 wss://.../botapi/wss/connection（带 protobuf 帧）
#    只是心跳/部署状态通道，正文一律走 HTTP。所谓"protobuf 帧解析处"在本家的落点是
#    iter_envelopes() / _parse() —— 未知 event 类型必须登记进 phases/extra，绝不丢。
#
# 8. 新增可选钩子（PROVIDER_SPEC.md）：CAPABILITY_MAP={} / models()->[] /
#    probe()->supported=None（诚实）/ classify() 收「换个话题聊聊」=content_policy、
#    cookie 失效=auth；不定义 poll()（无异步任务）。ask() 签名与老 key 一字未改。
#
# 9. 【致命】`Oasis-Token` 的 **access 段只有 ~29 分钟**（exp-create_at≈1800s）。
#    过期后不是"cookie 坏了"，是 `401 token is expired` —— 30 分钟后必然复现，
#    表现为用户嘴里的"stepfun 总是鉴权失败"。旧实现只靠 `login.silent_refresh`
#    开浏览器救场（慢、依赖 playwright/profile），所以体感就是"一直在失败"。
#    正解：**复刻网页端的 RefreshToken**（_PASSPORT/RefreshToken，body {}），
#    用 refresh 段换新 access 段 —— 见 refresh_credentials() / _call()。
#    ⚠️ 该端点凭据无效时**回 200 + 游客 token**（activated:false）→ 必须校验
#    activated，否则会把"过期"刷成"权限不足"（403 need sign in），比不刷更糟。


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
