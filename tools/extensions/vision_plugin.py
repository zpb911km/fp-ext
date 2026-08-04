"""
Qwen Vision 插件 — 图像识别
=============================
上传图像到 Qwen OSS，通过视觉模型识别，返回文本描述。

依赖：
    pip install oss2 requests

Cookie 配置（任选其一）：
    1. 环境变量 QWEN_COOKIE="cna=xxx; aui=xxx; token=xxx; ..."
    2. 文件 ~/.qwen_cookie：echo 'cna=xxx; aui=xxx; token=xxx; ...' > ~/.qwen_cookie

用法（由 Agent 自动调用）：
    vision(image_path="/path/to/image.png", query="描述这张图片")

实现说明：
    - 纯 requests + oss2 实现，无需 Playwright / 浏览器
    - 参考 smart_web_search_plugin.py 的流式 SSE 解析模式
"""

__fp__ = {
    "name": "vision",
    "version": "1.0.0",
    "description": "Qwen 视觉图像识别",
    "author": "zpb",
    "license": "GPL-3.0",
    "type": "tools",
}



import asyncio
import json
import mimetypes
import os
import time
import uuid
from typing import Any

import oss2
import requests

# ── 常量 ───────────────────────────────────────────────────────────

API_BASE = "https://chat.qwen.ai"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

_DEFAULT_COOKIE = os.environ.get("QWEN_COOKIE", "")
_COOKIE_FILE = os.path.expanduser("~/.qwen_cookie")


# ── Cookie ─────────────────────────────────────────────────────────


def _load_cookie() -> str:
    """从环境变量或 ~/.qwen_cookie 读取 cookie"""
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


# ── 请求头 ─────────────────────────────────────────────────────────


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


# ── 文件上传（OSS）────────────────────────────────────────────────


