"""
webai — 网页版 AI provider 层
=================================

把"某个网页版 AI 究竟怎么调"收拢在这里，工具层（copilot / ask_llm）
只声明自己要什么能力，不关心后端的端点与鉴权细节。

加一个新后端 = 在本目录加一个模块，然后在下面 _MODULES 注册一行。

设计原则（详见 DESIGN.md §5 / core.py 顶部）
-------------------------------------------
**契约（稳定，少）**：``Reply`` / ``Job`` / ``Asset`` / ``ErrorKind``  —— 见 core.py
**事实（易变，多）**：能力名 / 模型名 / 参数 / 限额 / 权限 → 由 provider 透传与运行时探测

目标不是"兼容无限"，而是"**不挡路**"。

provider 契约
-------------
**必须实现（老契约，一个字都没改）**：:

    name           str        提供方标识，如 "qwen"
    capabilities   set[str]   {"chat","search","vision"} 的子集（hint，非承诺）
    default_model  str
    available() -> (bool, str)
    new_session(model="") -> str
    ask(session_id, text, *, parent_message_id=None, model="", think=False,
        search=False, files=None) -> dict
        # 老键: text / thinking / message_id / references / queries
        # 新键（可选，缺了不影响）: assets / images / videos / files /
        #          model / extra / phases / status / task_id / needs_input
    upload(path) -> dict                            # {"ref","name","size"}
    search(query, *, think=False, model="") -> dict

**可选实现（新契约，缺了自动降级）**：:

    CAPABILITY_MAP = {"t2i": {"chat_type": "t2i"}, ...}   # 能力名 → 各家方言参数
    models() -> list[dict]         # [{"id","name","capabilities":set,"ctx":int}]
    probe(capability) -> dict      # {"supported":bool,"via":..,"note":..}
    poll(job) -> dict              # 轮询异步任务
    classify(text="", status=0, raw=None) -> str(ErrorKind)

三条来自实测的硬约束（DESIGN.md §7）
-----------------------------------
1. **未知的东西不能丢** —— 已三次踩到"用已知 phase 白名单筛流，产物被静默丢弃"。
   provider 应把原样 ``extra`` 塞进返回 dict 的 ``extra`` / ``phases`` 键。
2. **产物不一定有 URL** —— ``web_dev`` 返回内联 HTML。填 ``assets`` 时用
   ``{"kind":..,"content":..}``。
3. **产物 URL 会过期** —— 立刻 ``Asset.save()`` 落盘，别只存链接。

工具侧用法
---------
::

    webai = load_webai()            # 见各插件里的加载器（加载器不往 sys.path 加目录）

    # 老式（仍然完全可用）
    p = webai.get("deepseek")
    r = p.ask(sid, "你好");  print(r["text"])

    # 新式（富接口，返回 core.Reply / core.Job）
    r = webai.ask("deepseek", sid, "你好")            # -> Reply
    for a in r.assets: a.save("/tmp/out")             # 产物落盘
    if r.needs_input: ...                             # 交互式能力在追问
    if r.status == "job": j = webai.poll("qwen", r.job)
"""

import functools
import importlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import core
from .core import Asset, ErrorKind, Job, Reply, WebAIError

__all__ = [
    "core", "Asset", "Job", "Reply", "ErrorKind", "WebAIError",
    "get", "raw", "names", "available", "capabilities", "has", "models", "probe",
    "first_available", "classify", "new_session", "ask", "poll", "upload",
    "capability_map", "resolve_capability",
    "search", "asset_dir", "DEFAULT_ORDER", "next_available",
]

_MODULES = {
    "qwen": "qwen",
    "deepseek": "deepseek",
    "stepfun": "stepfun",
    "glm": "glm",
    "mimo": "mimo",
}

_cache = {}

# 会打到服务端、可能因凭据过期而失败的方法 —— 只给这几个挂自愈。
# 命名刻意收窄：available()/models()/probe() 等"查状态"的方法**不**触发登录。
_HEAL_METHODS = ("ask", "search", "upload", "poll")

# 自愈的开关：设 FP_WEBAI_NO_AUTOLOGIN=1 可完全关掉（不再后台拉起 login.py）
_ENV_OFF = "FP_WEBAI_NO_AUTOLOGIN"


