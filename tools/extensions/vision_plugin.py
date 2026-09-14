"""
Vision —— 让网页版 AI 看图
================================================

多后端：上传与理解的差异全在 public/webai/，本插件只做参数校验与结果整形。

    qwen      OSS STS 上传，把文件挂到 message.files
    deepseek  POST /api/v0/file/upload_file（需 PoW），file_id 放进 ref_file_ids

两者都实测支持**真正的图像理解**（不只是 OCR）：
给一张零文字的几何图，能正确答出"3 个红色圆形 + 1 个蓝色正方形"。
"""

__fp__ = {
    "name": "vision",
    "version": "2.0.0",
    "description": "图像理解（上传图片给网页版AI识别，多后端）",
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

_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")


def load_webai():
    """把 public/webai 包按路径加载进来（插件加载器不往 sys.path 加目录）"""
    if "webai" in sys.modules:
        return sys.modules["webai"]
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


def _vision_providers() -> list:
    """只列出声明了 vision 能力的后端"""
    if not _webai:
        return ["qwen"]
    out = []
    for n in _webai.names():
        try:
            if "vision" in getattr(_webai.get(n), "capabilities", set()):
                out.append(n)
        except Exception:  # noqa: BLE001
            continue
    return out or ["qwen"]


def vision(
    image_path: str,
    query: str = "描述这张图片",
    provider: str = "qwen",
    think: bool = False,
) -> str:
    """上传图片并提问，返回文本回答。

    参数：
        image_path: 图像文件路径
        query:      询问文本
        provider:   后端（默认 qwen）
        think:      深度思考模式（默认关闭；关闭时更快）
    """
    if not _webai:
        return "错误：webai 包缺失（<数据目录>/public/webai/）"

    name = (provider or "qwen").lower()
    try:
        p = _webai.get(name)
    except Exception as e:  # noqa: BLE001
        return f"错误：未知 provider「{name}」：{e}"

    if "vision" not in getattr(p, "capabilities", set()):
        return f"错误：provider「{name}」不支持 vision 能力"

    ok, why = p.available()
    if not ok:
        return f"错误：{why}"

    if not os.path.isfile(image_path):
        return f"错误：文件不存在 - {image_path}"
    if not image_path.lower().endswith(_IMG_EXT):
        return f"错误：不支持的图片格式 - {image_path}"

    try:
        ref = p.upload(image_path)
    except Exception as e:  # noqa: BLE001
        return f"文件上传失败: {e}"

    try:
        sid = p.new_session()
        r = p.ask(sid, query, files=[ref], think=think)
    except Exception as e:  # noqa: BLE001
        return f"识别失败: {e}"

    text = (r.get("text") or "").strip()
    return text or "(识别无返回)"


# ── 插件定义 ────────────────────────────────────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "vision",
        "description": (
            "将图像上传到网页版AI模型识别，返回文字描述或回答关于图像的问题。"
            "支持截图/照片/图表。"
        ),
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
                },
                "provider": {
                    "type": "string",
                    "enum": _vision_providers(),
                    "description": "用哪个后端，默认 qwen。不同厂商识别能力有差异，可换着试",
                },
            },
            "required": ["image_path"],
        },
    },
}


# ── 执行器 ──────────────────────────────────────────────────────

async def execute(params: dict[str, Any]) -> str:
    image_path = (params.get("image_path") or "").strip()
    if not image_path:
        return "错误：需要 image_path 参数"
    query = (params.get("query") or "描述这张图片").strip()
    provider = (params.get("provider") or "qwen").strip()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, vision, image_path, query, provider)
