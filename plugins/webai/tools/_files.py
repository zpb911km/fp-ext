"""路径 → provider 的 `files` 参数（ask_llm / copilot 共用）。

由已删除的 `vision.py` 抽出：**校验**（存在性 + 类型分诊）与**上传**（每家端点不同）
都是纯机械逻辑，两个工具都要用，故放这里，避免两份会漂移的拷贝。

v1.3.0 起从「只收图片」放宽为「收任意文件」，按扩展名分诊能力门：

    图片（IMG_EXT）→ 后端须有 vision 能力（模型要能看懂图）
    其它文件       → 后端须有 attach 能力（模型要能收下附件）
    混合           → 两者都要

能力不足一律**明确报错并指路**（cap_error），绝不静默丢文件 —— 静默丢文件会让
模型拿不到却照答不误，是最难排查的一类故障。

调用方只需：

    refs, err = upload_files(name, paths)
    if err:
        return _err(err)                 # 或 {"success": False, "error": err}
    p.search(q, files=refs) / p.ask(sid, q, files=refs)

provider 层契约（PROVIDER_SPEC §2）：`ask(..., files=[ref, ...])` —— ref 由 `upload()` 产出。
`search()` 是 `ask(search=True)` 的薄封装，故同样吃 `files`。
"""

import os
from pathlib import Path
from typing import Any

from .. import lib as _webai

# 图片扩展名 —— 只用来**分诊**（要不要 vision 能力），不再用来拒绝其它文件。
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

# 能力 → 人话（报错文案统一在这里，两个工具不各写一套）
_CAP_VERB = {"vision": "看图", "attach": "传文件"}
_CAP_HINT = {"vision": "带图可用", "attach": "带文件可用"}


def is_image(path: str) -> bool:
    return str(path).lower().endswith(IMG_EXT)


def required_caps(paths: Any) -> set:
    """这批路径要求后端具备哪些能力：图片→vision，其它→attach，混合则都要。"""
    caps = set()
    for p in paths or []:
        caps.add("vision" if is_image(p) else "attach")
    return caps


def capable(capability: str) -> list:
    """声明了该能力的后端 —— 报错时"指路"用（与 lib 的 capabilities 同源）。"""
    if not _webai:
        return []
    out = []
    for n in _webai.names():
        try:
            if capability in getattr(_webai.get(n), "capabilities", set()):
                out.append(n)
        except Exception:  # noqa: BLE001 —— 指路是尽力而为，一家挂了别拖垮全部
            continue
    return out


def cap_error(name: str, missing: set) -> str:
    """能力不足的统一报错：说清缺什么 + 谁能干（措辞就这一处，别处不要复刻）。"""
    miss = sorted(missing)
    verbs = "/".join(_CAP_VERB.get(c, c) for c in miss)
    hints = "；".join(
        f"{_CAP_HINT.get(c, c)}：{'/'.join(capable(c)) or '(无)'}" for c in miss)
    return f"provider「{name}」不支持{verbs}（无 {'/'.join(miss)} 能力）；{hints}"


def friendly_error(name: str, stage: str, exc: BaseException) -> str:
    """把裸异常翻成"下一步做什么"。归类失败时退回原始异常，绝不吞信息。"""
    try:
        kind = _webai.classify(name, exc)
        tag = getattr(kind, "value", str(kind))
        if tag == "auth":
            return (f"{stage}失败：凭据已失效（后台已自动拉起登录自愈，本次仍失败）。\n"
                    f"  立即手动：{_login_cmd(name)}\n"
                    f"  查全部：{_login_cmd('--check')}（{exc}）")
        if tag == "quota":
            return f"{stage}失败：{name} 额度/频率已用完 —— 重试无用，请换一家 provider（{exc}）"
        return f"{stage}失败（{name}·{tag}）：{exc}\n建议：{_webai.core.retry_hint(kind)}"
    except Exception:  # noqa: BLE001 —— 连归类都失败，也要给出原始信息
        return f"{stage}失败：{exc}"


def check_paths(files: Any) -> tuple[list, str]:
    """校验 → (路径列表, 错误信息)。非 list/str 一律拒绝（不猜调用方意图）。

    只查存在性 —— 类型由 required_caps() 分诊成能力门，不在这里硬拒绝：
    传 .txt/.pdf 是合法意图，能不能收是后端的事（收不了会明确报错）。
    """
    if isinstance(files, str):
        files = [files]
    if not isinstance(files, (list, tuple)):
        return [], f"files 应为字符串或字符串列表，收到 {type(files).__name__}"
    out = [str(p).strip() for p in files if str(p or "").strip()]
    if not out:
        return [], "files 为空"
    if len(out) > 6:
        # 单次上传多了既慢又容易撞后端限制 —— 明说，别让第 7 个静默丢失。
        return [], f"一次最多传 6 个文件（收到 {len(out)} 个），请分批"
    for p in out:
        if not os.path.isfile(p):
            return [], f"文件不存在 - {p}"
    return out, ""


def upload_files(name: str, paths: Any) -> tuple[list, str]:
    """校验 + 能力检查 + 上传 → `(files, err)`。err 非空时 files 无意义。

    **能力不足的后端直接报错并指路**（cap_error），而不是把文件静默丢掉 ——
    静默丢文件会让模型拿不到却照答不误（"我看不到"/凭空编造），最难排查。
    """
    if not _webai:
        return [], "webai 包缺失：<数据目录>/public/plugins/webai/lib/"
    paths, err = check_paths(paths)
    if err:
        return [], err

    try:
        p = _webai.get(name)
    except Exception as e:  # noqa: BLE001
        return [], f"未知 provider「{name}」：{e}"

    missing = required_caps(paths) - getattr(p, "capabilities", set())
    if missing:
        return [], cap_error(name, missing)

    ok, why = p.available()
    if not ok:
        return [], why

    refs = []
    for path in paths:
        try:
            refs.append(p.upload(path))
        except Exception as e:  # noqa: BLE001
            return [], friendly_error(name, f"文件上传（{path}）", e)
    return refs, ""


# 兼容旧名（v1.2.0 及更早叫 upload_images；外部/历史调用仍可能引）
upload_images = upload_files


def _login_cmd(*args: str) -> str:
    """复制即用的登录命令 —— 与 ask_llm._login_cmd 同思路（按 __file__ 推算路径）。"""
    p = Path(__file__).resolve().parent.parent / "lib" / "login.py"
    tail = (" " + " ".join(args)) if args else ""
    return f"python3 {p}{tail}"