def _get_sts_token(sess: requests.Session, filename: str, filesize: int, filetype: str) -> dict:
    """获取 OSS STS 凭证 + file_id/file_url"""
    r = sess.post(
        f"{API_BASE}/api/v1/files/getstsToken",
        headers=_make_headers(),
        json={"filename": filename, "filesize": str(filesize), "filetype": filetype},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _upload_file(cookie_str: str, file_path: str) -> dict:
    """
    上传文件到 Qwen OSS，返回文件元数据。

    纯 requests + oss2 实现，无需浏览器。
    """
    with open(file_path, "rb") as f:
        data = f.read()
    filename = os.path.basename(file_path)
    filetype, _ = mimetypes.guess_type(filename)
    filetype = filetype or "application/octet-stream"

    ft = filetype.split("/")[0] if "/" in filetype else "file"
    file_class = {"image": "vision", "video": "video", "audio": "audio"}.get(ft, "file")
    file_show_type = {"image": "image", "video": "video", "audio": "audio"}.get(ft, "file")

    sess = requests.Session()
    for item in cookie_str.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            sess.cookies.set(k, v)

    sts = _get_sts_token(sess, filename, len(data), filetype)

    auth = oss2.StsAuth(
        sts["access_key_id"],
        sts["access_key_secret"],
        sts["security_token"],
    )
    bucket = oss2.Bucket(
        auth,
        f"https://{sts['endpoint']}",
        sts["bucketname"],
    )
    result = bucket.put_object(sts["file_path"], data)
    if result.status != 200:
        raise OSError(f"OSS 上传失败: HTTP {result.status}")

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


# ── SSE 解析 ──────────────────────────────────────────────────────


def _extract_text_from_sse(sse_text: str) -> str:
    """从 SSE 原始文本中提取最终回答内容"""
    parts = []
    for line in sse_text.split("\n"):
        if not line.startswith("data: "):
            continue
        ds = line[6:].strip()
        if ds == "[DONE]" or '"response.created"' in ds:
            continue
        try:
            data = json.loads(ds)
            for choice in data.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("phase") == "answer":
                    c = delta.get("content", "")
                    if c:
                        parts.append(c)
        except (json.JSONDecodeError, KeyError):
            continue
    return "".join(parts)


# ═══════════════════════════════════════════════════════════════════
# 核心函数（同步，纯 requests）
# ═══════════════════════════════════════════════════════════════════


def vision(image_path: str, query: str = "描述这张图片", model: str = "qwen3.7-plus", cookie: str = "") -> str:
    """
    同步函数：上传图像 → Qwen 视觉识别 → 返回文本描述。

    纯 requests + oss2 实现，无需 Playwright / 浏览器。
    参考 smart_web_search 的流式 SSE 写法。

    参数：
        image_path:  图像文件路径
        query:       询问文本，默认"描述这张图片"
        model:       模型名，默认 qwen3.7-plus
        cookie:      cookie 字符串（可选，默认从环境变量 QWEN_COOKIE
                     或 ~/.qwen_cookie 读取）

    返回：
        str: Qwen 的文本回复
    """
    cookie = cookie or _load_cookie()
    if not cookie:
        return "错误：未找到 Qwen cookie。请将 cookie 写入 ~/.qwen_cookie 文件，或设置环境变量 QWEN_COOKIE"

    if not os.path.isfile(image_path):
        return f"错误：文件不存在 - {image_path}"

    if not image_path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")):
        return f"错误：不支持的文件格式 - {image_path}"

    # ── 1. 上传文件到 OSS ──
    try:
        file_info = _upload_file(cookie, image_path)
    except Exception as e:
        return f"文件上传失败: {e}"

    # ── 2. 创建 session ──
    sess = requests.Session()
    for item in cookie.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            sess.cookies.set(k, v)

    # ── 3. 创建聊天会话 ──
    try:
        r = sess.post(
            f"{API_BASE}/api/v2/chats/new",
            headers=_make_headers(),
            json={"model": model},
            timeout=15,
        )
        r.raise_for_status()
        chat_id = r.json()["data"]["id"]
    except Exception as e:
        return f"创建会话失败: {e}"

    # ── 4. 构建消息 ──
    fid = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    user_msg = {
        "fid": fid,
        "parentId": None,
        "childrenIds": [cid],
        "role": "user",
        "content": query,
        "user_action": "chat",
        "files": [file_info],
        "timestamp": int(time.time()),
        "models": [model],
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

    body = {
        "stream": True,
        "version": "2.1",
        "incremental_output": True,
        "chat_id": chat_id,
        "chat_mode": "normal",
        "model": model,
        "parent_id": None,
        "messages": [user_msg],
        "timestamp": int(time.time()),
    }

    # ── 5. 流式请求，逐行读取 SSE ──
    #     与 smart_web_search_plugin.py 完全一致的 pattern
    try:
        r2 = sess.post(
            f"{API_BASE}/api/v2/chat/completions?chat_id={chat_id}",
            headers={
                **_make_headers(),
                "Accept": "application/json, text/event-stream",
                "X-Accel-Buffering": "no",
            },
            json=body,
            stream=True,
            timeout=120,
        )
        r2.raise_for_status()

        sse_text = ""
        for line in r2.iter_lines(decode_unicode=True):
            if line is None:
                continue
            sse_text += line + "\n"
            if line.strip() == "data: [DONE]":
                break

    except Exception as e:
        return f"API 请求失败: {e}"

    # ── 6. 解析 SSE ──
    reply = _extract_text_from_sse(sse_text)
    return reply or "(识别无返回)"


# ═══════════════════════════════════════════════════════════════════
# 插件定义
# ═══════════════════════════════════════════════════════════════════

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "vision",
        "description": "将图像上传到视觉模型识别，返回文字描述。支持截图/照片/图表。",
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "图像文件的绝对路径（如 /tmp/screenshot.png）",
                },
                "query": {
                    "type": "string",
                    "description": "对图像的询问文本，如: 描述这张图片 / 这张图表显示什么趋势",
                    "default": "描述这张图片",
                },
            },
            "required": ["image_path"],
        },
    },
}


# ═══════════════════════════════════════════════════════════════════
# 执行器（异步适配器，供插件系统调用）
# ═══════════════════════════════════════════════════════════════════


async def execute(params: dict[str, Any]) -> str:
    """
    执行图像识别（异步适配器）

    Args:
        params: {"image_path": "...", "query": "..."}

    Returns:
        识别结果文本
    """
    image_path = params.get("image_path", "")
    query = params.get("query", "描述这张图片")

    if not image_path:
        return "错误：需要 image_path 参数"

    # vision 是同步函数，在线程池中运行以避免阻塞事件循环
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, vision, image_path, query)
    return result
