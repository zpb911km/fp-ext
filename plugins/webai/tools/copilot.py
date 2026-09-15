"""
Copilot 插件 — 把网页版 AI 抽象成 FP 的协作者
====================================================

三个工具的分工（provider 层已统一在 public/plugins/webai/lib/，差别只在工具语义）：
    ask_llm  = provider.search()  无状态单轮，总是联网检索，返回答案 + 引用来源
    vision   = provider.ask()     无状态单轮，带图片附件（看图问答）
    copilot  = provider.ask()     有状态多轮（本插件）：记得上下文 / 可绑人设 / 可并行多专家

多后端：
    真正的调用细节（端点、鉴权、PoW、SSE 解析）全在 public/plugins/webai/lib/ 里。
    本插件只负责 会话持久化 + 角色注入 + 工具层。
    换/加后端 = 在 webai 加一个模块，这里一行都不用改。

续接机制（各后端不同，但都对 provider 封装好了）：
    qwen      chat_id + parent_id          = 上轮 response_id
    deepseek  chat_session_id + parent_message_id = 上轮 message_id
    stepfun   仅 chatSessionId（服务端自持上下文，无 parent 指针）
    glm       仅 conversation_id（**没有建会话 API**：new_session 返回占位符 "new"，
              ask() 换发空串、服务端由首条消息隐式建会话并在响应里回真实 id，
              故 ask 之后要把返回的 session_id 回写到 chat_id）

会话持久化在 ~/.local/share/fp/webai/copilot/sessions.json：
    多个命名会话 = 多个并行的"专家"，各自独立记忆。
"""

import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any


from .. import lib as _webai

TOOL_NAME = "copilot"
_STORE_FILE = os.path.expanduser("~/.local/share/fp/webai/copilot/sessions.json")
_LOCK = threading.RLock()
_HISTORY_CAP = 40  # 本地留存的对话条数上限（服务端才是真相）


def _providers() -> list:
    return list(_webai.names()) if _webai else ["qwen"]


def _get_provider(name: str):
    p = _webai.get(name)
    ok, why = p.available()
    if not ok:
        raise RuntimeError(why)
    return p


# ── 错误友好化 ──────────────────────────────────────────────────
# 裸异常（HTTP 401 / "Messages too frequent" / 内容策略拒绝 …）对用户没有意义，
# 归类后翻成"下一步该做什么"。归类走 webai.classify（provider 方言优先，认不出
# 回落 core 的通用中英文词表），建议走 core.retry_hint。
# 原则：宁可少说，不可说错 —— webai 缺失或归类失败时退化为原始文案。

def _login_cmd(*args: str) -> str:
    """给用户的"复制即用"命令 —— 统一入口，不必记四个 *_login.py 的名字。

    路径按**本文件位置**推算（不再假设它住在某个来源目录里）：
    tools/<this>.py → ../lib/login.py
    """
    p = Path(__file__).resolve().parent.parent / "lib" / "login.py"
    tail = (" " + " ".join(args)) if args else ""
    return f"python3 {p}{tail}"


def _login_advice(prov: str) -> str:
    """AUTH 场景的下一步动作。

    远端已收敛为**统一入口** public/plugins/webai/lib/login.py（四家各有 *_login.py，qwen 现在也有）。
    provider 层发现 AUTH 时会先自动试一次静默刷新（headless/不等人/硬超时），
    所以走到这里还报 AUTH，基本就是"需要人工登录"了。
    """
    return (f"已自动尝试过静默刷新；仍报此错说明需要人工登录。\n"
            f"  刷新它：{_login_cmd(prov)}\n"
            f"  查全部：{_login_cmd('--check')}")


