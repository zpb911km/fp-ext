"""subagent —— 派遣一个**一次性**子实例执行独立任务，同步取回结果。

来源：public 资产（fp-ext 分发）。**不是出厂内置工具**——它曾被内置在
``fp_core/tools/extensions/``，但内置位置让核心反向依赖了前端（见下"依赖倒置"），
且把「多智能体」这件事教义上劈成了两套（层级调用 vs 对等邻居）。现在它躺在这里，
需要时装、不需要时不装，核心一无所知。

它是什么
========
一个**同步 RPC**：spawn → 等 stdout → 拿字符串。与之互补的是 ``zeta``（邻居面）：
异步、对等、可离线、可反向找你、但**不保证**被处理。

选择建议：
- 结论要**在本轮内**用于决策、且愿意等 → subagent（本资产）
- 事情不急、对方有自己的议程、要谈多轮、要契约双锁 → zeta（``peer_send``）

它**不是**"更多智能体"：真正的价值只有两条——上下文外部化（脏活不进我的
上下文）、独立验证（第三方视角）。"多"是副产品。

依赖倒置（为什么能搬出核心）
============================
旧实现写死 ``[sys.executable, "-m", "fp_cli.main", "-m"]``：核心侧代码反向依赖
了具体前端（L2→L3 违规），且换前端（ACP / WebUI / 第三方）即静默失效。
现在拉起命令由**前端自己**经 ``FP_SPAWN_ENTRY`` 声明，本资产只读不猜；
未声明时给出一句可读拒绝，而不是 traceback。

防递归
======
``fp_core.spawn.MAX_DEPTH`` 是唯一上限（当前 = 1，即子实例不可再派）。
本资产不自设阈值，改上限只改那一个常量。

会话归属
========
父进程**预分配** sid 传给子进程（``FP_SPAWN_SID``），因此子进程无论怎么死
（含 SIGKILL）父进程都能兜底补写 meta；``source`` 由本资产自报
（``FP_SPAWN_SOURCE=subagent``），核心不认识这个名字——它只把自报值前缀到
摘要、在列表里打 🔹 标记。换任何名字都同样工作。
"""

import asyncio
import json
import os
import time
from typing import Any, cast

# ── 本资产自报的身份（核心不认识这个字符串，只是透传）────────────────
SOURCE_NAME = "subagent"

# ── 会话兜底：子进程可能被 SIGKILL/崩溃（跑不到 shutdown），父进程补写 meta ──


def _derive_summary_from_file(sid: str) -> str:
    """从会话文件派生摘要（与 ``save_and_summarize`` 同策略：末条 user 前 20 字）。

    复用核心的 ``derive_summary``，不自造第二套策略——过去这里是独立实现的
    ``last_user[:20]``，核心改成 kw 派生后两套规则就漂移了。
    """
    from fp_core.core.session import derive_summary, session_file_path
    from fp_core.core.session_keywords import read_session

    path = session_file_path(sid)
    if not os.path.exists(path):
        return ""
    meta, msgs = read_session(path)
    last_user = ""
    for msg in reversed(msgs):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                last_user = content
                break
    return derive_summary(meta.get("kw") or [], last_user)


def _finalize_session(sid: str, parent: str, fallback_summary: str = "") -> None:
    """兜底补写子会话 meta（source + parent_sid + summary）。

    **只收尾已落盘的子会话**：文件不存在 = 子实例从未产出任何消息
    （启动即失败/空转），此时补写只会造出一个 0 长度会话文件，对会话列表
    纯属噪声——直接跳过，不创建文件。
    """
    from fp_core.core.session import (
        derive_summary,
        session_file_path,
        tagged_summary,
        update_session_meta,
    )

    if not os.path.exists(session_file_path(sid)):
        return

    try:
        summary = _derive_summary_from_file(sid)
        if not summary and fallback_summary:
            summary = derive_summary(None, fallback_summary)
        update_session_meta(
            sid,
            source=SOURCE_NAME,
            parent_sid=parent,
            summary=tagged_summary(SOURCE_NAME, summary),
        )
    except Exception:
        pass  # 兜底失败不该反过来弄挂派遣方（子会话已在盘上，人工可查）


# ── 插件定义（OpenAI function calling schema）────────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": (
            "派遣子 agent 执行独立任务，子 agent 有独立上下文，"
            "其工具调用和中间推理不占主上下文 token。"
            "适合多步分析、文件处理、代码调试等。"
            "超过 2 步工具调用或读大文件时使用。并发执行,适合平行任务分配."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "任务描述，需清晰完整自包含",
                },
                "cwd": {
                    "type": "string",
                    "description": "必填。工作目录，通过 bash(pwd) 获取",
                },
                "context": {
                    "type": "string",
                    "description": "可选。背景上下文：文件路径、代码片段、前置结论等。子 agent 无对话历史",
                },
                "store_result": {
                    "type": "string",
                    "description": "可选。结果自动存入记忆，之后 memory_read 读取。如 'draft_content'",
                },
                "timeout": {
                    "type": "integer",
                    "description": "可选。超时秒数；缺省或 0 = 无限时长（默认）。显式值下限 10 秒，无上限",
                },
                "constraints": {
                    "type": "object",
                    "description": "可选。输出契约",
                    "properties": {
                        "verbose": {
                            "type": "boolean",
                            "description": "默认 false，只输出结论；true 输出完整推理链",
                        },
                        "output_format": {
                            "type": "string",
                            "enum": ["text", "json", "markdown"],
                            "description": "输出格式，默认 text",
                        },
                        "max_length": {
                            "type": "integer",
                            "description": "结果最大字符数，默认不限制",
                        },
                    },
                },
            },
            "required": ["task", "cwd"],
        },
    },
}


