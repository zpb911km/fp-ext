"""webai.core — 跨 provider 的公共词汇表

设计原则（见 DESIGN.md §5）：**契约少而稳，事实多而变**。

契约（本模块，几乎不变）::

    Reply   一次调用的结果（可能是答案、可能是"它在追问"、也可能是一个 Job）
    Job     异步任务（提交 → 轮询 → 取产物）
    Asset   产物（图 / 视频 / PDF / 内联源码 …）
    ErrorKind  六类错误（客户端只做归类，不建模限额）

事实（由各 provider 透传 / 运行时探测，本模块不枚举）::

    能力名 chat_type / assistant_id / 模型名 / 参数 / 限额 / 权限

两条硬约束，来自实测（DESIGN.md §7）：

1. **未知的东西不能丢。** 已经三次踩到"用已知 phase 白名单筛流，结果产物被静默丢弃"
   （deepseek 的 ``search_status``、qwen 的 ``image_gen`` / ``slides``）。
   所以 ``Reply.meta`` / ``Job.meta`` 必须能装下 provider 原样给的 ``extra``。
2. **产物不一定有 URL。** query ``web_dev`` 返回的是内联 HTML 源码，没有链接。
   所以 ``Asset.url`` 与 ``Asset.content`` 都可为空，二者至少有一个。

本模块只用标准库，不 import 任何 provider —— 依赖方向永远是 provider → core。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

__all__ = [
    "ASSET_KINDS",
    "JOB_STATES",
    "REPLY_STATUS",
    "CAPABILITY_ALIASES",
    "canonical",
    "same_capability",
    "Asset",
    "Job",
    "Reply",
    "ErrorKind",
    "WebAIError",
    "coerce_asset",
    "coerce_assets",
    "coerce_reply",
    "coerce_job",
    "looks_like_job",
    "kind_of",
    "retry_hint",
    "classify_text",
    "AUTH_HINTS",
    "QUOTA_HINTS",
    "UNSUPPORTED_HINTS",
    "TRANSIENT_HINTS",
    "POLICY_HINTS",
]

# ── 词汇表 ────────────────────────────────────────────────────────
# 开放字符串：这里是"已知的常见值"，不是白名单。未知值一律原样保留。

ASSET_KINDS = (
    "image",
    "video",
    "audio",
    "pdf",
    "pptx",
    "doc",
    "sheet",
    "code",
    "webpage",
    "report",
    "poster",
    "file",
    "text",
)

JOB_STATES = ("pending", "running", "done", "failed", "cancelled")

REPLY_STATUS = (
    "answered",   # 正常答完
    "needs_input",  # 交互式能力在追问（slides / deep_research）→ 调用方需再发一轮
    "job",        # 返回的是异步任务，去轮询
    "partial",    # 拿到了东西但不完整（流中断等）
    "empty",      # 什么都没拿到（通常是解析问题，不是模型问题）
)


# ── 能力别名：只用于**跨厂商匹配**，不用于发起调用 ──────────────
# 实测各家用不同名字指同一件事：Qwen 叫 t2i，GLM 叫 image_gen。
# 如果连"这是不是同一个能力"都判不出来，多后端路由就无从谈起。
#
# 设计边界（重要）：
#   * **发起调用**一律用各家自己的名字 —— 以 provider 的 CAPABILITY_MAP 为准，原样透传。
#   * 这张表**只在"要不要交给某一家"这种判断里用**（find / has / first_available）。
#   * 表是数据，可以随时加一行；加错了最多影响路由，不影响调用正确性。
CAPABILITY_ALIASES: dict[str, str] = {
    # 生图
    "t2i": "image_gen", "text2img": "image_gen", "text_to_image": "image_gen",
    "draw": "image_gen", "drawing": "image_gen", "painting": "image_gen",
    "image_generation": "image_gen", "cogview": "image_gen", "picture": "image_gen",
    "image_edit": "image_edit", "edit_image": "image_edit", "image_edit_tool": "image_edit",
    # 生视频
    "t2v": "video_gen", "text2video": "video_gen", "text_to_video": "video_gen",
    "video_generation": "video_gen",
    # 幻灯片 / PPT
    "slides": "slides", "ppt": "slides", "slide": "slides",
    "presentation": "slides", "deck": "slides",
    # 深度研究
    "deep_research": "deep_research", "research": "deep_research",
    "deep_thinking": "deep_research", "report": "deep_research",
    # 网页 / 前端
    "web_dev": "web_dev", "webpage": "web_dev", "web": "web_dev",
    "html": "web_dev", "artifacts": "web_dev", "webpage_model_starter": "web_dev",
    # 数据分析
    "da": "data_analysis", "data_analysis": "data_analysis",
    "engine_da": "data_analysis", "analysis": "data_analysis",
    "code_interpreter": "code_interpreter", "python": "code_interpreter",
    # 海报
    "poster": "poster", "engine_poster": "poster", "illustration": "poster",
    # 检索 / 多模态（这些已有静态声明，别名用于跨家对齐）
    "web_search": "search", "search_image": "search", "web_search_image": "search",
    "image": "vision", "vlm": "vision", "image_understanding": "vision",
    "file": "attach", "upload": "attach", "document": "attach",
}


def canonical(capability: str) -> str:
    """把各家方言折叠成通用能力名（**仅供匹配**，不可用于调用）。"""
    c = (capability or "").strip().lower()
    return CAPABILITY_ALIASES.get(c, c)


def same_capability(a: str, b: str) -> bool:
    return canonical(a) == canonical(b)


class ErrorKind(str, Enum):
    """六类错误。客户端只做归类 —— 限额是服务端状态，建模不了也不必建模。"""

    AUTH = "auth"                    # 凭据失效 → 提示重跑 *_login.py，不重试
    QUOTA = "quota"                  # 额度/频率用完 → 不重试，换一家
    UNSUPPORTED = "unsupported"      # 该能力/模型不可用 → 更新探测缓存，降级到别家
    TRANSIENT = "transient"          # 网络/服务端抖动 → 退避重试
    CONTENT_POLICY = "content_policy"  # 内容策略拒绝 → 重试无用（复读会同样失败）
    UNKNOWN = "unknown"


class WebAIError(Exception):
    """带归类的异常。provider 内部抛出，工具层按 kind 决定重试 / 换家 / 提示。"""

    def __init__(self, kind: ErrorKind | str, message: str, *, provider: str = "", raw: Any = None):
        self.kind = ErrorKind(kind) if not isinstance(kind, ErrorKind) else kind
        self.provider = provider
        self.raw = raw
        super().__init__(message)

    def __str__(self) -> str:
        tag = f"[{self.provider}] " if self.provider else ""
        return f"{tag}{self.kind.value}: {self.args[0]}"


# ── 产物 ─────────────────────────────────────────────────────────


@dataclass
class Asset:
    """归一化产物。

    各家的产物形状完全不同（实测三种 + 内联源码共四种）::

        Qwen t2i   URL 混在 delta.content 的文本里
        GLM  生图   content[].type=="image" → image[].image_url（一次 4 张）
        Qwen t2v   task/status 的 content 字段
        Qwen web_dev  正文就是 HTML 源码，没有 URL

    所以 url / content 都可为空，二者至少有一个。
    """

    kind: str = "file"
    url: Optional[str] = None
    content: Optional[str] = None      # 内联内容（源码 / 文本），无 URL 时用
    path: Optional[str] = None         # 已落盘路径
    mime: str = ""
    name: str = ""
    size: Optional[int] = None
    meta: dict = field(default_factory=dict)   # w/h/duration/pages… 原样

    def __post_init__(self) -> None:
        if self.kind not in ASSET_KINDS:
            # 未知 kind 不报错，原样保留 —— 别再用白名单筛别人的东西
            self.meta.setdefault("kind_raw", self.kind)

    @property
    def local(self) -> bool:
        return bool(self.path and os.path.exists(self.path))

    def save(self, directory: str, *, filename: str = "", headers: dict | None = None,
             timeout: int = 60) -> "Asset":
        """下载到本地并回填 ``path``。

        实测：Qwen 的产物 URL 带 JWT（含 resource_user_id，会过期）。
        **取到即落盘** 是唯一安全做法，不能只存 URL。
        """
        if self.content is not None and not self.url:
            os.makedirs(directory, exist_ok=True)
            name = filename or self.name or f"{self.kind}-{int(time.time())}.txt"
            p = os.path.join(directory, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.content)
            self.path = p
            return self
        if not self.url:
            return self
        os.makedirs(directory, exist_ok=True)
        name = filename or self.name or _guess_name(self.url, self.kind)
        p = os.path.join(directory, name)
        req = urllib.request.Request(self.url, headers=headers or {"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            with open(p, "wb") as f:
                f.write(r.read())
        self.path = p
        if not self.size:
            try:
                self.size = os.path.getsize(p)
            except OSError:
                pass
        return self

    def summary(self) -> dict:
        """给人类/日志看 —— **刻意不含 url 全文**（含签名与 user_id）。"""
        d = {"kind": self.kind}
        if self.path:
            d["path"] = self.path
        if self.content is not None:
            d["content_len"] = len(self.content)
        if self.url:
            d["url_host"] = _host_of(self.url)
        for k in ("w", "h", "duration", "pages"):
            if k in self.meta:
                d[k] = self.meta[k]
        return d


def _host_of(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except Exception:
        return ""


def _guess_name(url: str, kind: str) -> str:
    base = url.split("?", 1)[0].rsplit("/", 1)[-1]
    if not base or "." not in base:
        ext = {"image": ".png", "video": ".mp4", "audio": ".mp3", "pdf": ".pdf"}.get(kind, ".bin")
        base = f"{kind}-{int(time.time())}{ext}"
    if len(base) > 80:
        base = base[-80:]
    return base


def coerce_asset(raw: Any, *, kind: str = "") -> Optional[Asset]:
    """把各家千奇百怪的产物描述统一成 Asset。宽容优先 —— 认不出就返回 None。

    ``kind`` 只是**兜底默认值**（通常是调用时声明的能力名，如 "t2i"）。
    provider 若给了更精确的 kind（"image"/"video"/"pdf"），**以 provider 为准** ——
    否则 "video" 会被能力名 "t2v" 覆盖掉，Asset.kind 就失去意义了。
    """
    if raw is None:
        return None
    if isinstance(raw, Asset):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.startswith("http://") or s.startswith("https://"):
            return Asset(kind=kind or "file", url=s)
        # 无 URL 的长文本 = 内联内容（如 web_dev 的 HTML）
        return Asset(kind=kind or "text", content=s)
    if not isinstance(raw, dict):
        return None

    declared = str(raw.get("kind") or raw.get("type") or "").strip()
    a = Asset(kind=declared or kind or "file")
    for k in ("url", "image_url", "video_url", "file_url", "download_url", "src", "href"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            a.url = v.strip()
            break
    for k in ("content", "text", "source", "code", "html", "markdown"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            a.content = v
            break
    for k in ("mime", "mime_type", "content_type"):
        if raw.get(k):
            a.mime = str(raw[k])
            break
    for k in ("name", "filename", "file_name", "title"):
        if raw.get(k):
            a.name = str(raw[k])
            break
    if raw.get("size") is not None:
        try:
            a.size = int(raw["size"])
        except (TypeError, ValueError):
            pass
    # 宽高 / 时长 / 页数：名字各家不同，全部收进 meta，同时归一化一份
    meta = dict(raw.get("meta") or {})
    for k in ("width", "height", "duration", "pages", "page_count", "style", "aspect_ratio"):
        if raw.get(k) is not None:
            meta[k] = raw[k]
    if "output_image_hw" in raw:
        # 实测：名字骗人，是 [高, 宽]；且常为 [[h,w]]
        hw = raw["output_image_hw"]
        try:
            if isinstance(hw, list) and hw and isinstance(hw[0], list):
                hw = hw[0]
            meta["h"], meta["w"] = int(hw[0]), int(hw[1])
        except (TypeError, ValueError, IndexError):
            meta["output_image_hw"] = raw["output_image_hw"]
    a.meta = meta
    if not a.url and not a.content:
        return None
    return a


def coerce_assets(raw: Any, *, kind: str = "") -> list:
    """list 化 + 去重（GLM 生图一次给 4 张，且到达顺序是乱的）。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = [raw]
    out, seen = [], set()
    for it in items:
        a = coerce_asset(it, kind=kind)
        if not a:
            continue
        key = a.url or (a.content[:64] if a.content else "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(a)
    return out


# ── 一次调用的结果 ───────────────────────────────────────────────


@dataclass
class Reply:
    """一次调用的完整结果。"""

    text: str = ""
    thinking: str = ""
    assets: list = field(default_factory=list)          # list[Asset]
    references: list = field(default_factory=list)      # [{"url","title","snippet"}]
    queries: list = field(default_factory=list)         # 实际发出的搜索词
    model: str = ""                                     # 实际使用的模型（服务端可能静默替换）
    session_id: str = ""
    message_id: str = ""                                # 下轮续接用的指针
    provider: str = ""
    status: str = "answered"                            # REPLY_STATUS
    job: Optional["Job"] = None
    meta: dict = field(default_factory=dict)            # ⚠️ provider 原样 extra + phases 落这里
    raw: dict = field(default_factory=dict)
    elapsed: float = 0.0

    @property
    def needs_input(self) -> bool:
        """交互式能力在追问（slides / deep_research 实测会连续多轮追问）。"""
        return self.status == "needs_input"

    @property
    def ok(self) -> bool:
        return self.status in ("answered", "job") or bool(self.text or self.assets)

    def summary(self) -> dict:
        d = {
            "provider": self.provider,
            "status": self.status,
            "text_len": len(self.text),
            "think_len": len(self.thinking),
            "assets": [a.summary() if isinstance(a, Asset) else a for a in self.assets],
            "refs": len(self.references),
        }
        if self.model:
            d["model"] = self.model
        if self.meta.get("phases"):
            d["phases"] = sorted(self.meta["phases"].keys())
        return d


def coerce_reply(raw: Any, *, provider: str = "", session_id: str = "", kind: str = "") -> Reply:
    """把旧式 dict（``{"text","thinking","message_id","references","queries"}``）升级成 Reply。

    这是**向后兼容的关键**：provider 的 ``ask()`` 可以只多填几个 key，
    缺的一律按空处理，老调用点一行都不用改。
    """
    if isinstance(raw, Reply):
        if provider and not raw.provider:
            raw.provider = provider
        return raw
    if not isinstance(raw, dict):
        return Reply(text=str(raw or ""), provider=provider, session_id=session_id)

    r = Reply(
        text=raw.get("text") or "",
        thinking=raw.get("thinking") or "",
        references=list(raw.get("references") or []),
        queries=list(raw.get("queries") or []),
        model=str(raw.get("model") or ""),
        provider=provider or str(raw.get("provider") or ""),
        session_id=str(raw.get("session_id") or session_id or ""),
        message_id=str(raw.get("message_id") or raw.get("response_id") or ""),
        status=str(raw.get("status") or ""),
    )

    # 产物：provider 可以直接给 assets / images / files / videos
    r.assets = coerce_assets(
        raw.get("assets") or raw.get("images") or raw.get("videos") or raw.get("files"),
        kind=kind,
    )

    # meta：provider 的 extra 原样保留 —— 未知字段不能丢
    for k in ("extra", "meta", "phases", "raw_extra", "milestone"):
        if raw.get(k):
            r.meta[k] = raw[k]

    if not r.status:
        if raw.get("job") or raw.get("task_id"):
            r.status = "job"
        elif raw.get("needs_input") or raw.get("interactive"):
            r.status = "needs_input"
        elif r.text or r.assets or r.thinking:
            r.status = "answered"
        else:
            r.status = "empty"

    if raw.get("job"):
        r.job = coerce_job(raw["job"], provider=provider, session_id=r.session_id)
    elif raw.get("task_id"):
        r.job = coerce_job(
            {"id": raw["task_id"], "status": raw.get("task_status") or "running"},
            provider=provider,
            session_id=r.session_id,
            kind=kind,
        )
    return r


# ── 异步任务 ─────────────────────────────────────────────────────


@dataclass
class Job:
    """异步任务：提交 → 轮询 → 取产物。

    实测只有 Qwen ``t2v`` 是真异步（提交时 ``stream:false``，随后轮询
    ``GET /api/v2/task/status/<id>``，~145s）。生图 / slides 都是同步流。
    **服务端会显式给信号**（``stream:false``），不用猜。
    """

    id: str = ""
    provider: str = ""
    status: str = "pending"                     # JOB_STATES
    assets: list = field(default_factory=list)  # list[Asset]
    progress: float = 0.0
    note: str = ""                              # 失败原因 / 里程碑文字
    session_id: str = ""
    message_id: str = ""
    model: str = ""
    kind: str = ""                              # 这次做的是什么能力
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    polls: int = 0
    meta: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status in ("done", "failed", "cancelled")

    def summary(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "polls": self.polls,
            "note": self.note[:120],
            "assets": [a.summary() if isinstance(a, Asset) else a for a in self.assets],
        }


def coerce_job(raw: Any, *, provider: str = "", session_id: str = "", kind: str = "") -> Optional[Job]:
    if raw is None:
        return None
    if isinstance(raw, Job):
        return raw
    if not isinstance(raw, dict):
        return None

    j = Job(
        id=str(raw.get("id") or raw.get("task_id") or ""),
        provider=provider or str(raw.get("provider") or ""),
        status=str(raw.get("status") or raw.get("task_status") or "pending"),
        session_id=str(raw.get("session_id") or session_id or ""),
        message_id=str(raw.get("message_id") or ""),
        model=str(raw.get("model") or ""),
        kind=kind or str(raw.get("kind") or raw.get("chat_type") or ""),
        note=str(raw.get("note") or raw.get("message") or raw.get("error") or ""),
    )
    j.assets = coerce_assets(raw.get("assets") or raw.get("content") or raw.get("files"), kind=kind)
    try:
        j.progress = float(raw.get("progress") or 0.0)
    except (TypeError, ValueError):
        pass
    if raw.get("remaining_time"):
        j.meta["remaining_time"] = raw["remaining_time"]
    for k in ("extra", "meta", "raw_extra"):
        if raw.get(k):
            j.meta[k] = raw[k]

    j.status = str(j.status).lower()
    if j.status in ("success", "succeeded", "finished", "complete", "completed"):
        j.status = "done"
    elif j.status in ("error", "fail"):
        j.status = "failed"
    elif j.status in ("queue", "queued", "pending"):
        j.status = "pending"
    elif j.status in ("processing", "generating", "typing"):
        j.status = "running"
    if j.status not in JOB_STATES:
        j.status = "running" if j.assets else "pending"
    return j


def looks_like_job(raw: Any) -> bool:
    if isinstance(raw, Job):
        return True
    if not isinstance(raw, dict):
        return False
    # 用键存在性判断，不看真值 —— {"job": {}} 也是 job
    return "job" in raw or bool(raw.get("task_id"))


# ── 错误归类 ─────────────────────────────────────────────────────
# 六类提示词表。各 provider 可以在此基础上叠加自己的方言（见各自 classify()）。
# 注意：判定顺序重要 —— auth 优先于 quota，quota 优先于 transient。

AUTH_HINTS = (
    "unauthorized", "401", "403", "forbidden", "invalid token", "token expired",
    "token invalid", "not logged in", "未登录", "登录失效", "登录已过期", "请重新登录",
    "credentials", "cookie expired", "no cookie", "sign expired", "签名",
    # 实测补充：qwen 过期时回的是 "Token has expired, please log in again."
    # —— 原表只有 "token expired"（词序不同）匹配不到，导致凭据失效被归成 unknown，
    #    既给不出"重跑登录脚本"的提示，也让 auto-heal 永远不会触发。
    "has expired", "expired", "please log in", "log in again", "login again",
    "please login", "session expired", "凭据过期", "凭据已过期", "令牌过期",
    "需要登录", "请登录", "重新登陆",
)

QUOTA_HINTS = (
    "rate limit", "ratelimit", "too many requests", "429", "quota", "exceeded",
    "额度", "次数已达", "频率", "请求过于频繁", "insufficient", "balance",
    "credit", "积分不足", "余额",
)

UNSUPPORTED_HINTS = (
    "not supported", "unsupported", "not available", "unavailable", "unknown model",
    "model not found", "invalid model", "no permission", "not allowed",
    "权限不足", "暂不支持", "不支持", "无权限", "功能未开放", "该模型不可用",
    "capacity", "无可用", "degrade",
)

TRANSIENT_HINTS = (
    "timeout", "timed out", "connection", "reset by peer", "temporarily",
    "server error", "502", "503", "504", "500", "network", "broken pipe",
    "eof occurred", "超时", "网络", "稍后重试", "服务繁忙", "系统繁忙", "aborted",
)

POLICY_HINTS = (
    "content policy", "violat", "safety", "blocked", "sensitive", "inappropriate",
    "risk control", "换个话题", "内容安全", "违规", "敏感", "涉及敏感", "不予回答",
    "无法回答", "拒绝回答", "风控",
)


def classify_text(text: str, *, status: int = 0) -> ErrorKind:
    """从 HTTP 状态码 / 服务端文案推断错误类别。

    先看状态码（可信），再看文案（模糊）。都认不出就 UNKNOWN —— 这是诚实的选择，
    猜错会让调用方做错误的重试决策。
    """
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 429:
        return ErrorKind.QUOTA
    if status in (400, 404, 405, 422):
        # 400/404 也常是"能力不可用"，但更常见的是参数错 —— 归 UNSUPPORTED 更好降级
        return ErrorKind.UNSUPPORTED
    if status in (500, 502, 503, 504):
        return ErrorKind.TRANSIENT

    t = (text or "").lower()
    if not t:
        return ErrorKind.UNKNOWN
    # 顺序重要：越具体的越靠前。TRANSIENT 放在 UNSUPPORTED 之前，
    # 因为 "temporarily unavailable" 应归 transient 而不是 unsupported
    # —— 前者该重试，后者该换家，判错会导致相反的动作。
    for kind, hints in (
        (ErrorKind.AUTH, AUTH_HINTS),
        (ErrorKind.QUOTA, QUOTA_HINTS),
        (ErrorKind.TRANSIENT, TRANSIENT_HINTS),
        (ErrorKind.UNSUPPORTED, UNSUPPORTED_HINTS),
        (ErrorKind.CONTENT_POLICY, POLICY_HINTS),
    ):
        for h in hints:
            if h in t:
                return kind
    return ErrorKind.UNKNOWN


def kind_of(exc: BaseException | str, *, status: int = 0) -> ErrorKind:
    """异常/字符串 → ErrorKind。"""
    if isinstance(exc, WebAIError):
        return exc.kind
    return classify_text(str(exc), status=status)


def retry_hint(kind: ErrorKind | str) -> str:
    """给调用方的动作建议 —— 让"多后端自动降级"有统一依据。"""
    k = ErrorKind(kind) if not isinstance(kind, ErrorKind) else kind
    return {
        ErrorKind.AUTH: "重跑对应 *_login.py 刷新凭据",
        ErrorKind.QUOTA: "不重试；换一家 provider",
        ErrorKind.UNSUPPORTED: "更新探测缓存；降级到别家",
        ErrorKind.TRANSIENT: "退避后重试",
        ErrorKind.CONTENT_POLICY: "重试无用（同样的输入会被同样拒绝）",
        ErrorKind.UNKNOWN: "人工看一眼 raw",
    }[k]


def to_json(obj: Any) -> str:
    """安全序列化（Asset/Job/Reply 里可能混着不可序列化对象）。"""
    def _d(o):
        if isinstance(o, (Asset, Job, Reply)):
            return o.summary()
        if isinstance(o, set):
            return sorted(o)
        return str(o)

    return json.dumps(obj, ensure_ascii=False, default=_d, indent=2)
