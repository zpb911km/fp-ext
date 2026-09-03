"""model 命令 — 在 LLM 供应商/模型间无缝切换（两级命名空间 + 持久化）

零核心侵入（扩展命令，自动发现注册）：
- 供应点表存于 ~/.config/fp/config.json 的 LLM_PROVIDERS 键，两级结构：
    LLM_PROVIDERS = { "<provider>": { api_key, base_url, ..., models: {
        "<model>": { temperature/max_tokens/extra_body 差异 } } } }
  激活 = ACTIVE_LLM 键，值 "provider/model"（唯一引用，取代顶层三键副本）
- 旧扁平格式（provider 直接带 model 字段）首次执行时自动迁移为两级结构
- 切换 = 就地替换 state.llm(LLMService) 的 _client/_config，所有引用方
  （agent/compact/前端）指向同一实例，自动跟随，无引用漂移
- 持久化 = 写 ACTIVE_LLM（真源）+ 顶层三键镜像（兼容旧读者），重启后
  config.py 模块加载时解析 ACTIVE_LLM 即恢复上次选择

用法：
  /model                       有序列表列出全部模型（带序号）+ 当前激活 + 接口图例
  /model <序号>                按列表序号切换并持久化（如 /model 2，重启后保持）
  /model <id>                  id 也支持：provider/model（精确）、provider（单模型时）、
                              模型名（全局唯一）；不同供应商同名模型用 provider/model 消歧。
                              注：序号与列表共用同一顺序（_all_entries），所见即所用。
"""

__fp__ = {
    "name": "model",
    "version": "1.0.0",
    "description": "/model 查看/切换 LLM 供应商·模型（有序列表+序号切换，持久化）",
    "author": "zpb",
    "license": "GPL-3.0",
    "type": "commands",
}

import json
import os

from fp_core import config
from fp_core.core.llm_client import Client
from fp_core.core.llm_service import LLMConfig
from fp_core.core.state import State

name = "model"
aliases: list[str] = []
description = "查看/切换 LLM 供应商·模型（两级结构，持久化，重启后保持）"

# ── config.json 读写（保留全部既有键，原子写回） ───────────────────


def _load_cfg() -> dict:
    """读取用户 config.json；不存在/损坏 → {}。"""
    try:
        with open(config.USER_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write(cfg: dict) -> None:
    """原子写回 config.json（tmp + os.replace）。"""
    tmp = config.USER_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config.USER_CONFIG_PATH)


# ── 旧格式一次性迁移 ───────────────────────────────────────────────


def _ensure_new_schema(cfg: dict) -> tuple[bool, str]:
    """旧扁平格式 → 两级结构；缺 ACTIVE_LLM → 由顶层三键推导补写。

    Returns:
        (是否发生写盘级变更, 变更说明)
    """
    notes: list[str] = []
    changed = False
    raw = cfg.get("LLM_PROVIDERS")
    if isinstance(raw, dict):
        legacy = any(
            isinstance(v, dict) and isinstance(v.get("model"), str) and not isinstance(v.get("models"), dict)
            for v in raw.values()
        )
        if legacy:
            cfg["LLM_PROVIDERS"] = config.normalize_providers(raw)
            notes.append("LLM_PROVIDERS 已从旧扁平格式迁移为两级结构（provider/models）")
            changed = True
    if not cfg.get("ACTIVE_LLM"):
        top_model = cfg.get("LLM_MODEL", "")
        if top_model:
            norm = config.normalize_providers(cfg.get("LLM_PROVIDERS"))
            inferred = config.infer_active_from_top(
                norm,
                top_model,
                str(cfg.get("LLM_API_BASE_URL", "") or ""),
                cfg.get("LLM_API_KEY", ""),
            )
            if inferred:
                cfg["ACTIVE_LLM"] = inferred
                notes.append(f"ACTIVE_LLM 已由顶层三键推导为 `{inferred}`")
                changed = True
    return changed, "；".join(notes)


# ── 匹配与展示 ─────────────────────────────────────────────────────


def _all_entries(norm: dict) -> list[tuple[str, str]]:
    """展平全部 (provider, model)，顺序 = 表内配置顺序。

    渲染（_render）与序号解析共用此顺序，保证 `/model N` 与列表中的
    序号一一对应（所见即所用）。
    """
    return [
        (pname, m)
        for pname, p in norm.items()
        for m in list(p.get("models", {}))
    ]


def _model_note(norm: dict, provider: str, model: str) -> str:
    """模型级差异参数的紧凑注记（无差异 → ""）。"""
    md = norm.get(provider, {}).get("models", {}).get(model, {})
    if not md:
        return ""
    bits = []
    if md.get("temperature") is not None:
        bits.append(f"temp={md['temperature']}")
    if md.get("max_tokens") is not None:
        bits.append(f"max_tokens={md['max_tokens']}")
    if md.get("extra_body"):
        bits.append("extra_body")
    return f"  ·  {', '.join(bits)}" if bits else ""


