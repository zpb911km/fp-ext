"""
Ask LLM 插件 — AI 联网搜索与总结
==========================================
通过 chat.qwen.ai 的联网搜索能力，让 AI 搜索互联网并阅读总结，
返回结构化回答 + 引用来源列表。

相比传统的 web_search（仅返回搜索结果列表），
Ask LLM 更进一步——让 AI 读完原文再总结。

依赖：
    1. 将 Qwen cookie 写入 ~/.qwen_cookie：
       echo 'cna=xxx; aui=xxx; token=xxx; ...' > ~/.qwen_cookie
    2. 或设置环境变量 QWEN_COOKIE

用法（由 Agent 自动调用）：
    Ask LLM(keywords="zpb911km github")
"""

__fp__ = {
    "name": "ask_llm",
    "version": "1.0.0",
    "description": "AI 联网搜索与总结（Qwen 联网）",
    "author": "zpb",
    "license": "GPL-3.0",
    "type": "tools",
}



import asyncio

# ── qwen模块 ──────────────────────────────────────────────
import json
import os
import time
import uuid
from typing import Any

import requests

# ── 路径 ──────────────────────────────────────────────────────

API_BASE = "https://chat.qwen.ai"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


# ── Cookie 读取 ────────────────────────────────────────────────

_COOKIE_FILE = os.path.expanduser("~/.qwen_cookie")


def _load_cookie() -> str:
    """从 ~/.qwen_cookie 或环境变量读取 cookie"""
    cookie = os.environ.get("QWEN_COOKIE", "")
    if cookie:
        return cookie
    try:
        with open(_COOKIE_FILE) as f:
            cookie = f.read().strip()
            if cookie:
                return cookie
    except (FileNotFoundError, PermissionError):
        pass
    return ""


# ── SSE 解析 ──────────────────────────────────────────────────


def _parse_sse(sse_text: str) -> dict:
    """
    解析 SSE 原始文本，提取回答文本 + 搜索信息。
    返回：
        {"content": "...", "search_info": {...} | None}
    """
    result = {
        "content": "",
        "search_info": None,
    }

    # 搜索追踪状态
    search_queries = []
    search_args_latest = ""
    search_results = []
    in_search = False

    for line in sse_text.split("\n"):
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            continue

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        choices = data.get("choices", [])
        if not choices:
            continue

        delta = choices[0].get("delta", {})
        phase = delta.get("phase", "")

        # ── web_search phase ──
        if phase == "web_search":
            in_search = True
            status = delta.get("status", "")
            fn_call = delta.get("function_call", {})

            if status == "typing" and fn_call:
                args = fn_call.get("arguments", "")
                if args:
                    search_args_latest = args
                    try:
                        parsed = json.loads(search_args_latest)
                        qs = parsed.get("queries", [])
                        if qs:
                            search_queries = qs
                    except (json.JSONDecodeError, TypeError):
                        pass

            elif status == "finished":
                extra = delta.get("extra", {})
                if isinstance(extra, dict):
                    tool_result = extra.get("tool_result", {})
                    if isinstance(tool_result, dict):
                        docs = tool_result.get("docs", [])
                        for doc in docs:
                            search_results.append({
                                "url": doc.get("url", ""),
                                "title": doc.get("title", ""),
                                "snippet": doc.get("snippet", ""),
                            })
            continue

        # ── answer phase ──
        if phase == "answer":
            content = delta.get("content", "")
            if content:
                result["content"] += content

    # 组装 search_info
    if in_search:
        # 去重
        seen_urls = set()
        unique_results = []
        for doc in search_results:
            url = doc.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_results.append(doc)

        result["search_info"] = {
            "queries": search_queries,
            "results": unique_results,
            "result_count": len(unique_results),
        }

    return result


# ── 核心函数 ────────────────────────────────────────────────────


def _make_headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": API_BASE,
        "Referer": f"{API_BASE}/",
        "Version": "0.2.63",
        "source": "web",
        "X-Request-Id": str(uuid.uuid4()),
        "Timezone": time.strftime("%a %b %d %Y %H:%M:%S GMT+0800"),
    }


