"""webai 的两个工具实现（v1.2.0 起 vision 并入 ask_llm；v1.3.0 起 image_paths → files）。

每个模块统一提供三个符号（供 plugin.py 注册）：

    TOOL_NAME   str                 工具名（= function name）
    DEFINITION  dict                OpenAI function-calling schema
    execute     async def(params)   执行器，返回字符串

``_files.py`` 是两个工具共用的辅助（路径校验 + 类型分诊 + 上传），**不注册**，
只提供 ``upload_files``（别名 ``upload_images``）/ ``check_paths`` /
``required_caps`` / ``cap_error`` / ``friendly_error``。
"""
