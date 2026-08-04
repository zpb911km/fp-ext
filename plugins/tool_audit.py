"""
Tool Audit — 工具审核插件
==========================
人工审核 Agent 的每一次工具调用，让用户对危险操作拥有最终决定权。
仅在**终端 CLI** 环境下生效，其他界面自动放行、零打扰。

────────────────────────────────────────────────────────────
一、为什么需要它
────────────────────────────────────────────────────────────
FP Agent 会自主调用 bash / write_file / edit_file / subagent 等工具。
多数时候这是高效且安全的，但以下场景你可能会想先过目一眼：

  • Agent 即将执行一条影响面较大的命令（如 rm、git push、pip install）
  • Agent 要修改/删除你本地的文件
  • Agent 打算派遣子代理执行你不知道的任务
  • 你想在放行前顺手调整工具的参数（如加个 --dry-run）

本插件在**工具真正执行之前**拦截，把调用展示给你，由你决定
放行 / 拒绝 / 改参数。

────────────────────────────────────────────────────────────
二、适用环境（仅终端）
────────────────────────────────────────────────────────────
只在终端会话（fp-terminal 的 CLIIO，即 input() 交互）启用审核：

  CLI 终端   → ✅ 交互审核（io.ask() 等待你的决定）
  WebUI      → ✅ 自动放行，不打扰前端
  ACP/IDE    → ✅ 自动放行
  REST API   → ✅ 自动放行
  子代理     → ✅ 自动放行（FP_IS_SUBAGENT=1 时豁免）

判断依据：`get_current_io()` 返回的通道类名为 CLIIO
（或类名含 "CLI" 且实现了 ask()）才认为处于终端。

────────────────────────────────────────────────────────────
三、三种审核模式（会话中按 s 可切换）
────────────────────────────────────────────────────────────
  all   — 审核所有有副作用的工具调用（默认；只读查询自动放行）
  risky — 只审核危险工具列表中的工具
  off   — 不审核，直接放行

危险工具默认列表（risky 模式只审这些）：
  bash, python, write_file, edit_file, delete_file, subagent

────────────────────────────────────────────────────────────
四、交互快捷键
────────────────────────────────────────────────────────────
  y / Enter  批准，继续执行
  n          拒绝（工具返回"被用户拒绝"）
  m          修改参数（输入新的 JSON，空行取消）
  a          全部批准（本次会话后续不再询问）
  s          切换审核模式（all / risky / off）
  h          帮助

参数展示：完整展示、不折叠不截断（多行值保留换行，缩进对齐），
所见即所审。

────────────────────────────────────────────────────────────
五、工作原理
────────────────────────────────────────────────────────────
  • 注册 LifecycleHook.ON_TOOL_CALL（transform 型钩子）
  • Agent 每次执行工具前，fp-core 触发该钩子（agent.py
    _execute_one_tool），本插件在此时拦截
  • 通过 HookContext.data 与主流程通信：
      cancelled=True + cancel_reason   → 拒绝该工具
      modified_tool_args=<JSON>        → 修改后的参数
  • 安全设计：transform 钩子异常会危及主流程，因此处理函数
    整体 try/except 兜底——任何异常只记日志并放行，绝不阻断
  • 并发安全：fp-core 用 asyncio.gather 并行执行工具，多个
    ON_TOOL_CALL 可能同时触发；本插件用 asyncio.Lock 将审核
    过程串行化——同一时刻只展示/询问一个工具，其余排队等待

────────────────────────────────────────────────────────────
六、安装 / 启用 / 禁用
────────────────────────────────────────────────────────────
  • 位置：{DATA}/private/plugins/tool_audit.py（三来源布局的
    private 来源，加载优先级最高）
  • 启用：文件名为 tool_audit.py 即被 PluginRegistry.scan() 加载
  • 禁用：改名为 tool_audit.py.disabled（或删除）后重启 /reload
  • 生效：插件在启动 scan() 时加载，改文件后需重启 FP 或 /reload

────────────────────────────────────────────────────────────
七、配置项（构造参数，均有默认值）
────────────────────────────────────────────────────────────
  mode: str            审核模式，默认 "all"
  risky_tools: set     危险工具列表，默认 DEFAULT_RISKY_TOOLS
  show_full_args: bool 是否展示参数（默认 True；False 则只显示工具名，不显示参数）

────────────────────────────────────────────────────────────
八、版本历史
────────────────────────────────────────────────────────────
  v2.3.0  (2026-08-04) 终端输出协调：解决审核提示与工具输出打架
    - 借 fp-terminal LLMStreamer 的类级输出锁：审核提示（input()）期间
      持有同一把锁，工具调用/结果输出排队，用户按键后再按序刷出
    - 展示提示前先 _flush_pending_outputs() 等上一个工具的输出刷完，
      让"提示 → 批准 → 该工具输出"顺序干净（理想序）
    - 锁获取失败自动降级 _NoopLock，非 fp-terminal 环境不崩不阻塞
    - 死锁分析：工具执行本身不碰该锁，输出协程只等待打印；提示只读
      锁不嵌套 → 无环
  v2.2.0  (2026-08-04) 并发安全修复：
    - 修复并行工具调用（fp-core asyncio.gather）时多个审核界面交叠：
      新增 asyncio.Lock 串行化审核，一次只展示/询问一个工具，其余排队
    - all 模式默认放行只读查询工具（read_file/memory_read/outline/
      task_list/web_fetch/ask_llm/vision/codegraph），只审有副作用的调用
    - 新增构造参数 skip_no_side_effect（默认 True）
  v2.1.0  (2026-08-04) 展示优化：参数完整展示、不折叠不截断（多行值
          保留换行缩进对齐），移除已无意义的 d 键
  v2.0.0  (2026-08-04) 重写适配当前 fp-core：
    - 移除已不存在的 fp_core.display 依赖 → 统一用 io.info/warning/error
    - 终端检测：get_current_io() 通道类名为 CLIIO 才启用审核
    - transform 钩子整体 try/except 兜底：异常一律放行
    - 危险工具列表扩充：加入 python（可执行任意代码）
  v1.1.0  旧版（已废弃）：依赖 fp_core.display，不适配当前 core，
          曾以 .disabled 停用
"""