def _friendly_error(prov: str, kind, detail: str = "") -> str:
    """ErrorKind → 可操作提示。

    重点三类（AUTH / QUOTA / CONTENT_POLICY）给固定话术 —— 它们比 core.retry_hint
    的通用建议更具体（AUTH 带上要重跑的脚本名）；其余直接用 retry_hint 的建议。
    """
    prov = prov or "该后端"
    detail = (detail or "").strip()
    try:
        advice = _webai.retry_hint(kind)
    except Exception:  # noqa: BLE001
        advice = ""
    EK = _webai.ErrorKind
    if kind == EK.AUTH:
        return f"{prov} 凭据失效，{_login_advice(prov)}"
    if kind == EK.QUOTA:
        return f"{prov} 额度/频率已用完，不重试（换一家 provider 或稍后再试）"
    if kind == EK.CONTENT_POLICY:
        return f"{prov} 内容策略拒绝，重试无用（同样的输入会被同样拒绝）"
    base = f"{prov} 调用失败：{detail}" if detail else f"{prov} 调用失败"
    return f"{base}（{advice}）" if advice else base


def _explain(prov: str, exc=None, *, text: str = "", raw=None) -> str:
    """异常/服务端文案 → 可操作提示（归类失败则原样返回）。"""
    fallback = (text or (str(exc) if exc is not None else "")).strip() or "未知错误"
    try:
        kind = _webai.classify(prov, exc, text=text, raw=raw)
    except Exception:  # noqa: BLE001
        return fallback
    return _friendly_error(prov, kind, fallback)


def _explain_unavailable(prov: str, why: str) -> str:
    """provider.available() 的原因文案 → 提示（凭据类原因同样给刷新动作）。"""
    why = (why or "").strip() or "不可用"
    try:
        kind = _webai.classify(prov, None, text=why)
    except Exception:  # noqa: BLE001
        return why
    if kind == _webai.ErrorKind.UNKNOWN:
        return f"{prov} 不可用：{why}"
    return _friendly_error(prov, kind, why)


def _kind_from_out(prov: str, out: dict) -> tuple:
    """从 ask() 返回体里**尽力**归类（空回答常是限流/审核，详情藏在 extra/error 里）。

    返回 ``(kind, detail)``；归不出返回 ``(None, "")`` —— 不猜。
    """
    if not isinstance(out, dict):
        return None, ""
    parts = [str(out[k]) for k in ("error", "raw_text") if isinstance(out.get(k), str)]
    extra = out.get("extra") if isinstance(out.get("extra"), dict) else None
    if extra:
        # 服务端详情常藏在 extra 里。**同时**当 text 传下去：
        # webai.classify 只有在 provider 方言认不出时才回落 core 词表，
        # 而回落后它只看 text —— 不放进 text，core 词表就看不到这段。
        try:
            parts.append(json.dumps(extra, ensure_ascii=False)[:200])
        except Exception:  # noqa: BLE001
            parts.append(str(extra)[:200])
    blob = " ".join(p.strip() for p in parts if p.strip())
    if not blob:
        return None, ""
    try:
        kind = _webai.classify(prov, None, text=blob, raw=extra)
    except Exception:  # noqa: BLE001
        return None, ""
    if kind == _webai.ErrorKind.UNKNOWN:
        return None, ""
    return kind, blob


# ── 会话持久化 ──────────────────────────────────────────────────