def _kind_of(name: str, exc: BaseException) -> ErrorKind:
    """归不出类别时返回 UNKNOWN（=不重试、不刷新）—— 猜错比不猜更糟。"""
    try:
        return classify(name, exc)
    except Exception:  # noqa: BLE001
        return ErrorKind.UNKNOWN


def _is_auth_error(name: str, exc: BaseException) -> bool:
    return _kind_of(name, exc) == ErrorKind.AUTH


# ── 抖动重试（TRANSIENT）─────────────────────────────────────────
# 只给 TRANSIENT 开这个口子，因为各类错误的**动作是相反的**：
#   AUTH       → 换凭据（重发没用）
#   QUOTA      → 停手（重发更糟）
#   UNSUPPORTED→ 换一家（重发没意义）
#   TRANSIENT  → 重发（这才会好）
# 以前这里只认 AUTH，网络抖一下就直接抛给用户 —— 用户体感就是"聊到一半断了"。
_RETRY_BACKOFF = (0.8, 2.5)          # 第 1 次 / 第 2 次重试前的退避秒数
_ENV_NO_RETRY = "FP_WEBAI_NO_RETRY"


def _retry_backoff() -> tuple:
    """退避表；`FP_WEBAI_NO_RETRY=1` 可关掉（调试 / 测试时用）。"""
    return () if os.environ.get(_ENV_NO_RETRY) else _RETRY_BACKOFF


_login_procs: dict = {}


