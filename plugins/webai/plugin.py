"""WebAIPlugin — 把 ask_llm / copilot 两个工具注册进 ToolRegistry

为什么是"插件包"而不是单文件工具：
  两个工具共享同一套 provider 层（`lib/`），而 ext 的分发粒度 = 插件目录。
  库若放在插件目录之外，分享插件时**库不会被带上**，插件就不自包含
  （见 plugin_lib_discipline 的反面教训）。

依赖：插件 → 库（`lib/`）= 允许；插件 → 插件 = 0 边。本包零跨包依赖。

工具经 ON_INIT 动态注册（executor 签名 `async def(params: dict) -> str`）。
`on_unregister` 成对撤销，避免 reload 后残留。

> `vision` 已取消（2026-09）：它的"无状态单轮 + 看图"被 `ask_llm.files`
> 覆盖，多轮看图/读文件由 `copilot.files` 承接 —— 三个工具压成两个，
> 少一份要维护的能力筛选与错误提示。**注册名不清理会导致 reload 后残留**，
> 故 on_unregister 撤销的是 `_registered` 里实际注册过的名字（含旧的 vision）。
> v1.3.0：`image_paths` 参数泛化为 `files`（任意文件，按类型分诊 vision/attach
> 能力门）；旧名仍在代码层兼容接收，但 schema 只暴露 `files`。
> v1.3.1：stepfun 支持文件附件（attach，覆盖 = vision = qwen/glm/stepfun）——
> `upload()` 按 mime 分流 `/api/resource/image`（图）与 `/api/resource/document`
> （文件，2026-09-25 实测端到端读出 md）；`_attachments()` 的 resource oneof 键
> 按 kind 分流 `image`/`doc`，此前硬编码 `image` 会让 document rid 被服务端**静默无视**。
> v1.4.0：mimo 支持附件+看图（attach/vision，覆盖 = vision = qwen/glm/stepfun/mimo）——
> `upload()` 走 `genUploadInfo → PUT → resource/parse`（文档 parse 的 tokenUsage/id
> 是出站必需字段，缺失 → "服务器繁忙"）；`ask()` 注入 `multiMedias`、有文件自动关
> 搜索（镜像前端 `0===u.length`）；**文档 × fastchat(ultra) 不兼容**（载荷被收下但
> 模型读不到）→ 自动切非 fastchat + `mimo-v2.6-pro` 通道（wire 抓包 + 端到端实测）。
> v1.5.0：deepseek 支持附件+看图（attach/vision，覆盖 = vision = 五家齐）——
> `upload()` 返回**延迟引用**（真实上传在安全后端页面内完成：前端自算 PoW +
> `/file/upload_file`，出站 completion 自动带 `ref_file_ids`；伪造请求曾致封号）；
> `ask()` 经 `--files` 透传（CLI 解析与 rpc 载荷**两处都要有**，漏 rpc 曾致
> files:0 静默丢件）；文件 × 搜索互斥强制关搜并给 warnings（前端 v9 同款规则）；
> 实测 .md 逐字读出 + 紫底绿三角描述正确。
> v1.6.0：qwen 改走**浏览器安全后端**（`lib/qwen_safe_backend/`，playwright chromium +
> Xvfb :99 懒启动 daemon，只操作 DOM）——2026-10 chat.qwen.ai 对
> `/api/v2/chat/completions` 上了阿里 WAF，纯 HTTP 通道（requests / curl_cffi /
> 补 cookie / 拷 bx-ua）全被 `RGV587_ERROR` 拦，且 bx 挑战**只有有头渲染**跑得完
> （headless 永久卡在 `response-loading`）。代价是能力声明收紧：
> `capabilities={chat,search}`、`CAPABILITY_MAP` 为空（t2i/t2v/slides/deep_research
> 未实现，不许吹）；`qwen.verify()` 退化为 profile 自检。
"""

from __future__ import annotations

from typing import Any, cast

from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import Plugin, PluginConfig
from fp_core.tools import ToolRegistry
from fp_core.tools.core import OpenAISchema

from .tools import ask_llm, copilot

logger = get_logger()

# 注册顺序即工具列表里的呈现顺序
_TOOL_MODULES = (ask_llm, copilot)


class WebAIPlugin(Plugin):
    """网页版 AI 多后端：一次注册 ask_llm / copilot。"""

    name = "webai"
    version = "1.6.0"

    def __init__(self, config: PluginConfig | None = None):
        super().__init__(config)
        self._tool_registry: ToolRegistry | None = None
        self._registered: list[str] = []

    # ── 生命周期 ─────────────────────────────────────────────

    def on_register(self, lifecycle: LifecycleManager):
        lifecycle.register(
            LifecycleHook.ON_INIT,
            self._on_init,
            priority=50,
            name="webai_init",
        )

    def on_unregister(self):
        if self._tool_registry is not None:
            for tool_name in self._registered:
                self._tool_registry.unregister_tool(tool_name)
        self._registered.clear()
        self._tool_registry = None

    # ── 钩子实现 ─────────────────────────────────────────────

    async def _on_init(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        registry: ToolRegistry | None = kwargs.get("tool_registry")
        if registry is None:
            logger.warning("[webai] ON_INIT 未拿到 tool_registry，两个工具未注册")
            return ctx

        self._tool_registry = registry
        for mod in _TOOL_MODULES:
            registry.register_tool(
                mod.TOOL_NAME,
                cast(OpenAISchema, mod.DEFINITION),
                mod.execute,
            )
            self._registered.append(mod.TOOL_NAME)
        logger.info(f"[webai] 已注册工具: {', '.join(self._registered)}")
        return ctx