def _store_load() -> dict:
    try:
        with open(_STORE_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        pass
    return {"sessions": {}}


def _store_save(data: dict) -> None:
    os.makedirs(os.path.dirname(_STORE_FILE), exist_ok=True)
    tmp = f"{_STORE_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _STORE_FILE)


def _get_session_rec(name: str):
    return _store_load()["sessions"].get(name)


def _put_session_rec(name: str, rec: dict) -> None:
    with _LOCK:
        data = _store_load()
        data["sessions"][name] = rec
        _store_save(data)


# ── 核心：一轮对话 ──────────────────────────────────────────────

def copilot_say(
    message: str,
    session: str = "default",
    model: str = "",
    think: bool = False,
    web: bool = False,
    role: str = "",
    provider: str = "",
) -> dict:
    """向命名会话说一句话。会话不存在则自动新建。

    provider 决定用哪个后端（qwen / deepseek）。会话一旦建立即绑定 provider，
    中途改需用 action=new 重建。

    model 与 provider 一样是**会话级**属性：只在建会话时生效；已有会话中途指定
    不同模型会被明确拒绝（换模型 = 让新模型接上旧模型的上下文树，语义不可靠）。
    """
    with _LOCK:
        rec = _get_session_rec(session) or {}

    prov = (provider or rec.get("provider") or "qwen").lower()
    if prov not in _providers():
        return {"success": False, "error": f"未知 provider：{prov}（可选 {'/'.join(_providers())}）"}
    if provider and rec.get("provider") and provider.lower() != rec["provider"].lower():
        return {
            "success": False,
            "error": (
                f"会话「{session}」已绑定 provider={rec['provider']}，中途不能改。"
                f"要换请用 action=new session={session} provider={provider}（会重置会话）"
            ),
        }

    try:
        p = _get_provider(prov)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": _explain_unavailable(prov, str(e))}

    # 模型门禁：会话一旦存在（有 chat_id），换模型 = 新模型接上旧模型产出的上下文树。
    # 两个风险：① 不同模型对同一段上下文的解释可能不同；
    #           ② 续接指针（last_response_id）由旧模型产生，新模型未必认 →
    #              轻则上下文错位，重则接口直接报错。
    # 故只允许在**建会话时**指定；中途换一律拒绝并指路 action=new。
    # （静默忽略比报错更糟 —— 用户以为换了模型，实际没换。）
    want_model = (model or "").strip()
    rec_model = (rec.get("model") or "").strip()
    if rec.get("chat_id") and want_model and want_model != rec_model:
        return {
            "success": False,
            "session": session,
            "error": (
                f"会话「{session}」已绑定 model={rec_model or '(未记录)'}，中途换模型不安全"
                f"（新模型会接上旧模型留下的上下文树与续接指针）。"
                f"要换请用 action=new session={session} provider={prov} "
                f"model={want_model}（会重置会话、丢失上下文）"
            ),
        }

    model = want_model or rec_model or getattr(p, "default_model", "")

    # ── 新建 or 复用 ──
    if not rec.get("chat_id"):
        try:
            chat_id = p.new_session(model)
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"创建会话失败：{_explain(prov, e)}"}
        rec = {
            "chat_id": chat_id,
            "last_response_id": None,
            "model": model,
            "provider": prov,
            "role": role or "",
            "created": int(time.time()),
            "history": [],
            "turns": 0,
        }
    elif role and not rec.get("role"):
        rec["role"] = role

    # 角色设定：首轮注入（两家都不接受 role=system，故用文本前缀）
    payload = message
    if rec.get("role") and not rec.get("history"):
        payload = f"【角色设定】{rec['role']}\n\n{message}"

    try:
        out = p.ask(
            rec["chat_id"],
            payload,
            parent_message_id=rec.get("last_response_id"),
            model=rec.get("model", model),
            think=think,
            search=web,
        )
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": _explain(prov, e), "session": session}

    answer = (out.get("text") or "").strip()
    if not answer:
        # 空回答常见于限流 / 内容策略：错误详情往往藏在返回体里 ——
        # 能归类就给可操作提示，归不出就老实说"没拿到详情"（不猜）。
        kind, detail = _kind_from_out(prov, out)
        if kind is not None:
            return {"success": False, "session": session,
                    "error": _friendly_error(prov, kind, detail)}
        return {"success": False, "session": session,
                "error": "回答为空（可能被限流或内容审核；服务端未回传错误详情）"}

    # ── 续接指针：拿不到就必须**说出来** ──
    # 静默沿用旧 parent，等于让下一轮挂回上一轮的回答上 —— 会话树分叉，
    # 对方"记不起刚说过的话"。用户体感就是"聊着聊着断联了"，而且不报错。
    prev_parent = rec.get("last_response_id")
    new_parent = out.get("message_id")
    rec["last_response_id"] = new_parent or prev_parent
    warnings = []
    if not new_parent:
        warnings.append(
            "本轮没拿到续接指针（message_id），下一轮仍挂在上一轮回答上 —— "
            "若它「记不起」刚说的话，用 action=new 重建会话（会丢上下文）"
        )
    if out.get("truncated"):
        why = f"：{out['truncated_reason']}" if out.get("truncated_reason") else ""
        warnings.append(f"回答可能被截断（流被中断，不是正常收尾）{why}")

    # 有些后端（如 GLM）没有"建会话"API：会话由首条消息隐式创建，
    # 真实 id 由 ask() 带出，这里回写。其他后端不返回 session_id，无副作用。
    if out.get("session_id"):
        rec["chat_id"] = out["session_id"]
    rec["model"] = rec.get("model", model)
    rec["provider"] = rec.get("provider", prov)
    rec["updated"] = int(time.time())
    hist = rec.setdefault("history", [])
    hist.append({"role": "user", "content": message, "ts": int(time.time())})
    hist.append({"role": "assistant", "content": answer, "ts": int(time.time())})
    rec["history"] = hist[-_HISTORY_CAP:]
    rec["turns"] = len(rec["history"]) // 2
    _put_session_rec(session, rec)

    return {
        "success": True,
        "answer": answer,
        "session": session,
        "turns": rec["turns"],
        "model": rec["model"],
        "chat_id": rec["chat_id"],
        "provider": rec.get("provider", prov),
        "warnings": warnings,
    }


