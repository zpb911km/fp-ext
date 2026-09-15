"""WebAIPlugin — 把 ask_llm / vision / copilot 三个工具注册进 ToolRegistry

为什么是"插件包"而不是三个单文件工具：
  三个工具共享同一套 provider 层（`lib/`），而 ext 的分发粒度 = 插件目录。
  库若放在插件目录之外，分享插件时**库不会被带上**，插件就不自包含
  （见 plugin_lib_discipline 的反面教训）。

依赖：插件 → 库（`lib/`）= 允许；插件 → 插件 = 0 边。本包零跨包依赖。

工具经 ON_INIT 动态注册（executor 签名 `async def(params: dict) -> str`）。
`on_unregister` 成对撤销，避免 reload 后残留。
"""

from __future__ import annotations

from typing import Any, cast

from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import Plugin, PluginConfig
from fp_core.tools import ToolRegistry
from fp_core.tools.core import OpenAISchema

from .tools import ask_llm, copilot, vision

logger = get_logger()

# 注册顺序即工具列表里的呈现顺序
_TOOL_MODULES = (ask_llm, vision, copilot)


class WebAIPlugin(Plugin):
    """网页版 AI 多后端：一次注册 ask_llm / vision / copilot。"""

    name = "webai"
    version = "1.0.0"

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
            logger.warning("[webai] ON_INIT 未拿到 tool_registry，三个工具未注册")
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
