"""
Ask LLM —— 向网页版 AI 提问（无状态单轮）
================================================

三个工具的分工（provider 层已统一在 public/plugins/webai/lib/，差别只在工具语义）：
    ask_llm  = provider.search()  无状态单轮，**总是联网检索**，返回答案 + 引用来源
    vision   = provider.ask()     无状态单轮，带图片附件（看图问答）
    copilot  = provider.ask()     有状态多轮，同一会话记得上下文（探讨 / 评审 / 结对）

注意 ask_llm 与 vision 走的是**不同的 provider 方法**（search vs ask），
所以不是"vision 是 ask_llm 的特例"——两者服务不同意图，故各自保留。

多后端：真正的调用细节（端点 / 鉴权 / 搜索解析）在 public/plugins/webai/lib/。
本插件只做：能力选择 + 结果格式化。

provider 可选（取决于 webai 里注册了哪些）：
    qwen      结构化引用来自 web_search phase
    deepseek  结构化引用来自 response/search_results
两者都会返回 {url, title, snippet} 形态的引用，故输出格式统一。
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any


from .. import lib as _webai

TOOL_NAME = "ask_llm"
def _search_providers() -> list:
    """只列出声明了 search 能力的后端"""
    if not _webai:
        return ["qwen"]
    out = []
    for n in _webai.names():
        try:
            if "search" in getattr(_webai.get(n), "capabilities", set()):
                out.append(n)
        except Exception:  # noqa: BLE001
            continue
    return out or ["qwen"]


def _err(msg: str) -> dict:
    return {"answer": "", "references": [], "queries": [],
            "raw_content": "", "success": False, "error": msg}


def _available_models(name: str) -> list:
    """列出某 provider 的可用模型 id。探测失败返回 []（不硬编名字）。"""
    try:
        ms = _webai.models(name) or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for m in ms:
        mid = (m.get("id") or m.get("name")) if isinstance(m, dict) else m
        if mid:
            out.append(str(mid))
    return out


def _login_cmd(*args: str) -> str:
    """给用户的"复制即用"命令 —— 统一入口，不必记四个 *_login.py 的名字。

    路径按**本文件位置**推算（不再假设它住在某个来源目录里）：
    tools/<this>.py → ../lib/login.py
    """
    p = Path(__file__).resolve().parent.parent / "lib" / "login.py"
    tail = (" " + " ".join(args)) if args else ""
    return f"python3 {p}{tail}"


def _friendly_error(name: str, exc: Any, model: str = "") -> str:
    """把裸露异常归类成可操作的提示（webai.classify + core.retry_hint）。"""
    kind = _webai.classify(name, exc)
    EK = _webai.core.ErrorKind

    if kind == EK.AUTH:
        # 凭据失效 → 不重试，给一条复制即用的刷新命令
        return (f"凭据失效：{exc}\n"
                f"  已自动尝试过静默刷新；仍报此错说明需要人工登录。\n"
                f"  刷新它：{_login_cmd(name)}\n"
                f"  查全部：{_login_cmd('--check')}")

    if kind == EK.QUOTA:
        # 额度/频率用完 → 直说，别让调用方白重试
        return f"{name} 额度/频率已用完，请勿重试，换一家 provider（{exc}）"

    if kind == EK.UNSUPPORTED:
        msg = f"{name} 不支持该请求"
        if model:
            msg += f"（模型「{model}」不可用）"
            avail = _available_models(name)
            if avail:  # models() 返回空就不列，别硬编
                shown = ", ".join(avail[:20])
                if len(avail) > 20:
                    shown += f" …(共 {len(avail)} 个)"
                msg += f"；可用模型: {shown}"
        return f"{msg}（{exc}）"

    # TRANSIENT / CONTENT_POLICY / UNKNOWN → 给通用动作建议
    return f"{name} 调用失败（{kind.value}）：{exc}；建议：{_webai.core.retry_hint(kind)}"


def ask_llm(keywords: str, provider: str = "", think: bool = False,
            model: str = "") -> dict:
    """单轮联网提问。

    provider 留空则按 webai 的优先级自动挑（deepseek → qwen → glm → stepfun），
    跳过凭据未就绪的后端。
    model 留空则用 provider 默认模型；不合法/不可用时会附上该家可用模型清单。
    返回 {answer, references, queries, raw_content, success, error}
    """
    if not _webai:
        return _err("webai 包缺失：<数据目录>/public/plugins/webai/lib/")

    name = (provider or "").strip().lower()
    if name in ("", "auto"):
        name, tried = _webai.first_available("search")
        if not name:
            return _err("没有可用的联网后端 —— " + "；".join(tried))

    try:
        p = _webai.get(name)
    except Exception as e:  # noqa: BLE001
        return _err(f"未知 provider「{name}」：{e}")

    if "search" not in getattr(p, "capabilities", set()):
        return _err(f"provider「{name}」不支持 search 能力")

    ok, why = p.available()
    if not ok:
        return _err(why)

    try:
        r = p.search(keywords, think=think, model=model)
    except Exception as e:  # noqa: BLE001
        return _err(_friendly_error(name, e, model))

    answer = (r.get("text") or "").strip()
    if not answer:
        return _err("回答为空（可能被限流、内容审核，或该后端未返回搜索结果）")

    return {
        "answer": answer,
        "references": r.get("references") or [],
        "queries": r.get("queries") or [],
        "raw_content": answer,
        "success": True,
        "error": "",
        "provider": name,
    }


# ── 插件定义 ────────────────────────────────────────────────────

DEFINITION = {
    "type": "function",
    "function": {
        "name": "ask_llm",
        "description": (
            "联网检索并作答（无状态单轮，多后端）。"
            "总是开启网页搜索，返回答案 + 结构化引用来源，"
            "适合查资料、要出处的实时性问题、多厂商交叉验证（同一问题换 provider 对比）。"
            "不保留上下文：每次调用都是一次全新对话。"
            "边界：要多轮探讨 / 让它记住上下文 → copilot；"
            "要理解一张图（截图 / 图表） → vision。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "要检索并回答的问题（本工具总是联网搜索，写成问题陈述比写成指令更有效）",
                },
                "provider": {
                    "type": "string",
                    "enum": _search_providers(),
                    "description": "用哪个后端，留空=自动（优先后端不可用时自动换）。不同厂商检索覆盖不同，可换着问。",
                },
                "think": {
                    "type": "boolean",
                    "description": "深度思考模式，更慢但更准，默认 false",
                },
                "model": {
                    "type": "string",
                    "description": "指定模型（留空=用 provider 默认）。不同厂商可用模型不同，传了不可用的模型会返回该家可用清单。可先用 webai.models() 查询。",
                },
            },
            "required": ["keywords"],
        },
    },
}


# ── 执行器 ──────────────────────────────────────────────────────

async def execute(params: dict[str, Any]) -> str:
    keywords = (params.get("keywords") or "").strip()
    if not keywords:
        return "错误：需要 keywords 参数"

    provider = (params.get("provider") or "").strip()
    think = bool(params.get("think", False))
    model = (params.get("model") or "").strip()

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, ask_llm, keywords, provider, think, model)

    if not result.get("success"):
        return f"联网搜索失败: {result.get('error', '未知错误')}"

    answer = result.get("answer", "")
    references = result.get("references", [])
    queries = result.get("queries", [])
    used = result.get("provider", provider)

    lines = []
    head = f"🔍 搜索结果 [{used or '自动'}]"
    if queries:
        head += f"（查询: {' | '.join(queries)}）"
    lines.append(head + ":\n")
    lines.append(answer)

    if references:
        lines.append(f"\n\n📎 引用来源 ({len(references)} 条):")
        for i, ref in enumerate(references[:10], 1):
            lines.append(f"\n{i}. {ref.get('title', '')}")
            lines.append(f"   {ref.get('url', '')}")
    return "\n".join(lines)