def _resolve_target(norm: dict, arg: str) -> tuple[str, str | tuple[str, str]]:
    """把用户输入解析为目标 (provider, model)。

    Returns:
        ("ok", (provider, model))  可切换
        ("err", 消息)              无法匹配（不改动当前状态）
        ("warn", 消息)             命中多个/需消歧（不改动当前状态）
    """
    arg_s = arg.strip()
    if not arg_s:
        return ("err", "")
    low = arg_s.lower()

    # 0. 纯数字 → 有序列表序号（与 _render 同序）。模型名若为纯数字（罕见），
    #    请用 `provider/数字` 形式走 A 分支精确指定。
    if arg_s.isdigit():
        entries = _all_entries(norm)
        if not entries:
            return ("err", "尚无可用模型。`/model` 查看配置指引。")
        idx = int(arg_s)
        if 1 <= idx <= len(entries):
            return ("ok", entries[idx - 1])
        return ("err", f"序号越界：有效范围 1~{len(entries)}。`/model` 查看列表。")

    # A. provider/model 精确
    if "/" in arg_s:
        p, _, m = arg_s.partition("/")
        if p in norm and m in norm[p].get("models", {}):
            return ("ok", (p, m))
        return (
            "err",
            f"找不到 `{arg_s}`（应为 `provider/model`，且两者都需存在于 LLM_PROVIDERS）。`/model` 查看全部。",
        )

    # B. provider 名（精确 → 唯一包含）
    pk = [k for k in norm if low == k.lower()]
    if not pk:
        pk = [k for k in norm if low in k.lower()]
    if len(pk) == 1:
        p = pk[0]
        ms = list(norm[p].get("models", {}))
        if len(ms) == 1:
            return ("ok", (p, ms[0]))
        return ("warn", f"供应商 `{p}` 下有多个模型：{'、'.join(ms)}。请用 `{p}/<model>` 指定。")
    if len(pk) > 1:
        return ("err", f"`{arg_s}` 同时匹配多个供应商：{'、'.join(pk)}。请用 `provider/model` 精确指定。")

    # C. 模型名全局唯一（精确 → 包含）
    mh = [(p, m) for p, pv in norm.items() for m in pv.get("models", {}) if low == m.lower()]
    if not mh:
        mh = [(p, m) for p, pv in norm.items() for m in pv.get("models", {}) if low in m.lower()]
    if len(mh) == 1:
        return ("ok", mh[0])
    if len(mh) > 1:
        names = "、".join(f"`{p}/{m}`" for p, m in mh)
        return ("err", f"模型名 `{arg_s}` 在多个供应商下存在：{names}。请用 `provider/model` 消歧。")
    return ("err", f"找不到匹配 `{arg_s}` 的供应商或模型。`/model` 查看全部。")


def _svc_state(svc) -> tuple[str, str, str]:
    """从 LLMService 取真实运行态 (api_key, base_url, model)。"""
    client = getattr(svc, "_client", None)
    cfgobj = getattr(svc, "_config", None)
    return (
        getattr(client, "api_key", ""),
        str(getattr(client, "base_url", "") or ""),
        getattr(cfgobj, "model", ""),
    )


def _current_active(norm: dict, svc) -> tuple[str | None, str]:
    """按 svc 真实运行态在表中找命中的 (provider, model)。

    Returns:
        (命中的 "provider/model" | None, 当前实际值描述)
    """
    api_key, base_url, model = _svc_state(svc)
    for pname, p in norm.items():
        if model not in p.get("models", {}):
            continue
        if base_url.rstrip("/") != str(p.get("base_url", "") or "").rstrip("/"):
            continue
        if api_key != p.get("api_key", ""):
            continue
        return f"{pname}/{model}", f"{model} @ {p.get('base_url')}"
    if model:
        return None, f"{model} @ {base_url}（不在供应点表内）"
    return None, "未初始化"


def _render(cfg: dict, svc) -> str:
    norm = config.normalize_providers(cfg.get("LLM_PROVIDERS"))
    active, cur_desc = _current_active(norm, svc)
    entries = _all_entries(norm)
    width = max(1, len(str(len(entries))))
    lines = ["## 🔌 LLM 供应商 / 模型", ""]
    if not entries:
        lines.append("> 配置中无 `LLM_PROVIDERS`。可在 `~/.config/fp/config.json` 添加：")
        lines.append("```json")
        lines.append(
            '"LLM_PROVIDERS": { "my": { "api_key": "sk-…", "base_url": "https://…/v1", '
            '"models": { "my-model": {} } } }'
        )
        lines.append("```")
        lines.append("> 切换后自动写入 `ACTIVE_LLM = \"my/my-model\"`（重启后保持）。")
    else:
        used: list[str] = []
        for i, (pname, m) in enumerate(entries, 1):
            kid = f"{pname}/{m}"
            if pname not in used:
                used.append(pname)
            mark = "✅ " if kid == active else ""
            note = _model_note(norm, pname, m)
            lines.append(f"{i:>{width}}. {mark}`{kid}`{note}")
        conn = "；".join(
            f"`{p}` → `{norm[p].get('base_url') or '?'}`" for p in used
        )
        lines += ["", f"接口：{conn}"]
    lines += ["", f"当前：`{cur_desc}`", ""]
    lines += [
        "用法：`/model <序号>` 按上面列表序号切换并持久化（如 `/model 2`）",
        "  id 也支持 `provider/model`、供应商名（供应商下仅 1 个模型时）、模型名（全局唯一时）",
    ]
    return "\n".join(lines)