__fp__ = {
    "name": "tool_audit",
    "version": "2.3.0",
    "description": "工具调用人工审核（仅终端）",
    "author": "zpb",
    "license": "GPL-3.0",
    "type": "plugins",
}


import asyncio
import json
import os

from fp_core.core.agent import get_current_io
from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import Plugin, PluginConfig

# ── 默认危险工具列表 ──────────────────────────────
# mode=risky 时，只有这些工具会被审核
DEFAULT_RISKY_TOOLS = {
    "bash",  # 执行 shell 命令
    "python",  # 执行 Python 代码
    "write_file",  # 写文件
    "edit_file",  # 修改文件
    "delete_file",  # 删除文件
    "subagent",  # 派遣子 agent
}

# ── 只读/无副作用工具 ──────────────────────────────
# 纯查询、无破坏性：即使 all 模式也自动放行，不打扰
NO_SIDE_EFFECT_TOOLS = {
    "read_file",      # 读文件
    "outline",        # 结构大纲
    "memory_read",    # 读长期记忆
    "task_list",      # 查看任务
    "web_fetch",      # 抓取网页
    "ask_llm",        # 询问在线模型
    "vision",         # 图像识别
    "codegraph",      # 代码关系查询
}

# ── 模式说明 ──────────────────────────────────────
MODE_HELP = (
    "\n  审核模式：\n"
    "    all   — 审核所有有副作用的工具调用（只读查询自动放行）\n"
    "    risky — 只审核危险工具: "
    + ", ".join(sorted(DEFAULT_RISKY_TOOLS))
    + "\n"
    "    off   — 不审核，直接放行\n"
    "\n"
    "  输入 y / all / risky / off 切换模式，Enter 保持当前模式\n"
)

