"""
Ask LLM —— 向网页版 AI 提问（无状态单轮）
================================================

与 copilot 的分工：
    ask_llm  = 无状态单轮：问一句、答一句，用完即弃（查资料）
    copilot  = 有状态多轮：同一会话记得上下文（探讨 / 评审 / 结对）

多后端：真正的调用细节（端点 / 鉴权 / 搜索解析）在 public/webai/。
本插件只做：能力选择 + 结果格式化。

provider 可选（取决于 webai 里注册了哪些）：
    qwen      结构化引用来自 web_search phase
    deepseek  结构化引用来自 response/search_results
两者都会返回 {url, title, snippet} 形态的引用，故输出格式统一。
"""

__fp__ = {
    "name": "ask_llm",
    "version": "2.0.0",
    "description": "向网页版AI模型提问（无状态单轮，多后端）",
    "author": "zpb",
    "license": "GPL-3.0",
    "type": "tools",
}

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any


def load_webai():
    """把 public/webai 包按路径加载进来（插件加载器不往 sys.path 加目录）"""
    # ⚠️ 不复用 sys.modules 缓存：fp 的 /reload 只重载 fp_core.* 模块，webai 不在其中，
    #    会存活下来 → 新增 provider 后重载仍拿到旧 _MODULES（表现为"未知 provider"）。
    #    这里每次强制从磁盘重新执行（3 个小文件，开销可忽略）。
    for _k in [k for k in list(sys.modules) if k == "webai" or k.startswith("webai.")]:
        sys.modules.pop(_k, None)
    try:
        from fp_core.platform_utils import get_data_dir

        data = str(get_data_dir())
    except Exception:
        data = os.path.expanduser("~/.local/share/fp")
    pkg = Path(data) / "public" / "webai"
    if not (pkg / "__init__.py").exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "webai", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webai"] = mod
    spec.loader.exec_module(mod)
    return mod


_webai = load_webai()


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


def _friendly_error(name: str, exc: Any, model: str = "") -> str:
    """把裸露异常归类成可操作的提示（webai.classify + core.retry_hint）。"""
    kind = _webai.classify(name, exc)
    EK = _webai.core.ErrorKind

    if kind == EK.AUTH:
        # 凭据失效 → 不重试，指引刷新
        return f"凭据失效，请重跑 {name}_login.py 刷新（{exc}）"

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
        return _err("webai 包缺失：<数据目录>/public/webai/")

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

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "ask_llm",
        "description": (
            "向当前最强大的在线llm模型网页版发出单轮次独立询问."
            "功能: 1. 全网络信息检索总结; 2. 思考复杂问题; 3. 获得示范"
            "注意: 此接口不保留状态,llm没有上下文,可以多次调用,但无法多轮对话"
            "需要多轮探讨/记住上下文请用 copilot。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "发问,或者想对其说的话",
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
