"""webai 的三个工具实现。

每个模块统一提供三个符号（供 plugin.py 注册）：

    TOOL_NAME   str                 工具名（= function name）
    DEFINITION  dict                OpenAI function-calling schema
    execute     async def(params)   执行器，返回字符串
"""