def copilot_new(session: str, role: str = "", model: str = "", provider: str = "qwen") -> dict:
    """新建（或重置）一个会话。

    **这里是 model 唯一生效的地方**（建会话即绑定模型 + provider + 人设）。
    重置会丢弃上下文 —— 这是"换模型"的代价，也是唯一安全的换法。
    """
    prov = (provider or "qwen").lower()
    if prov not in _providers():
        return {"success": False, "error": f"未知 provider：{prov}（可选 {'/'.join(_providers())}）"}
    try:
        p = _get_provider(prov)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": _explain_unavailable(prov, str(e))}

    model = model or getattr(p, "default_model", "")
    try:
        chat_id = p.new_session(model)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"创建会话失败：{_explain(prov, e)}"}

    rec = {
        "chat_id": chat_id, "last_response_id": None, "model": model, "provider": prov,
        "role": role or "", "created": int(time.time()), "updated": int(time.time()),
        "history": [], "turns": 0,
    }
    _put_session_rec(session, rec)
    return {"success": True, "session": session, "chat_id": chat_id,
            "role": role, "provider": prov, "model": model}


def copilot_list() -> list:
    data = _store_load()
    out = []
    for nm, rec in data["sessions"].items():
        out.append({
            "session": nm,
            "turns": rec.get("turns", len(rec.get("history", [])) // 2),
            "role": rec.get("role", ""),
            "model": rec.get("model", ""),
            "provider": rec.get("provider", "qwen"),
            "updated": rec.get("updated", rec.get("created", 0)),
        })
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out


def copilot_history(session: str) -> list:
    rec = _get_session_rec(session)
    return rec.get("history", []) if rec else []


def _fmt_ts(ts: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "?"


# ── 工具定义 ────────────────────────────────────────────────────

DEFINITION = {
    "type": "function",
    "function": {
        "name": "copilot",
        "description": (
            "与一个有状态的网页版AI协作者进行多轮对话。它有记忆——同一个 session 里后续追问会记得之前聊过什么。"
            "适合：复杂问题探讨、方案评审、结对思考、需要连续多轮的协作。"
            "边界：查资料的一次性检索请用 ask_llm（它总是联网搜索）；"
            "看懂一张图请用 vision（本工具目前不接受图片、文件附件）。"
            "action: say=发消息(默认) / new=新建或重置会话(可用 role 设定人设) / list=列出所有会话 / history=查看某会话记录。"
            "不同 session 名 = 不同专家的独立记忆，可并行多个。"
            f"provider 可选 {'/'.join(_providers())} —— 不同厂商的模型，换后端等于换脑子。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "要说的话（action=say 时必填）"},
                "session": {"type": "string", "description": "会话名，默认 default。换个名字就是开一个独立记忆的新协作者"},
                "action": {
                    "type": "string",
                    "enum": ["say", "new", "list", "history"],
                    "description": "say=对话(默认) / new=新建重置 / list=列出 / history=看记录",
                },
                "role": {"type": "string", "description": "角色设定（action=new 或首次对话时用），如'你是一位资深C++架构师，回答简洁犀利'"},
                "provider": {
                    "type": "string",
                    "enum": _providers(),
                    "description": "用哪个后端，默认 qwen。会话建立后即绑定，中途改需 action=new 重建",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "模型，留空用后端默认。**只在 action=new（或首次 say 隐式建会话）时生效**；"
                        "已有会话中途换模型会被拒绝（换模型 = 新模型接旧上下文树，不安全），"
                        "请改用 action=new 重建会话"
                    ),
                },
                "think": {"type": "boolean", "description": "深度思考模式，更慢但更准，默认 false"},
                "web": {"type": "boolean", "description": "是否允许联网搜索，默认 false"},
            },
            "required": [],
        },
    },
}