def ask_llm(keywords: str, cookie: str = "") -> dict:
    """
    联网搜索：使用 Qwen 搜索互联网并返回结构化答案。
    纯 requests 实现，无需浏览器。

    参数：
        keywords: 搜索关键词（空格分隔），例如 "zpb911km github"
        cookie:   Qwen cookie 字符串（可选，默认从 ~/.qwen_cookie 或环境变量读取）

    返回：
        {
            "answer":       str,    # Qwen 总结后的回答文本
            "references":   list,   # [{url, title, snippet}, ...] 去重
            "queries":      list,   # 实际执行的搜索查询语句
            "raw_content":  str,    # 原始回答（同 answer）
            "success":      bool,
            "error":        str,
        }
    """
    cookie = cookie or _load_cookie()
    if not cookie:
        return {
            "answer": "",
            "references": [],
            "queries": [],
            "raw_content": "",
            "success": False,
            "error": "未找到 Qwen cookie。请写入 ~/.qwen_cookie 或设置环境变量 QWEN_COOKIE",
        }

    # ── 1. 创建 session ──
    sess = requests.Session()
    for item in cookie.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            sess.cookies.set(k, v)

    # ── 2. 创建会话 ──
    try:
        r = sess.post(
            f"{API_BASE}/api/v2/chats/new",
            headers=_make_headers(),
            json={"model": "qwen3.7-plus"},
            timeout=15,
        )
        r.raise_for_status()
        chat_id = r.json()["data"]["id"]
    except Exception as e:
        return error_result(f"创建会话失败: {e}")

    # ── 3. 构建搜索消息 ──
    query_text = f"{keywords}"

    msg = {
        "stream": True,
        "version": "2.1",
        "incremental_output": True,
        "chat_id": chat_id,
        "chat_mode": "normal",
        "model": "qwen3.7-plus",
        "parent_id": None,
        "messages": [
            {
                "fid": str(uuid.uuid4()),
                "parentId": None,
                "childrenIds": [str(uuid.uuid4())],
                "role": "user",
                "content": query_text,
                "user_action": "chat",
                "files": [],
                "timestamp": int(time.time()),
                "models": ["qwen3.7-plus"],
                "chat_type": "t2t",
                "feature_config": {
                    "thinking_enabled": True,
                    "output_schema": "phase",
                    "research_mode": "normal",
                    "auto_thinking": True,
                    "thinking_mode": "Auto",
                    "thinking_format": "summary",
                    "auto_search": True,
                },
                "extra": {"meta": {"subChatType": "t2t"}},
                "sub_chat_type": "t2t",
            }
        ],
        "timestamp": int(time.time()),
    }

    # ── 4. 流式请求（等流自然结束）──
    try:
        r2 = sess.post(
            f"{API_BASE}/api/v2/chat/completions?chat_id={chat_id}",
            headers={
                **_make_headers(),
                "Accept": "application/json, text/event-stream",
                "X-Accel-Buffering": "no",
            },
            json=msg,
            stream=True,
        )
        r2.raise_for_status()

        # 逐行读取 SSE 流，检测到 [DONE] 后主动断开
        sse_text = ""
        for line in r2.iter_lines(decode_unicode=True):
            if line is None:
                continue
            sse_text += line + "\n"
            if line.strip() == "data: [DONE]":
                break

        # 检测 API 返回的错误（非 SSE 格式的错误响应）
        first_line = sse_text.split("\n")[0] if sse_text else ""
        if first_line and '"ret"' in first_line and '"FAIL' in first_line:
            try:
                err_data = json.loads(first_line[6:] if first_line.startswith("data: ") else first_line)
                ret = err_data.get("ret", ["未知错误"])
                return error_result(f"API 拒绝请求: {ret[1] if len(ret) > 1 else ret[0]}")
            except json.JSONDecodeError:
                pass

    except Exception as e:
        return error_result(f"联网搜索请求失败: {e}")

    # ── 5. 解析 ──
    parsed = _parse_sse(sse_text)
    search_info = parsed.get("search_info") or {}
    references = [
        {"url": d["url"], "title": d["title"], "snippet": d["snippet"]} for d in search_info.get("results", [])
    ]

    return {
        "answer": parsed.get("content", ""),
        "references": references,
        "queries": search_info.get("queries", []),
        "raw_content": parsed.get("content", ""),
        "success": True,
        "error": "",
    }


def error_result(msg: str) -> dict:
    return {
        "answer": "",
        "references": [],
        "queries": [],
        "raw_content": "",
        "success": False,
        "error": msg,
    }


# ── 插件定义 ──────────────────────────────────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "ask_llm",
        "description": (
            "向当前最强大的在线llm模型网页版发出单轮次独立询问."
            "功能: 1. 全网络信息检索总结; 2. 思考复杂问题; 3. 获得示范"
            "注意: 此接口不保留状态,llm没有上下文,可以多次调用,但无法多轮对话"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "发问,或者想对其说的话",
                },
            },
            "required": ["keywords"],
        },
    },
}


# ── 执行器 ────────────────────────────────────────────────────


async def execute(params: dict[str, Any]) -> str:
    keywords = params.get("keywords", "").strip()
    if not keywords:
        return "错误：需要 keywords 参数"

    # smart_web_search 是同步函数，在线程池中运行
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, ask_llm, keywords)

    if not result.get("success"):
        return f"联网搜索失败: {result.get('error', '未知错误')}"

    answer = result.get("answer", "")
    references = result.get("references", [])
    queries = result.get("queries", [])

    # 格式化输出
    lines = []
    lines.append(f"🔍 搜索结果（查询: {' | '.join(queries)}）:\n")
    lines.append(answer)
    if references:
        lines.append(f"\n\n📎 引用来源 ({len(references)} 条):")
        for i, ref in enumerate(references[:10], 1):
            lines.append(f"\n{i}. {ref['title']}")
            lines.append(f"   {ref['url']}")
    return "\n".join(lines)