# ── 就地换芯 ───────────────────────────────────────────────────────


async def _switch(svc, provider: str, model: str, cfg: dict):
    """按 (provider, model) 构造新 client+config 并替换 LLMService 内部状态。

    参数由 config.resolve_llm_params 三级合并（model → provider → 顶层 → 默认）。
    旧 client 由调用方（execute）保存并关闭。
    """
    params = config.resolve_llm_params(provider, model)
    if params is None:
        raise ValueError(f"供应点 `{provider}/{model}` 无法解析（不在 LLM_PROVIDERS 表中）")

    new_client = Client(
        api_key=params["api_key"],
        base_url=params["base_url"],
        timeout=params["timeout"],
    )
    svc._client = new_client
    svc._config = LLMConfig(
        model=params["model"],
        temperature=params["temperature"],
        max_tokens=params["max_tokens"],
        extra_body=params["extra_body"],
    )
    return new_client, params


async def _close_old(old_client) -> None:
    if old_client is None:
        return
    close = getattr(old_client, "close", None)
    if close is None:
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception:
        pass  # 旧连接池关闭失败不影响切换


# ── execute ────────────────────────────────────────────────────────


async def execute(state: State, arg: str) -> tuple[bool, str]:
    svc = state.llm
    if not (hasattr(svc, "_client") and hasattr(svc, "_config")):
        return (True, "当前 LLM 服务对象不支持运行时切换（非 LLMService 实例）。")

    cfg = _load_cfg()
    migration_note = ""
    if isinstance(cfg, dict):
        changed, note = _ensure_new_schema(cfg)
        if changed:
            migration_note = note
            try:
                _atomic_write(cfg)
                migration_note += "（已写回 config.json）"
            except OSError as e:
                migration_note += f"（⚠️ 写回失败：{e}）"
    else:
        cfg = {}

    norm = config.normalize_providers(cfg.get("LLM_PROVIDERS"))

    if not arg.strip():
        body = _render(cfg, svc)
        if migration_note:
            body += f"\n\n> ♻️ {migration_note}"
        return (True, body)

    status, payload = _resolve_target(norm, arg)
    if status == "err":
        if migration_note:
            return (True, f"{payload}\n\n> ♻️ {migration_note}")
        return (True, payload)
    if status == "warn":
        if migration_note:
            return (True, f"{payload}\n\n> ♻️ {migration_note}")
        return (True, payload)
    provider, model = payload  # type: ignore[misc]

    # ── 就地换芯（旧 client 先留着，换完再关，避免构造异常丢状态） ──
    old_client = getattr(svc, "_client", None)
    try:
        new_client, params = await _switch(svc, provider, model, cfg)
    except Exception as e:
        return (True, f"❌ 切换失败（未改动当前状态）：{e}")

    # 同步 agent.client → agent shutdown 时能正确关闭新 client
    agent = getattr(state, "agent", None)
    if agent is not None and hasattr(agent, "client"):
        agent.client = new_client
    await _close_old(old_client)

    # ── 持久化：ACTIVE_LLM 真源 + 顶层三键镜像（温度/上限不镜像，保留全局默认）──
    cfg["ACTIVE_LLM"] = f"{provider}/{model}"
    cfg["LLM_API_KEY"] = params["api_key"]
    cfg["LLM_API_BASE_URL"] = params["base_url"]
    cfg["LLM_MODEL"] = params["model"]
    try:
        _atomic_write(cfg)
        persist_note = "已持久化，重启后保持此供应点。"
    except OSError as e:
        persist_note = f"⚠️ 已切换但持久化失败（{e}），重启后可能回退。"

    # 同步模块内存态（config.LLM_* / ACTIVE_LLM 不再滞后）
    config.set_active_llm_state(
        provider=provider,
        model=params["model"],
        api_key=params["api_key"],
        base_url=params["base_url"],
        temperature=params["temperature"],
        max_tokens=params["max_tokens"],
        extra_body=params["extra_body"],
        timeout=params["timeout"],
        retry_count=params["retry_count"],
    )

    note_role = ""
    role = getattr(getattr(state, "agent", None), "_role", None)
    if role is not None and getattr(role, "llm_model", None):
        note_role = "\n\n> ℹ️ 当前为角色模式，本切换会覆盖角色指定的模型（仅本次运行）"

    return (
        True,
        "✅ 已切换到 `%s/%s`\n\n- 模型：`%s`\n- 接口：`%s`\n- 温度 / 上限：%s / %s\n- extra_body：%s\n\n%s%s%s"
        % (
            provider,
            params["model"],
            params["model"],
            params["base_url"],
            params["temperature"],
            params["max_tokens"],
            json.dumps(params["extra_body"], ensure_ascii=False),
            persist_note,
            note_role,
            (f"\n\n> ♻️ {migration_note}" if migration_note else ""),
        ),
    )