def _spawn_login(name: str) -> None:
    """凭据失败的自愈：**非阻塞**拉起对应的 login.py（后台自愈契约）。

    契约（与 `_wrap_heal` 配合）：
      * 失败**立刻**把原始错误返回给系统 —— 调用方不等登录；
      * login 在后台独立跑（detached，stdin 断开，日志进 autologin.log）；
      * 后台登录**成功也不回填**本次结果（不补返回、不重试）——
        下一次调用凭据就绪了自然恢复。
    每个 provider 至多一个 login 进程在跑；本函数吞掉一切异常（绝不覆盖原始错误）。
    """
    if os.environ.get(_ENV_OFF):
        return
    p = _login_procs.get(name)
    if p is not None and p.poll() is None:
        return                                  # 已有一个在跑，不重复拉起
    try:
        login = Path(__file__).resolve().parent / "login.py"
        logf = Path.home() / ".local" / "share" / "fp" / "webai" / "autologin.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, str(login), name],
            stdout=logf.open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        _login_procs[name] = proc
        print(f"[webai] {name} 凭据失效 → 后台自愈: login.py {name} (pid={proc.pid})",
              file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _wrap_heal(name, fn):
    """两条通道，互不串门：

    * **AUTH** → 立即把失败交回系统（不阻塞、不重试），同时**非阻塞**拉起
      `login.py <name>` 后台自愈 —— 后台成功也**不回填**本次结果。
      契约来自用户指定："失败时先返回系统；尝试自动愈合：非阻塞地调用对应的
      login；成功了也不返回。"
    * **TRANSIENT** → 退避重试 `_RETRY_BACKOFF` 次（网络断流 / 5xx / 读超时）。

    ⚠️ 重试**不是幂等**的：provider 若不能在重发时复用同一份请求体
    （如 qwen 的流式发消息），它应当**自己**在内部重试，别指望这一层
    —— 否则可能往会话里插入第二条相同的用户消息。这里只兜"请求根本没落地"的抖动。
    """

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        attempt = 0
        while True:
            try:
                return fn(*a, **kw)
            except Exception as e:  # noqa: BLE001
                kind = _kind_of(name, e)
                if kind == ErrorKind.AUTH:
                    _spawn_login(name)
                    raise                     # 立即返回失败，登录在后台跑
                backoff = _retry_backoff()
                # `no_retry`：provider 已经自己重发过（或用重发也治不了，如 WAF 挑战）。
                # 不认这个标记就会形成"内层重发 × 外层重发"的乘法，白打一串请求。
                if (kind == ErrorKind.TRANSIENT and not getattr(e, "no_retry", False)
                        and attempt < len(backoff)):
                    wait = backoff[attempt]
                    attempt += 1
                    # 留痕：看不见的重试会让"偶发失败"永远查不出来
                    print(f"[webai] {name} 抖动重试 {attempt}/{len(backoff)}"
                          f"（{wait}s 后）：{type(e).__name__}: {e}", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise

    return wrapper


# ── 负载均衡：**仅**限制同一后端"新建会话"的频率 ──────────────────
# 其它调用（ask / search / available…）一概不管；不做跨后端分发。
# 间隔按后端风险各自配置，env `FP_WEBAI_NS_INTERVAL_<NAME>` 秒级覆盖，0=不限；
# `FP_WEBAI_NO_BALANCE=1` 整体关闭（调试/测试用）。
# deepseek 维持 60（2026-09-25 用户确认）：解除的是它独有的"每日配额/强制间隔"
# 惩罚性限制（已删），此常规建会话间隔不属区别对待，保留原值。

_NS_INTERVAL = {"deepseek": 60.0, "qwen": 30.0, "glm": 30.0, "stepfun": 30.0}
_ENV_NS_OFF = "FP_WEBAI_NO_BALANCE"
_ns_lock = threading.Lock()
_ns_last: dict = {}


def _ns_interval(name: str) -> float:
    if os.environ.get(_ENV_NS_OFF):
        return 0.0
    env = os.environ.get(f"FP_WEBAI_NS_INTERVAL_{name.upper()}")
    if env is not None:
        try:
            return max(0.0, float(env))
        except ValueError:
            pass
    return float(_NS_INTERVAL.get(name, 30.0))


def _wrap_new_session(name, fn):
    """同一后端两次 new_session 之间至少间隔 _ns_interval(name) 秒。

    * 时间戳在**调用前**登记：失败的建会话尝试同样计数（限的是"尝试频率"，
      这才是风控在意的量）；
    * 等待期在锁外 sleep，多线程安全；等待可见（stderr 留痕）。
    """

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        interval = _ns_interval(name)
        if interval > 0:
            while True:
                with _ns_lock:
                    now = time.time()
                    wait = _ns_last.get(name, 0.0) + interval - now
                    if wait <= 0:
                        _ns_last[name] = now
                        break
                print(f"[webai] 负载均衡: {name} 新建会话限速，等待 {wait:.0f}s",
                      file=sys.stderr)
                time.sleep(min(wait, interval))
        return fn(*a, **kw)

    return wrapper


class _HealingProvider:
    """provider 模块的透明包装，两件事都挂在**唯一入口** get() 上：

    * **AUTH** → 立即失败返回 + 后台非阻塞 login.py 自愈（成功不回填）
    * **new_session** → 负载均衡：同一后端新建会话限速（其它方法一概不管）

    其余属性**原样转发**，所以它仍然"长得像"那个模块
    （capabilities / models / available / 常量都能取到）。

    为什么包在 get()：这是所有调用方的**唯一入口** ——
    一处生效 = 三个工具 + webai.ask/search 全被覆盖，不必改四个 provider。
    """

    __slots__ = ("_name", "_mod")

    def __init__(self, name, mod):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_mod", mod)

    def __getattr__(self, k):
        v = getattr(object.__getattribute__(self, "_mod"), k)
        name = object.__getattribute__(self, "_name")
        if k == "new_session" and callable(v):
            return _wrap_new_session(name, v)
        if k in _HEAL_METHODS and callable(v):
            return _wrap_heal(name, v)
        return v

    def __setattr__(self, k, v):
        setattr(object.__getattribute__(self, "_mod"), k, v)

    def __repr__(self):
        return f"<webai provider '{object.__getattribute__(self, '_name')}'>"


def raw(name: str):
    """未包装的原始 provider 模块（调试 / 测试 / 需要绕开自愈时用）。"""
    name = (name or "").lower()
    if name not in _MODULES:
        raise KeyError(f"未知 provider: {name}（可用: {', '.join(_MODULES)}）")
    return importlib.import_module(f".{_MODULES[name]}", __package__)


def get(name: str):
    """取得 provider（带凭据自愈的包装）。

    延迟 import —— 某个后端依赖缺失时不影响其他后端。
    """
    name = (name or "").lower()
    if name in _cache:
        return _cache[name]
    if name not in _MODULES:
        raise KeyError(f"未知 provider: {name}（可用: {', '.join(_MODULES)}）")
    mod = importlib.import_module(f".{_MODULES[name]}", __package__)
    _cache[name] = _HealingProvider(name, mod)
    return _cache[name]


def names() -> list:
    return list(_MODULES)


# ── 能力：provider 的声明只是 hint，不是承诺 ──────────────────────


def capabilities(name: str) -> set:
    """provider 声明的能力集合（**归一化后**的通用名）。

    两项来源合并，避免"声明与实际实现脱节"：
      1. 模块级 ``capabilities`` 集合（老声明，如 {"chat","search","vision"}）
      2. ``CAPABILITY_MAP`` 的键（实测能力，如 qwen 的 t2i/t2v/slides…）

    每一项都过 ``core.canonical()`` 折叠方言名 —— 否则 qwen 的 ``t2i``
    与 glm 的 ``image_gen`` 会被当成两个能力，跨家路由就断了。

    ⚠️ 这仍然是 **hint**：实测 Qwen 同一家族内能力都不同（``qwen3.7-max``
    反而不支持 vision/search）。真正的答案是"试一次"。
    """
    try:
        mod = get(name)
    except Exception:  # noqa: BLE001
        return set()
    cap = getattr(mod, "capabilities", None)
    if callable(cap):
        try:
            cap = cap()
        except Exception:  # noqa: BLE001
            cap = ()
    out = {core.canonical(c) for c in (cap or ()) if c}
    for k in (getattr(mod, "CAPABILITY_MAP", None) or {}):
        out.add(core.canonical(k))
    return out


def has(name: str, capability: str) -> bool:
    """是否声明支持某能力（方言名会自动折叠，``t2i`` == ``image_gen``）。"""
    return core.canonical(capability) in capabilities(name)


def capability_map(name: str) -> dict:
    """能力名 → 该家的请求方言（如 Qwen 的 ``{"chat_type": "t2i"}``）。

    ⚠️ 调用时要用**这家自己的名字**取值：``capability_map("qwen")["t2i"]``。
    传通用名（``image_gen``）时若查不到，会退化为在所有键里做一次别名匹配。
    """
    try:
        return dict(getattr(get(name), "CAPABILITY_MAP", {}) or {})
    except Exception:  # noqa: BLE001
        return {}


def resolve_capability(name: str, capability: str) -> tuple:
    """把通用/方言能力名解析成这家自己的键 + 方言参数。

    返回 ``(native_key, dialect)``；认不出返回 ``("", {})``。
    """
    cm = capability_map(name)
    if capability in cm:
        return capability, dict(cm[capability] or {})
    want = core.canonical(capability)
    for k, v in cm.items():
        if core.canonical(k) == want:
            return k, dict(v or {})
    return "", {}


def models(name: str) -> list:
    """枚举可选模型。没实现的 provider 返回 []（不是错误）—— 别假装有。"""
    try:
        mod = get(name)
    except Exception:  # noqa: BLE001
        return []
    fn = getattr(mod, "models", None)
    if not callable(fn):
        return []
    try:
        return list(fn() or [])
    except Exception:  # noqa: BLE001
        return []


def probe(name: str, capability: str = "") -> dict:
    """尽力而为地探测能力。**允许返回 ``{"supported": None}``** —— 不知道就说不知道。

    实测只有 Qwen / GLM 有可用的配置接口，DeepSeek 只能靠"试一次看报什么错"。
    所以调用方不该依赖 probe 的结果做关键决策。
    """
    try:
        mod = get(name)
    except Exception as e:  # noqa: BLE001
        return {"supported": None, "note": f"加载失败: {type(e).__name__}"}
    fn = getattr(mod, "probe", None)
    if callable(fn):
        try:
            out = fn(capability)
            if isinstance(out, dict):
                out.setdefault("provider", name)
                return out
            return {"supported": bool(out), "provider": name}
        except Exception as e:  # noqa: BLE001
            return {"supported": None, "provider": name, "note": f"{type(e).__name__}: {e}"[:120]}
    return {
        "supported": capability in capabilities(name) or None,
        "provider": name,
        "note": "该 provider 未实现 probe()",
    }


# 默认优先级：mimo 首选 → deepseek → glm → stepfun → qwen（2026-09-25 用户指定）。
# deepseek 走安全后端（真 Chrome），封号问题已彻底修复（2026-09-25 确认），
# 节流与限次已全部解除（后端 pacing 移除、_NS_INTERVAL 置 0）。
# + 负载均衡限速，观察窗口由用户此次排序指令解除，升入池内第二位。
# 凭据未就绪的会被跳过 —— 避免"默认后端 token 一过期就整体不可用"。
DEFAULT_ORDER = ("mimo", "deepseek", "glm", "stepfun", "qwen")


def first_available(capability: str = "", order=None):
    """按优先级挑第一个可用 provider。

    capability 非空时只考虑声明了该能力的后端，且会跳过凭据未就绪的。
    返回 (name, tried) —— name 为空表示全都不可用，tried 是逐家原因（给人看）。
    """
    cands = [n for n in (order or DEFAULT_ORDER) if n in _MODULES]
    cands += [n for n in _MODULES if n not in cands]
    tried = []
    for n in cands:
        try:
            mod = get(n)
        except Exception as e:  # noqa: BLE001
            tried.append(f"{n}:加载失败({type(e).__name__})")
            continue
        if capability and not has(n, capability):
            tried.append(f"{n}:无 {capability} 能力")
            continue
        try:
            ok, why = mod.available()
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"{type(e).__name__}: {e}"[:80]
        if ok:
            return n, tried
        tried.append(f"{n}:{why}")
    return "", tried


# 轮询游标：留空自动时让 search 调用在四家之间轮流，避免压力连续集中同一家。
# （first_available 是固定优先级；next_available 从上次命中的后一个开始挑。）
_rotate_last: str | None = None


def next_available(capability: str = "", order=None) -> tuple[str, list]:
    """轮询版 first_available：从上次成功命中的后一家开始按序挑可用者。

    首次调用等价于 first_available；之后每家轮一次。全部不可用时返回
    ("", tried)，不推进游标（下次仍从原位置继续）。
    """
    global _rotate_last
    base = [n for n in (order or DEFAULT_ORDER) if n in _MODULES]
    base += [n for n in _MODULES if n not in base]
    if _rotate_last in base:
        i = base.index(_rotate_last)
        base = base[i + 1:] + base[:i + 1]
    name, tried = first_available(capability, order=base)
    if name:
        _rotate_last = name
    return name, tried


def available() -> dict:
    """{provider: (是否可用, 原因)} —— 用于给用户提示"该配哪个凭据" """
    out = {}
    for n in _MODULES:
        try:
            out[n] = get(n).available()
        except Exception as e:  # noqa: BLE001
            out[n] = (False, f"{type(e).__name__}: {e}")
    return out


# ── 错误归类：客户端只做归类，不建模限额 ─────────────────────────


def classify(name: str, exc=None, *, text: str = "", status: int = 0, raw=None) -> ErrorKind:
    """把异常/文案归到六类之一。provider 有方言就用自己的，否则用 core 的通用词表。"""
    if isinstance(exc, WebAIError):
        if not name or exc.provider == name:
            return exc.kind
    blob = text or (str(exc) if exc is not None else "")
    if name:
        try:
            fn = getattr(get(name), "classify", None)
            if callable(fn):
                k = fn(text=blob, status=status, raw=raw, exc=exc)
                if k:
                    return ErrorKind(k) if not isinstance(k, ErrorKind) else k
        except Exception:  # noqa: BLE001
            pass
    return core.classify_text(blob, status=status)


def retry_hint(kind) -> str:
    return core.retry_hint(kind)


# ── 富接口：包装老式 dict 接口，零破坏 ───────────────────────────


def new_session(name: str, *, model: str = "", capability: str = "") -> str:
    """新建会话。

    ⚠️ **能力是会话级属性**（实测）：Qwen 在 ``POST /api/v2/chats/new`` 时就定了
    ``chat_type``，不是每条消息能切。所以"切能力"要新建会话。
    provider 支持 capability 就会透传（从 CAPABILITY_MAP 取方言）。
    """
    mod = get(name)
    if capability:
        native, params = resolve_capability(name, capability)
        fn = getattr(mod, "new_session", None)
        if fn is not None:
            try:
                return fn(model=model, capability=capability, **params)
            except TypeError:
                # 老 provider 不支持 capability → 退回普通会话，别炸
                pass
    return mod.new_session(model=model)


def ask(name: str, session_id: str, text: str, *, kind: str = "", **kw) -> Reply:
    """富接口调用。返回 ``core.Reply``（内部可能挂着一个 ``core.Job``）。

    老式 ``provider.ask() -> dict`` 完全不受影响 —— 这里只是把它包一层。
    """
    mod = get(name)
    t0 = time.time()
    try:
        raw = mod.ask(session_id, text, **kw)
    except WebAIError:
        raise
    except Exception as e:  # noqa: BLE001
        raise WebAIError(classify(name, e), str(e)[:400], provider=name, raw=e) from e
    r = core.coerce_reply(raw, provider=name, session_id=session_id, kind=kind)
    r.elapsed = time.time() - t0
    return r


def poll(name: str, job, *, kind: str = "") -> Job:
    """轮询异步任务。把 provider 返回的增量合并进原 Job（保留 id / created / polls）。"""
    if isinstance(job, dict):
        job = core.coerce_job(job, provider=name, kind=kind)
    if not isinstance(job, Job):
        raise WebAIError(ErrorKind.UNKNOWN, "poll() 需要 Job", provider=name)

    mod = get(name)
    fn = getattr(mod, "poll", None)
    if not callable(fn):
        raise WebAIError(ErrorKind.UNSUPPORTED, f"{name} 未实现 poll()", provider=name)
    raw = fn(job)
    new = core.coerce_job(raw, provider=name, session_id=job.session_id, kind=job.kind or kind) or Job()
    new.id = new.id or job.id
    new.kind = new.kind or job.kind
    new.session_id = job.session_id or new.session_id
    new.message_id = new.message_id or job.message_id
    new.model = job.model or new.model
    new.created = job.created
    new.updated = time.time()
    new.polls = job.polls + 1
    if not new.assets:
        new.assets = job.assets            # 别把上一轮拿到的产物弄丢
    new.meta = {**job.meta, **new.meta}
    return new


def upload(name: str, path: str) -> dict:
    return get(name).upload(path)


def search(name: str, query: str, **kw) -> Reply:
    """便捷搜索 —— 老式 dict 也顺手升成 Reply。"""
    mod = get(name)
    raw = mod.search(query, **kw)
    r = core.coerce_reply(raw, provider=name, kind="search")
    if not r.queries:
        r.queries = [query]
    return r


# ── 产物落盘目录 ─────────────────────────────────────────────────


def asset_dir(name: str = "", when: float | None = None) -> str:
    """产物落盘目录（``<data>/webai/assets/<provider>/<YYYY-MM-DD>``）。

    实测产物 URL 带 JWT（含 resource_user_id，会过期），**取到即落盘**是唯一安全做法。
    """
    try:
        from fp_core.platform_utils import get_data_dir  # type: ignore

        base = Path(get_data_dir())
    except Exception:  # noqa: BLE001
        base = Path.home() / ".local" / "share" / "fp"
    d = time.strftime("%Y-%m-%d", time.localtime(when or time.time()))
    p = base / "webai" / "assets" / (name or "_shared") / d
    os.makedirs(p, exist_ok=True)
    return str(p)


# ── 供插件使用的包加载器 ────────────────────────────────────────
# fp 的插件加载器用 spec_from_file_location 逐个加载文件，不会把插件目录加进 sys.path，
# 所以插件无法直接 `import webai`。各插件复制下面这个小函数即可（约 15 行，刻意不抽公共模块，
# 避免"为了复用而再引入一层加载难题"）。

BOOTSTRAP_SOURCE = '''
def load_webai():
    """把 public/plugins/webai/lib 包按路径加载进来（插件加载器不往 sys.path 加目录）"""
    import importlib.util as _iu
    import sys as _sys
    from pathlib import Path as _P

    # ⚠️ 别缓存！fp 的 /reload 不会清 sys.modules["webai"]，
    #    复用缓存会导致新增 provider 后重载仍拿到旧 _MODULES。
    for _k in [k for k in list(_sys.modules) if k == "webai" or k.startswith("webai.")]:
        _sys.modules.pop(_k, None)
    try:
        from fp_core.platform_utils import get_data_dir
        data = _P(get_data_dir())
    except Exception:
        data = _P.home() / ".local" / "share" / "fp"
    pkg = data / "public" / "webai"
    if not (pkg / "__init__.py").exists():
        return None
    spec = _iu.spec_from_file_location(
        "webai", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    mod = _iu.module_from_spec(spec)
    _sys.modules["webai"] = mod
    spec.loader.exec_module(mod)
    return mod
'''
