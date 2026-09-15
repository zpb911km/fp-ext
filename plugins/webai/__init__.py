"""webai — 网页版 AI 多后端插件包

一个插件包，注册三个工具：

    ask_llm  — 联网检索并作答（无状态单轮，返回答案 + 引用来源）
    vision   — 看图问答（无状态单轮，带图片附件）
    copilot  — 多轮协作者（有状态；命名会话即"专家"，各有独立记忆）

内聚库 ``.lib`` 是本包内的子包（四家 provider 层 / 登录器 / 离线测试），
**随插件目录一起被 ext 分发** —— 单资产、自包含、无跨包依赖。

库模块不注册钩子/工具，无生命周期语义（见 plugin_lib_discipline）。
"""

__fp__ = {
    "schema": 1,
    "name": "webai",
    "type": "plugin",
    "version": "1.1.0",
    "description": "网页版 AI 多后端：ask_llm(联网检索) / vision(看图) / copilot(多轮协作)",
    "author": "zpb",
    "license": "GPL-3.0",
}

from .plugin import WebAIPlugin

__all__ = ["WebAIPlugin"]