# ── 执行器 ──────────────────────────────────────────────────────

async def execute(params: dict[str, Any]) -> str:
    action = (params.get("action") or "say").strip().lower()
    session = (params.get("session") or "default").strip()
    loop = asyncio.get_running_loop()

    if action == "list":
        rows = await loop.run_in_executor(None, copilot_list)
        if not rows:
            return "（暂无 copilot 会话）"
        lines = ["🧠 copilot 会话："]
        for r in rows:
            tag = f" · {r['role'][:30]}" if r["role"] else ""
            lines.append(
                f"  • {r['session']} [{r.get('provider', 'qwen')}] — {r['turns']} 轮 · "
                f"{r['model']} · {_fmt_ts(r['updated'])}{tag}"
            )
        return "\n".join(lines)

    if action == "history":
        hist = await loop.run_in_executor(None, copilot_history, session)
        if not hist:
            return f"（会话 {session} 无记录）"
        lines = [f"📜 会话 {session}（最近 {len(hist)} 条）："]
        for h in hist[-12:]:
            who = "我" if h["role"] == "user" else "AI"
            lines.append(f"  [{who}] {h['content'][:160]}")
        return "\n".join(lines)

    if action == "new":
        res = await loop.run_in_executor(
            None, copilot_new, session, params.get("role", ""),
            params.get("model", ""), params.get("provider", "qwen"))
        if not res.get("success"):
            return f"❌ {res.get('error')}"
        tail = f"，人设：{res['role'][:50]}" if res.get("role") else ""
        return (f"✅ 已新建会话「{session}」"
                f"（{res.get('provider', 'qwen')} · {res.get('model', '')}）{tail}")

    # ── say ──
    message = (params.get("message") or "").strip()
    if not message:
        return "错误：action=say 需要 message 参数"

    res = await loop.run_in_executor(
        None, copilot_say, message, session,
        params.get("model", ""),
        bool(params.get("think", False)),
        bool(params.get("web", False)),
        params.get("role", ""),
        params.get("provider", ""),
    )
    if not res.get("success"):
        return f"❌ copilot 失败：{res.get('error')}"

    warn = "".join(f"\n⚠️ {w}" for w in (res.get("warnings") or []))
    return (
        f"🧠 [copilot:{res['session']} · {res.get('provider', 'qwen')} · "
        f"第{res['turns']}轮 · {res['model']}]\n{res['answer']}{warn}"
    )
