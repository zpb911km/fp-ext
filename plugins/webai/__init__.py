"""webai — 网页版 AI 多后端插件包

一个插件包，注册两个工具：

    ask_llm  — 联网检索并作答（无状态单轮，返回答案 + 引用来源；可带附件）
    copilot  — 多轮协作者（有状态；命名会话即"专家"，各有独立记忆；可带附件）

（v1.2.0 起 `vision` 并入 `ask_llm` —— "无状态单轮 + 看图" 正是 ask_llm 的
 语义加一个附件参数，不必再单开一个工具。v1.3.0 起参数 `image_paths` 泛化为
 `files`：图片走 vision 能力门，其它文件走 attach，能力不足明确报错并指路。）

内聚库 ``.lib`` 是本包内的子包（五家 provider 层 / 登录器 / 离线测试），
**随插件目录一起被 ext 分发** —— 单资产、自包含、无跨包依赖。

库模块不注册钩子/工具，无生命周期语义（见 plugin_lib_discipline）。
"""

__fp__ = {
    "schema": 1,
    "name": "webai",
    "type": "plugin",
    "version": "1.5.0",
    "description": "网页版 AI 多后端：ask_llm(联网检索+附件) / copilot(多轮协作+附件)",
    "author": "zpb",
    "license": "GPL-3.0",
}

from .plugin import WebAIPlugin

__all__ = ["WebAIPlugin"]