def _err(result: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "result": result, **extra}, ensure_ascii=False, indent=2)


async def execute(params: dict[str, Any]) -> str:
    """执行子 agent 任务（同步等待其 stdout）。"""
    task = str(params.get("task") or "").strip()
    cwd = str(params.get("cwd") or "").strip()
    context = str(params.get("context") or "")
    store_result = str(params.get("store_result") or "").strip()
    timeout = params.get("timeout") or 0
    try:
        timeout = max(10, int(timeout)) if timeout else 0
    except (TypeError, ValueError):
        timeout = 0

    constraints = cast("dict[str, Any]", params.get("constraints") or {})
    verbose = bool(constraints.get("verbose", False))
    output_format = str(constraints.get("output_format") or "text")
    max_length = constraints.get("max_length")

    if not task:
        return _err("task 不能为空")
    if not cwd:
        return _err("cwd 不能为空（用 bash(pwd) 获取当前工作目录）")
    if not os.path.isdir(cwd):
        return _err(f"cwd 目录不存在: {cwd}")

    # ── 递归守卫：深度上限由核心常量统一管理，本资产不自设阈值 ──
    from fp_core.spawn import MAX_DEPTH, can_spawn, depth

    if not can_spawn():
        return _err(
            f"递归调用被拒绝：当前实例深度 {depth()}（上限 {MAX_DEPTH}），不可再次派遣。"
            "请直接在当前上下文中完成任务。"
        )

    # ── 拉起命令：由前端声明的 FP_SPAWN_ENTRY 提供（依赖倒置）──
    from fp_core.spawn import entry

    argv = entry()
    if not argv:
        return _err(
            "当前前端未声明拉起入口（FP_SPAWN_ENTRY 缺失），无法派遣子 agent。"
            "这是前端能力缺失，不是参数问题——请换用 bash 后台任务或直接在本上下文完成。"
        )

    from fp_core.core.session import generate_sid, get_current_session_id
    from fp_core.spawn import child_env

    query = f"[背景信息]\n{context}\n\n[任务]\n{task}" if context else task

    # 预分配 sid：父进程预知子会话，子进程怎么死父进程都能兜底补 meta
    sub_sid = generate_sid()
    _parent_sid = get_current_session_id() or ""

    env = child_env(
        sid=sub_sid,
        parent=_parent_sid,
        source_name=SOURCE_NAME,
        silent=not verbose,
    )
    # quiet 与 silent 不同：quiet 关 Logo/提示，silent 关 display.* 全部输出。
    # 派遣场景两个都要（子实例的 stdout 是**数据通道**，不是给人看的）。
    from fp_core.spawn import QUIET_ENV

    env[QUIET_ENV] = "1"

    start_time = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            query,
            stdin=asyncio.subprocess.DEVNULL,  # 不继承父终端 stdin，防子实例内交互工具读父 TTY 挂起
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout or None)
        except TimeoutError:
            # 优雅终止：先 SIGTERM（给子实例走 shutdown 写摘要），3 秒后退 SIGKILL
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (TimeoutError, Exception):
                proc.kill()
                await proc.wait()
            _finalize_session(sub_sid, _parent_sid, fallback_summary=task)
            return _err(f"子任务超时（{timeout}秒）", duration=f">{timeout}s", estimated_tokens=0)
        except (KeyboardInterrupt, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            _finalize_session(sub_sid, _parent_sid, fallback_summary=task)
            raise
    except Exception as e:
        _finalize_session(sub_sid, _parent_sid, fallback_summary=task)
        return _err(f"子任务启动失败: {e}", duration=f"{time.time() - start_time:.1f}s", estimated_tokens=0)

    duration_str = f"{time.time() - start_time:.1f}s"

    if proc.returncode not in (0, None):
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        _finalize_session(sub_sid, _parent_sid, fallback_summary=task)
        return _err(
            f"子 agent 退出码 {proc.returncode}" + (f"\n{stderr_text[-2000:]}" if stderr_text else ""),
            duration=duration_str,
            estimated_tokens=0,
        )

    output = stdout.decode("utf-8", errors="replace").strip()
    _finalize_session(sub_sid, _parent_sid, fallback_summary=task)

    if not output:
        return json.dumps(
            {"status": "warning", "result": "子任务完成但无输出文本", "duration": duration_str, "estimated_tokens": 0},
            ensure_ascii=False,
            indent=2,
        )

    # ── 后处理：按输出契约转换 ──
    if isinstance(max_length, (int, float)) and max_length > 0:
        cap = int(max_length)
        if len(output) > cap:
            total = len(output)
            output = output[:cap] + f"\n\n...（已截断，原文 {total} 字符）"

    if output_format == "json":
        try:
            json.loads(output)
        except (json.JSONDecodeError, ValueError):
            output = json.dumps({"reply": output}, ensure_ascii=False, indent=2)

    if store_result:
        try:
            # 按名走 L1 公共工具注册表（禁止 import 兄弟扩展单元）
            from fp_core.tools import execute_tool

            await execute_tool(
                "memory_save",
                {
                    "root": "~",
                    "category": "reference",
                    "name": store_result,
                    "description": f"[{SOURCE_NAME}] {task[:80]}",
                    "content": output,
                },
            )
        except Exception as e:
            output += f"\n\n⚠️ 记忆保存失败 ({store_result}): {e}"

    # 成功时返回纯文本：主 agent 直接看到子 agent 的回复，
    # 不包 JSON 壳、不让 metadata 污染 LLM 的 tool result
    return output
