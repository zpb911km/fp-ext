"""
webai — 网页版 AI provider 层
=================================

把"某个网页版 AI 究竟怎么调"收拢在这里，工具层（copilot / ask_llm / vision）
只声明自己要什么能力，不关心后端的端点与鉴权细节。

加一个新后端 = 在本目录加一个模块，然后在下面 _MODULES 注册一行。

统一接口（每个 provider 模块实现）
---------------------------------
    name           str        提供方标识，如 "qwen"
    capabilities   set[str]   {"chat", "search", "vision"} 的子集
    default_model  str

    available() -> (bool, str)                      # 凭据/依赖是否就绪
    new_session(model="") -> str                    # 新建会话，返回会话 id
    ask(session_id, text, *, parent_message_id=None, model="", think=False,
        search=False, files=None) -> dict
        # {"text", "thinking", "message_id", "references", "queries"}
    upload(path) -> dict                            # {"ref", "name", "size"}
    search(query, *, think=False, model="") -> dict # 便捷：新会话 + search=True

工具侧用法
---------
    webai = load_webai()            # 见各插件里的加载器（加载器不往 sys.path 加目录）
    p = webai.get("deepseek")
    ref = p.upload("/tmp/a.png")
    r = p.ask(sid, "描述这张图", files=[ref])
    print(r["text"], r["references"])
"""

import importlib

_MODULES = {
    "qwen": "qwen",
    "deepseek": "deepseek",
    "stepfun": "stepfun",
    "glm": "glm",
}

_cache = {}


def get(name: str):
    """取得 provider。延迟 import —— 某个后端依赖缺失时不影响其他后端。"""
    name = (name or "").lower()
    if name in _cache:
        return _cache[name]
    if name not in _MODULES:
        raise KeyError(f"未知 provider: {name}（可用: {', '.join(_MODULES)}）")
    mod = importlib.import_module(f".{_MODULES[name]}", __package__)
    _cache[name] = mod
    return mod


def names() -> list:
    return list(_MODULES)


# 默认优先级：deepseek 优先（默认不带思考，简单问答明显快），其余按注册序兜底。
# 凭据未就绪的会被跳过 —— 避免"默认后端 token 一过期就整体不可用"。
DEFAULT_ORDER = ("deepseek", "qwen", "glm", "stepfun")


def first_available(capability: str = "", order=None):
    """按优先级挑第一个可用 provider。

    capability 非空时只考虑声明了该能力的后端，且会跳过凭据未就绪的。
    返回 (name, tried) —— name 为空表示全都不可用，tried 是逐家原因（给人看）。
    """
    cands = [n for n in (order or DEFAULT_ORDER) if n in _MODULES]
    cands += [n for n in _MODULES if n not in cands]
    tried = []
    for n in cands:
        try:
            mod = get(n)
        except Exception as e:  # noqa: BLE001
            tried.append(f"{n}:加载失败({type(e).__name__})")
            continue
        if capability and capability not in getattr(mod, "capabilities", set()):
            tried.append(f"{n}:无 {capability} 能力")
            continue
        try:
            ok, why = mod.available()
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"{type(e).__name__}: {e}"[:80]
        if ok:
            return n, tried
        tried.append(f"{n}:{why}")
    return "", tried


def available() -> dict:
    """{provider: (是否可用, 原因)} —— 用于给用户提示"该配哪个凭据" """
    out = {}
    for n in _MODULES:
        try:
            out[n] = get(n).available()
        except Exception as e:  # noqa: BLE001
            out[n] = (False, f"{type(e).__name__}: {e}")
    return out


# ── 供插件使用的包加载器 ────────────────────────────────────────
# fp 的插件加载器用 spec_from_file_location 逐个加载文件，不会把插件目录加进 sys.path，
# 所以插件无法直接 `import webai`。各插件复制下面这个小函数即可（约 15 行，刻意不抽公共模块，
# 避免"为了复用而再引入一层加载难题"）。

BOOTSTRAP_SOURCE = '''
def load_webai():
    """把 public/webai 包按路径加载进来（插件加载器不往 sys.path 加目录）"""
    import importlib.util as _iu
    import sys as _sys
    from pathlib import Path as _P

    # ⚠️ 别缓存！fp 的 /reload 不会清 sys.modules["webai"]，
    #    复用缓存会导致新增 provider 后重载仍拿到旧 _MODULES。
    for _k in [k for k in list(_sys.modules) if k == "webai" or k.startswith("webai.")]:
        _sys.modules.pop(_k, None)
    try:
        from fp_core.platform_utils import get_data_dir
        data = _P(get_data_dir())
    except Exception:
        data = _P.home() / ".local" / "share" / "fp"
    pkg = data / "public" / "webai"
    if not (pkg / "__init__.py").exists():
        return None
    spec = _iu.spec_from_file_location(
        "webai", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    mod = _iu.module_from_spec(spec)
    _sys.modules["webai"] = mod
    spec.loader.exec_module(mod)
    return mod
'''