class _NoopLock:
    """拿不到 fp-terminal 输出锁时的降级：无操作，行为等同旧版。

    只用于非 fp-terminal 环境（理论上插件只在终端启用，这是防御性兜底），
    保证 async with 语法一致、绝不崩、绝不阻塞。
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ToolAuditPlugin(Plugin):
    """
    工具审核插件（仅终端）

    配置项（构造参数，均有默认值，可无参实例化）：
      mode: str               审核模式，默认 "all"
      risky_tools: set        危险工具列表，默认 DEFAULT_RISKY_TOOLS
      show_full_args: bool    是否在交互前展示完整参数（默认 True）
      skip_no_side_effect: bool  只读工具（read_file/memory_read 等）
                              是否自动放行（默认 True）
    """

    name = "tool_audit"
    version = "2.3.0"

    def __init__(
        self,
        config: PluginConfig | None = None,
        mode: str = "all",
        risky_tools: set | None = None,
        show_full_args: bool = True,
        skip_no_side_effect: bool = True,
    ):
        if config is None:
            config = PluginConfig(enabled=True, priority=10)
        super().__init__(config)

        self.mode = mode  # "all" | "risky" | "off"
        self.risky_tools = (
            set(risky_tools) if risky_tools else DEFAULT_RISKY_TOOLS.copy()
        )
        self.show_full_args = show_full_args
        self.skip_no_side_effect = skip_no_side_effect

        # 运行态
        self._auto_approve = False  # "a" 后设为 True，本次会话不再询问
        self._lifecycle: LifecycleManager | None = None
        # 并发审核锁：fp-core 用 asyncio.gather 并行执行工具，多个
        # ON_TOOL_CALL 可能同时触发；此锁保证同一时刻只展示/询问一个
        # 工具，其余排队，避免终端输出交叠
        self._audit_lock = asyncio.Lock()

    # ── 注册 / 注销 ─────────────────────────────────────

    def on_register(self, lifecycle: LifecycleManager):
        """注册 ON_TOOL_CALL 钩子"""
        self._lifecycle = lifecycle
        lifecycle.register(
            LifecycleHook.ON_TOOL_CALL,
            self._on_tool_call,
            priority=10,
            name="tool_audit_on_tool_call",
            hook_type="transform",
        )

    def on_unregister(self):
        if self._lifecycle is not None:
            try:
                self._lifecycle.unregister(
                    LifecycleHook.ON_TOOL_CALL, "tool_audit_on_tool_call"
                )
            except Exception:
                pass
            self._lifecycle = None

    # ── 核心：工具调用审核 ───────────────────────────────

    async def _on_tool_call(self, ctx: HookContext, **kwargs):
        """ON_TOOL_CALL 处理函数（transform）

        安全原则：transform 钩子异常会危及主流程。
        这里整体 try/except 兜底——任何异常一律放行，只记日志。

        终端输出协调（解决审核提示与工具输出打架）：
          1. 审核锁（_audit_lock）串行化审核界面：一次只展示/询问一个工具
          2. _flush_pending_outputs()：展示提示前先等上一个工具的输出刷完
          3. 审核提示期间持有 fp-terminal LLMStreamer 的类级输出锁：
             工具输出打印共用这把锁，提示期间输出排队，用户按键后再刷出
        """
        try:
            async with self._audit_lock:
                # 先让上一个工具的输出（tool_call/tool_result）打印完
                await self._flush_pending_outputs()
                # 提示期间独占终端输出锁，输出排队，不再插进提示行
                async with await self._acquire_streamer_lock():
                    await self._on_tool_call_inner(ctx, **kwargs)
        except Exception as e:
            get_logger().warning(f"[tool_audit] 审核异常已忽略（放行）: {e}")

    async def _on_tool_call_inner(self, ctx: HookContext, **kwargs):
        if not self._enabled:
            return

        tool_name = kwargs.get("tool_name", "")
        tool_args_raw = kwargs.get("tool_args", "{}")

        # ── 自动放行 ──
        if self._auto_approve:
            return
        if self.mode == "off":
            return
        if self.mode == "risky" and tool_name not in self.risky_tools:
            return
        # ── 只读/无副作用工具：即使 all 模式也自动放行（不打扰查询） ──
        if self.skip_no_side_effect and tool_name in NO_SIDE_EFFECT_TOOLS:
            get_logger().info(f"[tool_audit] 只读工具 {tool_name} 自动放行")
            return

        # ── 子代理豁免 ──
        if os.environ.get("FP_IS_SUBAGENT") == "1":
            return

        # ── 仅终端：获取当前 IO 通道，非终端一律放行 ──
        io = get_current_io()
        if not self._is_terminal(io):
            cls_name = type(io).__name__ if io is not None else "None"
            get_logger().info(
                f"[tool_audit] 非终端通道({cls_name})，{tool_name} 自动放行"
            )
            return

        # ── 展示（完整参数，不折叠、不截断） ──
        io.info("")
        risky = tool_name in self.risky_tools
        header = (
            f"⚠️  工具审核：{tool_name}（危险操作）"
            if risky
            else f"🔧 工具审核：{tool_name}"
        )
        (io.warning if risky else io.info)(header)

        if self.show_full_args:
            io.info(self._render_args(tool_args_raw))
        io.info("──────────────────────────────────────────────────────────")

        # ── 等待用户决策 ──
        decision = await self._ask_user(io)

        # ── 处理决策 ──
        if decision == "approve":
            return

        if decision == "reject":
            ctx.data["cancelled"] = True
            ctx.data["cancel_reason"] = "用户拒绝了此工具调用"
            io.warning(f"  → ❌ 已拒绝: {tool_name}")
            return

        if decision == "modify":
            modified = await self._ask_modified_args(io, tool_args_raw)
            if modified is not None:
                ctx.data["modified_tool_args"] = modified
                io.info("  → ✏️  参数已修改")
            else:
                # 用户取消修改，按拒绝处理
                ctx.data["cancelled"] = True
                ctx.data["cancel_reason"] = "用户取消了此工具调用"
                io.info(f"  → ❌ 已取消: {tool_name}")
            return

        if decision == "approve_all":
            self._auto_approve = True
            io.info("  → ✅ 全部批准（本次会话内不再询问）")
            return

        if decision == "switch_mode":
            await self._switch_mode(io)
            # 重新判断是否要拦截此调用
            if self.mode == "off" or (
                self.mode == "risky" and tool_name not in self.risky_tools
            ):
                return
            # 否则递归询问
            return await self._on_tool_call_inner(ctx, **kwargs)

    # ── 环境检测 ─────────────────────────────────────

    @staticmethod
    async def _acquire_streamer_lock():
        """获取 fp-terminal LLMStreamer 的类级输出锁（异步锁对象）。

        工具调用/结果展示（LLMStreamer.tool / tool_result_line）共用这把锁
        串行输出；而审核提示走 input()，不参与该锁，所以并行工具的输出会
        插进提示行。这里在审核询问期间持有同一把锁，让输出排队等待，
        用户按键后再按序刷出。

        非 fp-terminal 环境拿不到 → 返回 _NoopLock（不崩、不阻塞）。
        """
        try:
            from fp_cli.display import LLMStreamer

            return LLMStreamer._get_tool_lock()
        except Exception:
            return _NoopLock()

    @staticmethod
    async def _flush_pending_outputs():
        """等上一个工具尚未打印完的输出刷完，再展示审核提示。

        工具输出是异步 fire-and-forget 任务（asyncio.create_task），
        审核锁串行化提示后，上一个工具的输出可能仍在排队打印。
        这里轮询 LLMStreamer 输出锁：无输出在打印、无输出在排队即返回；
        超时（约 2s）兜底放行，不无限等待。
        """
        try:
            from fp_cli.display import LLMStreamer

            lock = LLMStreamer._get_tool_lock()
        except Exception:
            return
        for _ in range(200):
            if not lock.locked():
                waiters = getattr(lock, "_waiters", None)
                if not waiters:
                    return
            await asyncio.sleep(0.01)

    @staticmethod
    def _is_terminal(io) -> bool:
        """判断当前 IO 通道是否为终端（CLIIO）"""
        if io is None:
            return False
        cls_name = type(io).__name__
        # 官方通道：fp-terminal 的 CLIIO
        if cls_name == "CLIIO":
            return True
        # 宽松兼容：类名含 CLI 且实现了 ask() 的通道视为终端
        if "CLI" in cls_name and hasattr(io, "ask"):
            return True
        return False

    # ── 用户交互（通过 IO 通道） ───────────────────────

    @staticmethod
    async def _ask_user(io) -> str:
        """通过 IO 通道等待用户决策"""
        prompt = (
            "  [Y]批准  [n]拒绝  [m]修改参数  "
            "[a]全部批准  [s]设置模式  [h]帮助 > "
        )
        while True:
            raw = (await io.ask(prompt)).strip().lower()

            if raw in ("", "y", "yes"):
                return "approve"
            if raw in ("n", "no", "拒绝"):
                return "reject"
            if raw in ("m", "mod", "修改"):
                return "modify"
            if raw in ("a", "all", "全部"):
                return "approve_all"
            if raw in ("s", "set", "模式"):
                return "switch_mode"
            if raw in ("h", "help", "?"):
                io.info(MODE_HELP)
            # 未知输入 → 继续询问

    @staticmethod
    async def _ask_modified_args(io, original: str) -> str | None:
        """让用户输入修改后的参数 JSON"""
        io.info("  当前参数 (JSON):")
        for line in ToolAuditPlugin._format_args(original).split("\n"):
            io.info(f"    {line}")
        io.info("  输入新的参数 JSON（空行取消）:")

        lines = []
        try:
            while True:
                line = await io.ask("    > ")
                if not line:
                    break
                lines.append(line)
            result = " ".join(lines).strip()
            if not result:
                return None
            # 验证是合法 JSON
            json.loads(result)
            return result
        except json.JSONDecodeError as e:
            io.error(f"  JSON 格式错误: {e}，已取消")
            return None

    async def _switch_mode(self, io):
        """切换审核模式"""
        io.info(MODE_HELP)
        raw = (await io.ask("  模式 > ")).strip().lower()

        if raw == "all":
            self.mode = "all"
            self._auto_approve = False
            io.info("  → ✅ 已切换为 all 模式（审核所有工具）")
        elif raw == "risky":
            self.mode = "risky"
            self._auto_approve = False
            io.info("  → ⚠️  已切换为 risky 模式（仅审核危险工具）")
        elif raw == "off":
            self.mode = "off"
            io.info("  → ⏹️  已切换为 off 模式（不审核）")
        else:
            io.info("  模式未变更")

    # ── 工具函数 ───────────────────────────────────────

    @classmethod
    def _render_args(cls, raw: str) -> str:
        """将工具参数完整美化为对齐格式（不折叠、不截断）

        - 短值（单行）：`    · key: value`
        - 多行值：`    · key:` 换行，内容逐行缩进 8 空格
        - 嵌套结构（dict/list）用 json.dumps(indent=2) 展开
        - 无法解析的原始参数按原样返回
        """
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            text = raw.strip()
            if not text:
                return "    （无参数）"
            return "\n".join(f"    {ln}" for ln in text.splitlines())

        if not isinstance(obj, dict):
            text = json.dumps(obj, indent=2, ensure_ascii=False)
            return "\n".join(f"    {ln}" for ln in text.splitlines())

        if not obj:
            return "    （无参数）"

        lines = []
        for k, v in obj.items():
            # 值文本化：字符串直接取，其他类型美化 JSON
            if isinstance(v, str):
                text = v
            else:
                text = json.dumps(v, indent=2, ensure_ascii=False)
            if "\n" in text:
                lines.append(f"    · {k}:")
                for ln in text.splitlines():
                    lines.append(f"        {ln}")
            else:
                lines.append(f"    · {k}: {text}")
        return "\n".join(lines)

    @staticmethod
    def _format_args(raw: str) -> str:
        """格式化工具参数（美化 JSON，完整展示用）"""
        try:
            obj = json.loads(raw)
            return json.dumps(obj, indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            return raw
