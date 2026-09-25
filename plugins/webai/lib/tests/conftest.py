#!/usr/bin/env python3
"""pytest 适配层 —— 把两个测试文件「自带 runner」的形参桥接成 fixture。

背景：这些测试是**为独立运行写的**::

    cd <PKG> && python3 tests/test_qwen_resilience.py

它们自己 `load()` 包、在 `main()` 里把 `webai`（和 `login`）当**位置参数**传给
每个 test_*。独立跑完全正常；但被 pytest 收集时，形参会被当成 fixture 查找 →
`fixture 'webai' not found`（8 个 ERROR）。

这里补上等价 fixture，保证两条路径**同一份加载逻辑**（先清 sys.modules 再重新
exec，避免拿到上一轮被 stub 掉 `_session` 的脏模块）。fixture 的行为与
`main()` 里手写的那段完全一致 —— 修这里时请同步修两边，或干脆只改 `load()`。
"""

import importlib
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)


def _load_webai():
    """与 test_*.py 里的 `load()` 逐字对齐（刻意保留两份，避免跨文件隐式耦合）。"""
    for k in [k for k in list(sys.modules) if k == "webai" or k.startswith("webai.")]:
        sys.modules.pop(k, None)
    spec = importlib.util.spec_from_file_location(
        "webai", os.path.join(PKG, "__init__.py"), submodule_search_locations=[PKG]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webai"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def webai():
    return _load_webai()


@pytest.fixture
def glm(webai):
    """被 webai 内部加载的那个 glm 模块实例。

    ⚠️ 别在测试里 `import glm` —— 那是**另一个模块对象**（不同 spec 加载），
    stub 打上去 webai 根本看不见，会变成"以为在 stub 其实发了真实请求"
    （2026-09 就这么白花了一次视频积分）。永远用 `webai.raw("<name>")`。
    """
    return webai.raw("glm")


@pytest.fixture
def login(webai):
    """`login.py` 是**脚本式**模块（顶层直接 `import qwen` / `import core`），
    所以必须先把 PKG 塞进 sys.path —— main() 里也是这么做的。"""
    if PKG not in sys.path:
        sys.path.insert(0, PKG)
    sys.modules.pop("login", None)      # 每个测试拿到干净的 login，避免 stub 泄漏
    return importlib.import_module("login")
