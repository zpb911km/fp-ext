#!/usr/bin/env python3
"""Qwen 安全后端（真实浏览器通道）
============================================================

chat.qwen.ai 从 2026-10 起对 ``/api/v2/chat/completions`` 上了阿里 WAF 风控
（``RGV587_ERROR`` → ``punish?x5secdata=...``）。纯 HTTP 客户端（requests / curl_cffi）
无论怎么补 cookie 与请求头都过不去；**只有真实浏览器**能过——而且必须是
**有头渲染**（headless 拿不到屏幕/渲染器，bx 挑战跑不完，页面永远停在
``response-loading``；headed 下第一次请求被 punish，前端 JS 会自动接住并重发，
第二次即成功）。

所以本后端与 ``deepseek_safe_backend`` 同构：**只操作真实浏览器的 DOM**，
不伪造任何 HTTP 请求。

用法::

    python3 backend.py status            # daemon/Xvfb/profile 状态
    python3 backend.py probe             # 打开会话自检（不发消息）
    python3 backend.py ask "问题"        # 提问 → JSON {answer, thinking, references, elapsed}
    python3 backend.py ask "问题" --search --new-chat --files a.png
    python3 backend.py stop              # 停止 daemon

daemon 常驻（unix socket ``qwen_backend.sock``），浏览器会话跨提问复用。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

try:  # 与 qwen_login.py 同源的取数逻辑
    from fp_core.platform_utils import get_data_dir  # type: ignore

    DATA = Path(get_data_dir())
except Exception:  # noqa: BLE001
    DATA = Path.home() / ".local" / "share" / "fp"

QWEN_DIR = DATA / "webai" / "qwen"
PROFILE = QWEN_DIR / "profile"
SOCK = HERE / "qwen_backend.sock"
DAEMON_LOG = HERE / "daemon.log"
ASKS_LOG = HERE / "asks.log"
STATE = HERE / "state.json"
SHOTS = HERE / "shots"

BASE = "https://chat.qwen.ai"

ASK_TIMEOUT = 300          # 单问等待上限(秒)
IDLE_SEC = 3600            # 无操作自动关闭(秒)
XVFB_DISPLAY = ":99"
XVFB_SCREEN = "1920x1080x24"
XVFB_PIDFILE = HERE / "xvfb.pid"
STABLE_POLLS = 3           # 连续 N 次文本不变才算完（仅在观测不到流事件时兜底用）
POLL_EVERY = 2.0           # 轮询间隔(秒)
# ── 完成判定的肯定式信号（2026-11-27 重做 = 反思58 的根治）─────────────────
# 旧判定是纯否定式的："连续 3 次文本不变 + 无 .response-loading ≈ 6s" 就收工。
# 长代码/长文流式生成里 ≥6s 的间隙会被当成结束 → **截断的正文被当最终答案**返回并
# 落进会话历史（反思58 实证：模型自报 402 行，返回只有 31 行）。
# 真正的肯定式信号在网络层：SSE `POST /api/v2/chat/completions` 的 requestfinished
# = 服务端把正文流写完了（实测 2026-11-27：流结束时刻与页面文本停增时刻一致）。
# 故判定 = 流结束 + DOM 再稳 SETTLE_POLLS 轮；只有观测不到流事件(页面改版)才退回
# 稳定窗口兜底，且兜底窗口放大到 FALLBACK_STABLE_POLLS（比旧 3 轮保守）。
STREAM_URL_KEY = "/chat/completions"
SETTLE_POLLS = 1                 # 流结束后再多稳几轮（等图表/产物渲染落定）
FALLBACK_STABLE_POLLS = 5        # 兜底：未观测到流事件时 N 次不变才算完（≈10s）
STREAM_PROBE_SEC = 15            # 发出后多久仍没看到流请求 → 视为端点不可观测
PASTE_THRESHOLD = 600      # 超过这么多字改用粘贴式 fill：逐字敲 18~45ms/字
                           # 会在 ~900 字处撞 Playwright 默认 30s 动作超时

SEL_INPUT = "textarea.message-input-textarea"
SEL_ASSISTANT = ".qwen-chat-message-assistant"
SEL_ANSWER = ".qwen-markdown"
SEL_LOADING = ".response-loading"
SEL_NEWCHAT = "button:has-text('新建对话'), [role=button]:has-text('新建对话')"

# ── 功能入口（2026-10-19 逆向探测实测，报告在 /tmp/qshots/report*.json）──
SEL_MODEL_TRIGGER = ".wms-trigger"          # 左上角模型下拉（aria-label=Select Model）
SEL_MODEL_ITEM = ".wms-list__item"          # 下拉里 role=option 条目
SEL_MODEL_NAME = ".wms-list__name-text"     # 条目内的模型名
SEL_MODEL_SELECTED = ".wms-list__item--selected"
SEL_MODEL_MORE = ".wms-more-entry"          # 一级菜单底部「展开更多模型」（hover 出二级）
SEL_MODEL_SECONDARY = ".wms-popup--secondary"  # hover 展开后在右侧浮出的二级面板
SEL_MODEL_PIN = ".wms-list__pin-action"     # 条目内「设为默认」（= 账号级默认模型）
SEL_PLUS = "div.mode-select-open"           # 输入框加号（aria-label=选择模式）
SEL_MENU_ITEM = ".qwen-chat-v2-dropdown-menu-item"      # 通用菜单条目（精确 class token）
SEL_MENU_CHECK = ".qwen-chat-v2-dropdown-menu-item-check"  # 条目选中态标记（仅 toggle 类条目）
SEL_MENU_SUB = ".qwen-chat-v2-dropdown-menu-item-has-submenu"  # 「更多」等 hover 出子菜单的条目
SEL_THINK_TRIGGER = ".qwen-chat-v2-dropdown-menu-select"   # 输入区「自动/思考/快速」下拉
SEL_CFG_TRIGGER = ".qwen-chat-v2-dropdown-menu-select"     # 输入区下拉（思考档 + 模式子配置共用）
SEL_MODE_CHIP = ".mode-select-current-mode"          # 已选模式的芯片（唯一可靠的模式状态源）
SEL_MODE_CHIP_CLOSE = ".mode-select-current-mode-close"  # 芯片上的 X（hover 芯片才渲染）
SEL_FILE_CHIP = ".message-input-column-file .file-card-list"  # 其子元素 = 每个附件卡片
SEL_FILE_REMOVE = "button.close-button"     # 芯片删除（aria-label=删除文件）
# 创建视频的**参数弹层**（2026-10-19 实测：不是下拉，是独立 ant-popover）
#   入口芯片 .video-params-summary（文案就是摘要「720P · 5s」）
#   弹层 .video-params-popover > .video-params-group > [role=radio][aria-checked]
#   四组：清晰度(720P/1080P+VIP) · 视频编辑模式(视频生成/多参考图) · 比例(6 档) · 时长(5s/10s/15s)
SEL_VIDEO_SUMMARY = ".video-params-summary"

# 加号菜单的模式条目：能力名 → 页面文本。前五个对应旧 API 通道的 chat_type；
# 后三个（preview/study/travel）藏在「更多」二级菜单里，是旧通道没有的新能力。
# 选模式 → 输入区出现模式芯片（.mode-select-current-mode），换模式直接点另一个，
# 退回普通对话 = 点芯片上的 X 或新建对话（new_chat 会重置为普通对话）。
PLUS_MODES = {
    "t2i": "生成图像", "t2v": "创建视频", "slides": "幻灯片",
    "web_dev": "网页开发", "deep_research": "深入研究",
    "preview": "预览模式", "study": "学习", "travel": "旅行规划师",
    "upload": "上传附件", "search": "网页搜索",
}
# 藏在「更多」二级菜单里的条目：点之前必须先 hover 一级菜单的「更多」
SUBMENU_MODES = {"preview", "study", "travel"}
# 非模式条目（点了不产生模式芯片）：上传附件是 filechooser 入口，工具是开关
NON_MODE_ITEMS = {"upload", "tools", "more"}
# 各模式的子配置下拉标签（页面把它们渲染成输入区里的一组 .qwen-chat-v2-dropdown-menu-select）。
# 用途：read_config() 如实汇报当前取值，set_config() 按键名改（比例/分辨率/研究深度…）。
# ⚠️ 2026-10-19 复测修正：t2v 的「清晰度/时长/比例/编辑模式」**不是**这类下拉，
# 而是独立弹层 .video-params-popover（见 read_video_params / set_video_params）。
MODE_CONFIG_KEYS = {
    "t2i": ("模型", "比例"),      # 生成图像：模型(Qwen-Image 3.0/2.0) + 比例(7 档)
    "t2v": ("模型",),             # 创建视频：模型在输入条上，其余四项走 video params 弹层
    "deep_research": ("深度",),   # 深入研究：正常 / 高级
}
# 输入区下拉的**语义键**：按文案猜（模型名/VIP 档 → model；N:M 或「原比例」→ ratio；
# 正常|高级 → depth；其余 → 序号名）。
_RATIO_RE = None  # 惰性编译（模块级 re 已 import）
VIDEO_PARAM_KEYS = {          # 视频参数弹层的组标题 → 语义键
    "清晰度": "clarity", "视频编辑模式": "vmode",
    "比例": "ratio", "时长": "duration",
}
VIDEO_KEY_ALIAS = {           # 调用方可能用别的说法
    "分辨率": "clarity", "清晰度": "clarity", "resolution": "clarity",
    "编辑模式": "vmode", "模式": "vmode", "video_mode": "vmode",
    "画幅": "ratio", "aspect": "ratio",
    "长度": "duration", "秒数": "duration", "seconds": "duration",
}


def _cfg_key(label: str) -> str:
    """输入区下拉的语义键（页面不给 key，只能按取值形态认）。"""
    t = (label or "").strip()
    if not t:
        return "cfg"
    if ":" in t and all(c.isdigit() or c == ":" for c in t):
        return "ratio"
    if t in ("原比例", "自适应"):
        return "ratio"
    if t in ("正常", "高级"):
        return "depth"
    if "Image" in t or "HappyHorse" in t or t.startswith("Qwen"):
        return "model"
    return "cfg"
# 思考下拉三档；THINK_ALIASES 收旧调用方的方言值。
# ⚠️ 实测：思考档是**页面级**设置（存 localStorage），新建对话不会重置。
THINK_LABELS = {"auto": "自动", "thinking": "思考", "fast": "快速"}
_THINK_ALIAS = {"1": "thinking", "0": "auto", "true": "thinking",
                "false": "auto", "思考": "thinking", "自动": "auto",
                "快速": "fast", "深度思考": "thinking"}

# 长耗时模式（生成类）：默认超时放宽
SLOW_MODES = {"t2v": 900, "slides": 900, "deep_research": 1200,
              "t2i": 420, "web_dev": 600, "preview": 420}

# 风控指纹：WAF 挑战页/拦截体里出现的标记（响应体）
_WAF_MARKS = ("RGV587", "FAIL_SYS_USER_VALIDATE", "x5secdata", "punish")
# 风控指纹：人机验证页面的 DOM/文案标记（命中即立刻停手，不再发任何动作）
_RISK_TEXT = ("人机验证", "滑动验证", "拖动滑块", "安全验证", "请完成验证",
              "哎哟喂", "访问受限", "环境异常", "验证码")
_RISK_SEL = "[class*=captcha],[id*=captcha],iframe[src*=captcha],[id*=punish]," \
            "[class*=verify-slider],[class*=nc_iconfont]"


# ---------------- 日志 ----------------


def log(msg: str) -> None:
    if os.environ.get("QWEN_QUIET"):
        return  # 调用方(插件)要纯净 JSON, 不混日志
    print(f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def audit(rec: dict) -> None:
    rec["ts"] = dt.datetime.now().isoformat(timespec="seconds")
    try:
        with ASKS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {"last_ask": 0.0, "count": 0}


def _save_state(st: dict) -> None:
    try:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    except Exception:  # noqa: BLE001
        pass


# ---------------- 浏览器会话 ----------------


class BrowserBackend:
    def __init__(self):
        from playwright.sync_api import sync_playwright

        if not PROFILE.exists():
            raise RuntimeError(
                f"找不到登录 profile: {PROFILE}\n"
                "请先跑：python3 <fp数据目录>/public/plugins/webai/lib/qwen_login.py"
            )
        # 虚拟屏自愈: Xvfb 掉了就按需重启(重启者即持有者)。
        # ⚠️ 必须有头(headed)：headless 下 WAF 挑战跑不完, 页面永远加载中。
        if not _xvfb_alive():
            if _ensure_xvfb():
                os.environ["QWEN_XVFB_OWNED"] = "1"
        os.environ["DISPLAY"] = XVFB_DISPLAY
        # ⚠️ KDE/Wayland 会话里 WAYLAND_DISPLAY 会被继承下来, chromium 优先走
        # wayland ozone → 连不上 Xvfb(:99) → 进程秒退, playwright 只报
        # "Target page, context or browser has been closed"(2026-10-18 实测根因)。
        os.environ.pop("WAYLAND_DISPLAY", None)

        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            args=[
                # KDE/Wayland 会话: chrome 见 WAYLAND_DISPLAY/XDG_SESSION_TYPE=wayland
                # 就去连 wayland(虚拟屏 :99 上没有) → 秒退。钉死 x11 ozone 后端。
                "--ozone-platform=x11",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            viewport={"width": 1300, "height": 880},
            locale="zh-CN",
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        for pg in self.ctx.pages[1:]:
            pg.close()
        # 风控观察器：记下 completions 的响应体, 供超时时诊断
        self._waf_hits: list[str] = []
        self._ok_seen = False
        self.ctx.on("response", self._on_response)
        # 流式完成观察器：只记时间戳，**不在回调里做 IO**（回调在事件泵线程里跑）。
        self._stream_reqs: list[float] = []   # completions 请求开始时刻
        self._stream_done: list[float] = []   # completions 响应结束时刻（= 服务端写完正文）
        # 端点自我发现：记下所有 POST 路径（只记路径不带 query）。
        # 流端点一改版，"观测不到流事件"就变成一个没法定位的黑洞（2026-11-27 实测：
        # 日志反复报"未观测到 completions 流事件"，但没人知道页面到底请求了哪个 URL）。
        # 有了这份清单，下次改版能直接从日志读出真端点。
        self._post_paths: list[str] = []
        self.page.on("request", self._rec_post_path)
        self.page.on("request", self._on_stream_request)
        self.page.on("requestfinished", self._on_stream_finished)
        self.page.on("requestfailed", self._on_stream_finished)
        self.page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(4000)
        # 页面配置缓存：菜单项多为持久开关，期望态与缓存不同才动手（避免误反选）。
        # search=None 表示「没读过状态」；new_chat() 会重置缓存。
        self._applied: dict = {"model": "", "think": "", "search": None, "mode": ""}
        self._models_cache: list = []

    # -- 响应观察 -------------------------------------------------

    def _on_response(self, r) -> None:
        try:
            if "/chat/completions" not in r.url:
                return
            body = r.text()[:400]
        except Exception:  # noqa: BLE001
            return
        if any(m in body for m in _WAF_MARKS):
            self._waf_hits.append(body[:200])
        elif "response.created" in body or "choices" in body:
            self._ok_seen = True

    # -- 流式完成观察器（肯定式完成信号，见文件头 STREAM_URL_KEY 注释）------

    def _rec_post_path(self, req) -> None:
        """记 POST 路径（去 query、去域名），用于诊断流端点改版。回调里不做 IO。"""
        try:
            if req.method != "POST":
                return
            u = req.url.split("?")[0]
            if u not in self._post_paths:
                self._post_paths.append(u)
                del self._post_paths[:-40]
        except Exception:  # noqa: BLE001
            pass

    def _post_paths_tail(self, n: int = 8) -> str:
        return " | ".join(p.split("//")[-1][:90] for p in self._post_paths[-n:])

    @staticmethod
    def _is_stream(req) -> bool:
        try:
            return req.method == "POST" and STREAM_URL_KEY in req.url
        except Exception:  # noqa: BLE001
            return False

    def _on_stream_request(self, req) -> None:
        if self._is_stream(req):
            self._stream_reqs.append(time.time())

    def _on_stream_finished(self, req) -> None:
        if self._is_stream(req):
            self._stream_done.append(time.time())

    def _stream_reset(self) -> None:
        self._stream_reqs.clear()
        self._stream_done.clear()

    def _stream_seen(self, since: float) -> bool:
        return any(t >= since for t in self._stream_reqs)

    def _stream_finished(self, since: float) -> bool:
        return any(t >= since for t in self._stream_done)

    @staticmethod
    def _notice_error(notice: str, where: str) -> RuntimeError:
        """对话框里的系统提示 → 立刻抛（不再空转到 ASK_TIMEOUT）。

        实测样本（2026-11-27）：`Qwen4-Max 今日已达上限，请升级会员或在上方切换其他
        模型继续使用。` 渲染在 assistant 气泡里的 `<section class="exceed-limit-error-card">`，
        **没有 .qwen-markdown**，旧逻辑认为"还没有正文"，一路等到 300s 报"未观测到新文本"
        —— 把配额耗尽的真因伪装成"页面没反应"。
        """
        t = (notice or "").strip()
        low = t.lower()
        if any(k in t for k in ("上限", "额度", "升级会员", "积分", "次数", "配额")) \
                or "quota" in low or "limit" in low:
            return RuntimeError(f"qwen 额度/次数已用完（quota）：{t}")
        if any(k in t for k in ("登录", "失效", "过期")) or "login" in low:
            return RuntimeError(f"qwen 登录态失效（auth）：{t}")
        if any(k in t for k in ("繁忙", "稍后", "失败", "重试", "网络", "繁忙")) \
                or "busy" in low or "retry" in low:
            return RuntimeError(f"qwen 服务端异常（transient）：{t}")
        return RuntimeError(f"qwen 对话系统提示（{where}）：{t}")

    # -- 自检 -----------------------------------------------------

    def probe(self) -> dict:
        risk = self.risk_state()
        st = {
            "url": self.page.url,
            "logged_in": bool(self._user_token()),
            "input_box": self.page.locator(SEL_INPUT).count() > 0,
            "model": self.current_model(),
            "mode": self.current_mode(),
            "configs": [c.get("label", "") for c in self.read_config()],
            "thinking": self._think_label(),
            # 视频参数摘要（不在创建视频模式时为空）
            "video_summary": self.video_params_summary(),
            # 风控自保：命中人机验证时 supported 全 False，调用方一眼看出不能干活
            "risk": risk.get("marks") or ([risk["el"]] if risk.get("el") else []),
        }
        SHOTS.mkdir(exist_ok=True)
        p = SHOTS / f"probe_{int(time.time())}.png"
        try:
            self.page.screenshot(path=str(p))
            st["screenshot"] = str(p)
        except Exception:  # noqa: BLE001
            pass
        return st

    def _think_label(self) -> str:
        try:
            return self.page.evaluate(
                """() => {
                  for (const e of document.querySelectorAll('.qwen-chat-v2-dropdown-menu-select')) {
                    const r = e.getBoundingClientRect();
                    const t = (e.innerText || '').trim();
                    if (r.width > 0 && ['自动', '思考', '快速'].includes(t)) return t;
                  }
                  return '';
                }"""
            ) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _user_token(self) -> str:
        try:
            return (self.page.evaluate("() => localStorage.getItem('token')") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _model_label(self) -> str:
        try:
            return self.page.evaluate(
                """() => {
                  const el = document.querySelector('.qwen-chat-v2-dropdown-menu-select-button')
                          || document.querySelector('[class*=model-select]');
                  return el ? (el.innerText || '').trim().slice(0, 40) : '';
                }"""
            )
        except Exception:  # noqa: BLE001
            return ""

    # -- 新对话 ---------------------------------------------------

    def new_chat(self) -> None:
        self._waf_hits.clear()
        self._ok_seen = False
        self._models_cache = []          # 模型清单与对话无关，但下拉可能因登录态变化
        opened = False
        try:
            btn = self.page.locator(SEL_NEWCHAT).first
            if btn.count():
                btn.click(timeout=15000)
                self._jitter(1500, 2400)
                opened = True
        except Exception:  # noqa: BLE001
            pass
        if not opened:
            # 兜底：直接回首页（等价于新对话）
            self.page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(3000)
        else:
            # 校验"真的切了"：点了没生效仍留在原对话 → 下一问的读数会被旧回答遮蔽
            # （deepseek 家 2026-11-26 就是这么假超时的，症状完全一样）。
            try:
                self.page.wait_for_function(
                    "() => document.querySelectorAll('.qwen-chat-message-assistant')"
                    ".length === 0", timeout=8000)
            except Exception:  # noqa: BLE001
                log("⚠️ 新对话未生效（页面仍有历史消息），回首页兜底")
                self.page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
                self.page.wait_for_timeout(3000)
        # 新对话 = 页面回默认态：模式/联网归零（页面上模式芯片消失）。
        # ⚠️ 实测：模型选择与思考档**不随新对话重置**（页面级偏好，存 localStorage）。
        self._applied = {**self._applied, "mode": "", "search": None}
        if self.current_mode():
            log(f"⚠️ 新对话后仍有模式芯片 {self.current_mode()!r}（页面改版？）")

    # -- 页面配置驱动（模型 / 思考 / 加号菜单）───────────────────

    @staticmethod
    def _norm(s: str) -> str:
        """归一化匹配键：小写 + 只留字母数字。'Qwen3.7-Plus' 与 'qwen3.7-plus'
        （旧 API 模型 id）都会归到 qwen37plus。"""
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())

    def _jitter(self, lo=700, hi=1500):
        self.page.wait_for_timeout(random.randint(lo, hi))

    # -- 风控自保 -------------------------------------------------
    #
    # 一旦页面出现人机验证，**立刻停手**：不再点、不再发、不再重试。
    # 这是硬约束（反复试探只会把账号推得更深），调用方会收到一条说人话的错误。

    def risk_state(self) -> dict:
        try:
            return self.page.evaluate(
                """() => {
                  const txt = (document.body?.innerText || '').slice(0, 5000);
                  const marks = %s;
                  const hit = marks.filter(m => txt.includes(m));
                  const el = document.querySelector(%s);
                  return {marks: hit, el: el ? (el.className || el.id || el.tagName) : '',
                          url: location.href};
                }""" % (json.dumps(list(_RISK_TEXT), ensure_ascii=False),
                         json.dumps(_RISK_SEL)),
            ) or {}
        except Exception:  # noqa: BLE001
            return {}

    def guard(self, where: str = "") -> None:
        """有验证就抛错（不再做任何动作），无声无息就放过。"""
        st = self.risk_state()
        if st.get("marks") or st.get("el"):
            self._waf_hits.append(f"dom:{where}:{(st.get('marks') or [st.get('el')])[0]}")
            SHOTS.mkdir(exist_ok=True)
            p = SHOTS / f"risk_{int(time.time())}.png"
            try:
                self.page.screenshot(path=str(p))
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                "qwen 页面出现人机验证（%s）——按纪律立即停手，"
                "请在浏览器里人工过一次验证后再重试。截图: %s"
                % ("/".join(st.get("marks") or [st.get("el") or "?"]), p))

    def _hover(self, loc, pad: float = 0.5) -> None:
        box = loc.bounding_box()
        if not box:
            return
        self.page.mouse.move(box["x"] + box["width"] * random.uniform(0.3, 0.7),
                             box["y"] + box["height"] * random.uniform(pad - 0.15, pad + 0.15),
                             steps=random.randint(8, 18))
        self._jitter(600, 1100)

    def _click_js(self, text: str, scope: str = "") -> str:
        """按条目首行文本点菜单项（先点可见的、含子菜单展开后的）。返回点到的文本。"""
        return self.page.evaluate(
            """([want, scope]) => {
              const root = scope ? document.querySelector(scope) : document;
              if (!root) return '';
              const hit = [...root.querySelectorAll('.qwen-chat-v2-dropdown-menu-item')]
                .filter(e => e.getBoundingClientRect().width > 0);
              // 精确匹配优先，其次前缀（条目文案常带描述换行）
              for (const e of hit) {
                if ((e.innerText || '').trim().split('\\n')[0] === want) { e.click(); return want; }
              }
              for (const e of hit) {
                if ((e.innerText || '').trim().startsWith(want)) { e.click(); return want; }
              }
              return '';
            }""", [text, scope]) or ""

    def _click_safe(self, loc) -> bool:
        """点一个可能被浮层（如「创作额度」弹层）挡住的元素：失败就 Escape 后重试一次。"""
        self.guard("click")
        try:
            loc.click(timeout=8000)
            return True
        except Exception as e:  # noqa: BLE001
            log(f"click retry after overlay: {str(e)[:80]}")
        self.page.keyboard.press("Escape")
        self._jitter(500, 900)
        try:
            loc.click(timeout=8000, force=True)
            return True
        except Exception as e:  # noqa: BLE001
            log(f"forced click failed: {str(e)[:80]}")
            return False

    def current_model(self) -> str:
        try:
            el = self.page.locator(SEL_MODEL_TRIGGER).first
            if el.count():
                return (el.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        return self._model_label()

    def _model_menu_open(self) -> bool:
        trig = self.page.locator(SEL_MODEL_TRIGGER).first
        if trig.count() == 0:
            return False
        if (trig.get_attribute("aria-expanded") or "").lower() == "true":
            return True
        if not self._click_safe(trig):
            return False
        self._jitter(900, 1600)
        return True

    def _read_model_items(self, secondary: bool = False) -> list:
        scope = ".wms-popup--secondary" if secondary else ".wms-popup:not(.wms-popup--secondary)"
        return self.page.evaluate(
            """([scope, tier]) => [...document.querySelectorAll(scope + ' .wms-list__item')]
                 .map(e => ({
                   name: (e.querySelector('.wms-list__name-text')?.innerText || '').trim(),
                   desc: (e.querySelector('.wms-list__desc')?.innerText || '').trim().slice(0, 160),
                   selected: e.className.split(' ').includes('wms-list__item--selected'),
                   pin: (e.querySelector('.wms-list__pin-action')?.innerText || '').trim(),
                   tier: tier
                 }))""", [scope, 2 if secondary else 1]) or []

    def list_models(self, force: bool = False) -> list:
        """读左上角模型下拉的全部条目（**多级**：一级 3 个 + 「展开更多模型」hover 出的二级）。

        读完关菜单。结果缓存（force=True 重读）。
        """
        if self._models_cache and not force:
            return self._models_cache
        if not self._model_menu_open():
            return []
        self.guard("models")
        raw = self._read_model_items()
        more = self.page.locator(SEL_MODEL_MORE).first
        if more.count():
            self._hover(more)
            self.guard("models:more")
            raw += self._read_model_items(secondary=True)
        self.page.keyboard.press("Escape")
        self._jitter(400, 800)
        out, seen = [], set()
        for it in raw:
            n = it.get("name") or ""
            if not n or n in seen:
                continue
            seen.add(n)
            out.append({"id": n, "name": n, "desc": it.get("desc", ""),
                        "capabilities": set(), "ctx": 0,
                        # default = 当前对话正在用的；pin_action = 页面「设为默认」入口文案
                        # （实测：没钉住的条目都写「设为默认」，钉住的会换成别的字）
                        "default": bool(it.get("selected")),
                        "selected": bool(it.get("selected")),
                        "pin_action": it.get("pin") or "",
                        "tier": it.get("tier", 1)})
        self._models_cache = out
        return out

    def _pick_model(self, want_n: str) -> str:
        """在已打开的模型菜单里按归一化名点条目（含 hover 出二级）。返回点到的页面名。"""
        js = """(wantN) => {
          const norm = t => [...(t || '').toLowerCase()].filter(c => /[a-z0-9]/.test(c)).join('');
          const items = [...document.querySelectorAll('.wms-popup .wms-list__item')]
              .filter(e => e.getBoundingClientRect().width > 0);
          for (const pass of [0, 1]) {
            for (const e of items) {
              const t = (e.querySelector('.wms-list__name-text')?.innerText || '').trim();
              const n = norm(t);
              if (!n) continue;
              const hit = pass === 0 ? n === wantN : (n.includes(wantN) || wantN.includes(n));
              if (hit) { e.click(); return t; }
            }
          }
          return '';
        }"""
        got = self.page.evaluate(js, want_n) or ""
        if not got:
            more = self.page.locator(SEL_MODEL_MORE).first
            if more.count():
                self._hover(more)
                self.guard("model:more")
                got = self.page.evaluate(js, want_n) or ""
        return got

    def set_model(self, want: str) -> str:
        """切换左上角模型。want 可为页面名/旧 API id（模糊匹配，两级菜单都找）。"""
        want_n = self._norm(want)
        if not want_n:
            return self.current_model()
        cur = self.current_model()
        if self._norm(cur) == want_n:
            self._applied["model"] = cur
            return cur
        if not self._model_menu_open():
            raise RuntimeError("找不到模型下拉触发器（页面改版？）")
        self.guard("set_model")
        got = self._pick_model(want_n)
        if not got:
            self.page.keyboard.press("Escape")
            names = [m["name"] for m in self.list_models(force=True)]
            raise RuntimeError(f"模型 {want!r} 不在页面可选清单: {names}")
        self._jitter(900, 1700)
        now = self.current_model()
        if self._norm(now) != self._norm(got):
            self.page.keyboard.press("Escape")
            raise RuntimeError(f"切换模型失败：点了 {got!r}，当前仍是 {now!r}")
        self._applied["model"] = now
        log(f"model -> {now}")
        return now

    def pin_model(self, want: str) -> str:
        """把某个模型「设为默认」（账号级偏好，页面 .wms-list__pin-action）。

        ⚠️ 这是**改动账号设置**的显式动作，只在调用方明确要求时才做。
        """
        want_n = self._norm(want)
        if not want_n:
            raise RuntimeError("pin_model() 需要模型名")
        if not self._model_menu_open():
            raise RuntimeError("找不到模型下拉触发器（页面改版？）")
        self.guard("pin_model")
        js = """(wantN) => {
          const norm = t => [...(t || '').toLowerCase()].filter(c => /[a-z0-9]/.test(c)).join('');
          for (const e of [...document.querySelectorAll('.wms-popup .wms-list__item')]
                 .filter(e => e.getBoundingClientRect().width > 0)) {
            const t = (e.querySelector('.wms-list__name-text')?.innerText || '').trim();
            const n = norm(t);
            if (!n) continue;
            if (n === wantN || n.includes(wantN) || wantN.includes(n)) {
              const pin = e.querySelector('.wms-list__pin-action');
              if (!pin) return 'already';
              pin.click(); return t;
            }
          }
          return '';
        }"""
        got = self.page.evaluate(js, want_n) or ""
        if not got:
            more = self.page.locator(SEL_MODEL_MORE).first
            if more.count():
                self._hover(more)
                got = self.page.evaluate(js, want_n) or ""
        self.page.keyboard.press("Escape")
        self._jitter(400, 800)
        if got == "already":
            return "already"
        if not got:
            raise RuntimeError(f"设为默认失败：菜单里找不到模型 {want!r} 的「设为默认」")
        self._models_cache = []
        log(f"default model pinned -> {got}")
        return got

    def _align_to_pinned_default(self) -> None:
        """新对话未显式指定模型时：把页面左上角下拉强制对齐到账号级 pin（「取消默认」标记项）。

        2026-11-27 实测页面行为：新对话的初始模型**不确定**——``new_chat()`` 有两条
        路径（点「新对话」按钮 / goto 回首页），左上角下拉是页面级偏好（localStorage，
        见 new_chat 注释），只在整页加载时才吃到账号 pin。10 分钟内三个新建对话两种
        结果（一个 4-Max、两个 3.7-Plus）；当时 4-Max 恰好无额度 → 不强制对齐的话，
        新对话提问会随机死于 quota，且报错看起来像「后端坏了」。

        set_model 是已验证可靠的「要哪个模型就给哪个」手段（点→复读复核，不会假成功），
        故发送前用它对齐。注意这**不是** best-effort：账号一旦有 pin，对齐是强制的——
        静默沿用不可控的页面默认等于把用户送回随机 quota 的陷阱。仅当清单读成功但
        账号确实无 pin（没有「取消默认」条目）时，才不碰页面、跟账号默认走。
        """
        # 新对话刚落地，下拉/菜单可能还没渲染：重试读到非空为止
        items, last_err = [], ""
        for attempt in range(5):
            try:
                items = self.list_models(force=True)
                if items:
                    break
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
            log(f"⚠️ align-pin: 模型清单未就绪 #{attempt + 1}")
            self._jitter(1200, 2200)
        pinned = next((it for it in items
                       if "取消默认" in (it.get("pin_action") or "")), None)
        if not pinned:
            if items:
                return  # 清单可读且账号没有 pin → 跟页面(账号默认)走
            raise RuntimeError(
                "新对话对齐失败：反复读取模型下拉清单为空"
                f"（页面未就绪或选择器变更）{('；' + last_err[:80]) if last_err else ''}")
        want = pinned["name"]
        if self._norm(want) == self._norm(self.current_model() or ""):
            return  # 页面已在 pin 上 → 不动
        last_err = ""
        for attempt in range(3):
            try:
                self.set_model(want)
                log(f"new-chat model aligned -> {want}")
                return
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                log(f"⚠️ align-pin: 第 {attempt + 1} 次尝试失败 {e}")
                self._jitter(800, 1600)
        raise RuntimeError(
            f"新对话模型对齐失败（账号 pin={want!r}，页面当前={self.current_model()!r}）: {last_err}")

    def set_thinking(self, want: str) -> str:
        """驱动输入区「自动/思考/快速」下拉。want: auto|thinking|fast 或中文档名。"""
        key = _THINK_ALIAS.get((want or "").lower(), (want or "").lower())
        label = THINK_LABELS.get(key, want if want in ("自动", "思考", "快速") else "")
        if not label:
            raise RuntimeError(f"未知思考档位 {want!r}（可用 auto/thinking/fast）")
        if self._applied.get("think") == label:
            return label
        trig = None
        loc = self.page.locator(SEL_THINK_TRIGGER)
        for i in range(loc.count()):
            t = (loc.nth(i).inner_text() or "").strip()
            if t in ("自动", "思考", "快速"):
                trig = loc.nth(i)
                self._applied["think"] = t
                break
        if trig is None:
            # 实测：生成类模式（生成图像/创建视频…）把输入区换成模式参数条，
            # 「自动/思考/快速」下拉**不渲染** —— 思考档只在普通对话提供。
            cur = self.current_mode()
            raise RuntimeError(
                "找不到思考档位下拉（页面改版？）" if not cur else
                f"当前模式 {cur!r} 没有思考档下拉——思考档只在普通对话提供，"
                f"先 set_mode('') 退出模式再设")
        if self._applied.get("think") == label:
            return label
        trig.click()
        self._jitter(700, 1300)
        got = self.page.evaluate(
            """(want) => {
              for (const e of document.querySelectorAll('.qwen-chat-v2-dropdown-menu-item')) {
                if (!e.className.split(' ').includes('qwen-chat-v2-dropdown-menu-item')) continue;
                const t = (e.innerText || '').trim();
                if (t === want) { e.click(); return t; }
              }
              return '';
            }""", label)
        self._jitter(600, 1100)
        if got != label:
            self.page.keyboard.press("Escape")
            raise RuntimeError(f"切换思考档位失败：目标 {label!r}")
        self._applied["think"] = label
        log(f"thinking -> {label}")
        return label

    def _plus_open(self) -> None:
        """（兼容旧调用）打开加号菜单 —— 实现见 _open_plus_menu（带浮层重试）。"""
        self._open_plus_menu()

    # -- 模式（加号菜单）------------------------------------------
    #
    # 2026-10-19 实测语义（关键，别再猜）：
    #   * 每个模式选中后 → 输入区出现芯片 `.mode-select-current-mode`（文案=模式名），
    #     这是**唯一可靠的模式状态源**；菜单条目本身带不带 check 都说明不了问题。
    #   * 换模式 = 直接再点另一个模式条目（不需要先退出）。
    #   * 退出模式 = hover 芯片 → 点芯片上的 X（`.mode-select-current-mode-close`，
    #     不 hover 时不渲染）；或者新建对话（页面回默认态）。
    #   * 「网页搜索」不是 toggle 而是**模式**；「工具」不是模式（点了不产生芯片）。

    def current_mode(self) -> str:
        try:
            return self.page.evaluate(
                "() => (document.querySelector('.mode-select-current-mode')?.innerText || '').trim()"
            ) or ""
        except Exception:  # noqa: BLE001
            return ""

    def exit_mode(self) -> str:
        """退出当前模式（点芯片上的 X）。返回退出前的模式名。"""
        cur = self.current_mode()
        if not cur:
            return ""
        chip = self.page.locator(SEL_MODE_CHIP).first
        self._hover(chip)                      # X 只有 hover 才渲染
        closed = self.page.evaluate(
            """() => {const b = document.querySelector('.mode-select-current-mode-close');
                      if (!b) return false; b.click(); return true;}"""
        )
        if not closed:
            self.page.keyboard.press("Escape")
            raise RuntimeError(f"退出模式失败：芯片 {cur!r} 上找不到关闭钮（页面改版？）")
        self._jitter(900, 1600)
        self._applied["mode"] = ""
        self._applied["search"] = None
        log(f"mode exit <- {cur}")
        return cur

    def _open_plus_menu(self) -> None:
        btn = self.page.locator(SEL_PLUS).first
        if btn.count() == 0:
            raise RuntimeError("找不到输入框加号按钮（.mode-select-open，页面改版？）")
        if not self._click_safe(btn):
            raise RuntimeError("加号按钮被浮层挡住，点不动（页面改版？）")
        self._jitter(900, 1500)

    def _open_submenu(self) -> bool:
        """让加号菜单里的「更多」hover 出二级浮层（`.mode-select-sub-popover-container`）。"""
        more = self.page.locator(SEL_MENU_SUB).first
        if more.count() == 0:
            return False
        for _ in range(3):
            self._hover(more)
            self.guard("submenu")
            # ⚠️ 容器一直在 DOM 里（display:none），必须判**可见性**，否则误判为已展开
            if self.page.evaluate(
                    """() => [...document.querySelectorAll(
                              '.mode-select-sub-popover-container .qwen-chat-v2-dropdown-menu-item')]
                            .some(e => e.getBoundingClientRect().width > 0)"""):
                return True
            self.page.wait_for_timeout(700)
        return False

    def set_mode(self, mode: str) -> str:
        """选模式：t2i/t2v/slides/web_dev/deep_research/preview/study/travel/search
        （或直接给中文页签名）。返回实际模式名（空串 = 普通对话）。"""
        if not mode or mode in ("chat", "t2t", "none"):
            if self.current_mode():
                self.exit_mode()
            return ""
        label = PLUS_MODES.get(mode, mode if mode in PLUS_MODES.values() else "")
        if not label:
            raise RuntimeError(f"未知模式 {mode!r}，可用: {sorted(PLUS_MODES)}")
        # 反查能力键（调用方可能直接给中文页签名），用来判「是否藏在更多子菜单里」
        key = mode if mode in PLUS_MODES else \
            next((k for k, v in PLUS_MODES.items() if v == label), "")
        if label in ("上传附件", "工具", "更多"):
            raise RuntimeError(f"{label!r} 不是模式（{label} 不是对话模式，别用 set_mode）")
        if self.current_mode() == label:
            self._applied["mode"] = label
            return label
        self._open_plus_menu()
        self.guard("set_mode")
        got = self._click_js(label)
        if not got and key in SUBMENU_MODES:
            # 「更多」子菜单：hover 才渲染，且浮层出现有延迟 → 轮询等它出来
            if self._open_submenu():
                got = self._click_js(label, scope=".mode-select-sub-popover-container")
            if not got:                      # 兜底：整页再找一次
                got = self._click_js(label)
        if not got:
            self.page.keyboard.press("Escape")
            raise RuntimeError(f"加号菜单里找不到 {label!r} 条目")
        self._jitter(1000, 1800)
        now = self.current_mode()
        if now != label:
            raise RuntimeError(f"选模式失败：点了 {label!r}，芯片显示 {now!r}")
        self._applied["mode"] = label
        self._applied["search"] = (label == PLUS_MODES["search"])
        log(f"mode -> {label}")
        return label

    def ensure_search(self, on: bool) -> None:
        """「网页搜索」在页面上是一个**模式**（不是开关）：on → 切到该模式；
        off → 若当前正处在该模式则退出。"""
        search_label = PLUS_MODES["search"]
        if on:
            self.set_mode("search")
        elif self.current_mode() == search_label:
            self.exit_mode()

    def ensure_chat_mode(self) -> None:
        """回到普通对话（任何模式退出）。"""
        if self.current_mode():
            self.exit_mode()

    # -- 模式子配置（比例/分辨率/研究深度…）------------------------
    #
    # 页面把每个模式的选项渲染成输入区里的一组下拉（`.qwen-chat-v2-dropdown-menu-select`），
    # 触发按钮的文案**就是当前值**（如 "16:9"、"720P · 5s"、"正常"）。思考档下拉同构，
    # 靠文案（自动/思考/快速）区分。

    def read_config(self) -> list:
        """读输入区所有子配置下拉的当前值（不含思考档）。"""
        return self.page.evaluate(
            """() => {
              const root = document.querySelector('.placeholder-container');
              if (!root) return [];
              const out = [];
              root.querySelectorAll('.qwen-chat-v2-dropdown-menu-select').forEach(e => {
                const r = e.getBoundingClientRect();
                const t = (e.innerText || '').trim();
                if (r.width > 0 && t && !['自动', '思考', '快速'].includes(t))
                  out.push({label: t, rect: [r.x|0, r.y|0, r.width|0, r.height|0]});
              });
              return out;
            }"""
        ) or []

    def configs_semantic(self) -> list:
        """read_config() + 语义键：调用方按名字改，不用记 index（页面顺序会变）。"""
        out = []
        for i, cfg in enumerate(self.read_config()):
            label = cfg.get("label", "")
            key = _cfg_key(label)
            out.append({**cfg, "index": i, "key": key})
        return out

    def set_config(self, value: str, *, key: str = "", index: int = 0) -> str:
        """把输入区某个子配置改成 value（精确匹配条目文案）。

        `key` 优先（ratio/model/depth，见 _cfg_key）；否则按 `index`。
        典型：t2i 的 ratio 是「16:9」；model 是「Qwen-Image 3.0」；
        deep_research 的 depth 是「正常/高级」。返回改后的文案。
        """
        cfgs = self.configs_semantic()
        if not cfgs:
            raise RuntimeError("当前模式没有可切换的子配置")
        if key:
            hit = [c for c in cfgs if c["key"] == key]
            if not hit:
                hint = ""
                # 实测坑：Qwen-Image 3.0 不提供比例下拉（只有 2.0 有）→ 给条明路，别让人瞎猜
                if key == "ratio" and any("3.0" in c["label"] for c in cfgs):
                    hint = ("；提示：Qwen-Image 3.0 不提供比例，"
                            "先 set_config('Qwen-Image 2.0', key='model')")
                raise RuntimeError(
                    f"当前子配置里没有 {key!r}（现有: "
                    f"{[(c['index'], c['key'], c['label']) for c in cfgs]}）{hint}")
            cur = hit[0]
        else:
            if index >= len(cfgs):
                raise RuntimeError(f"子配置只有 {len(cfgs)} 个，index={index} 越界")
            cur = cfgs[index]
        x, y, w, h = cur["rect"]
        self.page.mouse.click(x + w / 2, y + h / 2)
        self._jitter(700, 1200)
        self.guard("set_config")
        got = self._click_js(value)
        if not got:
            self.page.keyboard.press("Escape")
            raise RuntimeError(f"子配置 {cur['label']!r} 里没有 {value!r} 选项")
        self._jitter(700, 1200)
        now = self.read_config()
        val = now[cur["index"]]["label"] if cur["index"] < len(now) else ""
        log(f"config[{cur['key']}#{cur['index']}] {cur['label']} -> {val}")
        return val

    # -- 创建视频参数弹层（清晰度 / 编辑模式 / 比例 / 时长）--------
    #
    # 2026-10-19 实测（report15-18）：它**不是下拉**，是输入条右侧的独立 ant-popover。
    #   入口芯片 `.video-params-summary`（文案就是摘要「720P · 5s」）
    #   `.video-params-popover` > `.video-params-group` × 4
    #       组名 `.video-params-group-title` = 清晰度 | 视频编辑模式 | 比例 | 时长
    #       选项 button[role=radio][aria-checked] > .video-params-segment-label
    #       1080P 上挂 `.video-params-vip-badge`（要 VIP；点它只会弹升级，默认拒绝）
    # 这也是 `.qwen-chat-v2-dropdown-menu-select` 读不到它的原因。

    def video_params_summary(self) -> str:
        """不打开弹层读摘要（如 "720P · 5s"；不在创建视频模式时为空）。零风险。"""
        try:
            return (self.page.evaluate(
                "() => (document.querySelector('.video-params-summary')?.innerText || '').trim()"
            ) or "")
        except Exception:  # noqa: BLE001
            return ""

    def _video_pop_open(self) -> bool:
        try:
            return bool(self.page.evaluate(
                """() => {const p = document.querySelector('.video-params-popover');
                          return !!p && p.getBoundingClientRect().width > 0;}"""))
        except Exception:  # noqa: BLE001
            return False

    def _open_video_params(self) -> bool:
        if self._video_pop_open():
            return True
        chip = self.page.locator(SEL_VIDEO_SUMMARY).first
        if chip.count() == 0:
            return False
        if not self._click_safe(chip):
            return False
        for _ in range(4):
            self._jitter(500, 900)
            self.guard("video_params:open")
            if self._video_pop_open():
                return True
        return False

    def read_video_params(self, *, close: bool = True) -> dict:
        """读创建视频的四组参数。当前不在创建视频模式（没有该芯片）→ {}。"""
        if not self._open_video_params():
            return {}
        summary = self.video_params_summary()
        try:
            groups = self.page.evaluate(
                """() => {
                  const pop = document.querySelector('.video-params-popover');
                  if (!pop) return [];
                  return [...pop.querySelectorAll('.video-params-group')].map(g => ({
                    title: (g.querySelector('.video-params-group-title')?.innerText || '').trim(),
                    options: [...g.querySelectorAll('[role=radio]')].map(b => ({
                      label: (b.querySelector('.video-params-segment-label')?.innerText || '').trim(),
                      checked: b.getAttribute('aria-checked') === 'true',
                      vip: !!b.querySelector('.video-params-vip-badge')}))}));}"""
            ) or []
        finally:
            if close:
                self.page.keyboard.press("Escape")
                self._jitter(300, 600)
        out, raw = {}, {}
        for g in groups:
            key = VIDEO_PARAM_KEYS.get(g["title"], g["title"])
            raw[key] = g
            cur = next((o["label"] for o in g["options"] if o["checked"]), "")
            out[key] = cur
        return {"summary": summary, "current": out,
                "options": {k: v["options"] for k, v in raw.items()}}

    def set_video_params(self, want: dict, *, allow_vip: bool = False) -> dict:
        """改创建视频参数。want 例：{"clarity": "1080P", "ratio": "1:1", "duration": "10s"}。

        VIP 档（1080P）默认**拒绝**（点了只会弹升级，白费一次交互）：想试就 allow_vip=True。
        """
        kk = {(VIDEO_KEY_ALIAS.get(k, k)): v for k, v in (want or {}).items() if v}
        if not kk:
            return self.read_video_params()
        if not self._open_video_params():
            raise RuntimeError("当前不在创建视频模式（找不到 .video-params-summary）")
        try:
            for key, val in kk.items():
                self.guard("video_params:set")
                got = self.page.evaluate(
                    """([key, want]) => {
                      const MAP = {'clarity':'清晰度','vmode':'视频编辑模式',
                                   'ratio':'比例','duration':'时长'};
                      const title = MAP[key];
                      const pop = document.querySelector('.video-params-popover');
                      if (!pop || !title) return {err: 'no-popover-or-key'};
                      for (const g of pop.querySelectorAll('.video-params-group')) {
                        const t = (g.querySelector('.video-params-group-title')?.innerText || '').trim();
                        if (t !== title) continue;
                        for (const b of g.querySelectorAll('[role=radio]')) {
                          const label = (b.querySelector('.video-params-segment-label')?.innerText || '').trim();
                          if (label === want) {
                            if (b.getAttribute('aria-checked') === 'true') return {ok: label, noop: true};
                            if (b.querySelector('.video-params-vip-badge') && !%s)
                              return {err: 'vip', label};
                            b.click();
                            return {ok: label};
                          }
                        }
                        return {err: 'no-option', have: [...g.querySelectorAll(
                            '.video-params-segment-label')].map(e => (e.innerText||'').trim())};
                      }
                      return {err: 'no-group'};
                    }""" % ("true" if allow_vip else "false"),
                    [key, val])
                if not (got or {}).get("ok"):
                    raise RuntimeError(f"视频参数 {key}={val!r} 设置失败: {got}")
                self._jitter(600, 1100)
            after = self.read_video_params(close=True)
            return after
        finally:
            if self._video_pop_open():
                self.page.keyboard.press("Escape")
                self._jitter(200, 500)

    def mode_config_options(self) -> list:
        """枚举当前模式每个子配置的**全部可选项**（逐个打开读，读完关）。"""
        out = []
        for i, cfg in enumerate(self.read_config()):
            x, y, w, h = cfg["rect"]
            try:
                self.page.mouse.click(x + w / 2, y + h / 2)
                self._jitter(700, 1100)
                self.guard("cfg_options")
                opts = self.page.evaluate(
                    """() => [...document.querySelectorAll('.qwen-chat-v2-dropdown-menu-item')]
                         .filter(e => e.getBoundingClientRect().width > 0)
                         .map(e => (e.innerText || '').trim().split('\\n')[0])"""
                ) or []
                out.append({"index": i, "value": cfg["label"], "options": opts})
            except Exception as e:  # noqa: BLE001
                out.append({"index": i, "value": cfg["label"], "error": str(e)[:120]})
            finally:
                self.page.keyboard.press("Escape")
                self._jitter(300, 600)
        return out

    def _expand_thinking(self) -> bool:
        """展开最后一条回答的思考面板（默认只剩「已经完成思考」卡片，正文不在 DOM）。

        实测结构（2026-10-19）：点 `.qwen-chat-thinking-tool-status-card-wraper`
        → 浮出 `.qwen-chat-thinking-and-sources`（header「思考与搜索」+ content）。
        """
        try:
            if self.page.evaluate(
                    "() => !!document.querySelector('.qwen-chat-thinking-and-sources-content')"):
                return True
            hit = self.page.evaluate(
                """() => {
                  const cards = document.querySelectorAll(
                      '.qwen-chat-thinking-tool-status-card-wraper');
                  const card = cards.length ? cards[cards.length - 1] : null;
                  if (!card) return false;
                  const t = card.querySelector('.qwen-chat-thinking-status-card-title-text');
                  (t || card).click();
                  return true;
                }"""
            )
            if hit:
                for _ in range(8):
                    self.page.wait_for_timeout(350)
                    if self.page.evaluate(
                            "() => !!document.querySelector("
                            "'.qwen-chat-thinking-and-sources-content')"):
                        break
                self.page.wait_for_timeout(random.randint(300, 700))
            return bool(hit)
        except Exception:  # noqa: BLE001
            return False

    # -- 附件 -----------------------------------------------------

    def _upload_files(self, paths: list) -> int:
        """真实上传：首选「加号菜单→上传附件」拦截 filechooser（探测实测可用），
        兜底直接写 #filesUpload。芯片计数不足则**硬报错**——不盲发消息。"""
        abs_paths = [str(Path(p).expanduser().resolve()) for p in paths]
        for p in abs_paths:
            if not Path(p).is_file():
                raise RuntimeError(f"文件不存在: {p}")
        # 芯片选择器：直接数 .file-card-list 的子元素，覆盖所有卡片类型。
        # ⚠️ 2026-11 实测前端改版：文件卡片类名是 .fileitem-btn，
        # 旧写法 `+ " .vision-item-container"` 已永远数到 0（上传其实成功，
        # 却误报「芯片不足」并拒绝发送）。别再写死具体卡片类名。
        chip_sel = SEL_FILE_CHIP + " > *"
        before = self.page.locator(chip_sel).count()
        done = False
        try:
            with self.page.expect_file_chooser(timeout=15000) as fc:
                self._plus_open()
                # ⚠️ 不能 JS 盲点：浮层是异步渲染的，evaluate 里 querySelector
                # 可能一上来就查不到（拿到 0 个 → 什么都不点 → filechooser 超时）。
                # 用 Playwright locator 等可见再真实点击，稳得多。
                item = self.page.locator(
                    ".qwen-chat-v2-dropdown-menu-item", has_text=PLUS_MODES["upload"]).first
                item.wait_for(state="visible", timeout=10000)
                item.click()
            fc.value.set_files(abs_paths)
            done = True
        except Exception as e:  # noqa: BLE001
            log(f"filechooser route failed: {str(e)[:120]}")
            try:
                li = self.page.locator("#filesUpload")
                if li.count() == 0:
                    raise RuntimeError("页面未找到 #filesUpload")
                li.set_input_files(abs_paths)
                done = True
            except Exception:  # noqa: BLE001
                pass
        # ⚠️ 芯片出现 ≠ 附件可用（2026-11 实测）：
        #   文档类附件（txt/docx/pdf…）选中后卡片**立刻**出现，但还要走
        #   /api/v2/files/parse 解析（实测 2~4s，大文件更久），期间卡片文字是
        #   「解析中……」。此时发送会被前端挡下 —— 表现为消息没进对话、
        #   ask 侧「等待回答超时」，随后卡片转红「解析失败」。
        #   旧代码数的 .vision-item-container 只在解析完成后才渲染，等价于
        #   隐式等解析；改版后卡片提前出现，这层等待必须显式补回来。
        #   图片/视频不走 parse，pending 恒为 0，不受影响。
        want = before + len(abs_paths)
        deadline = time.time() + 120
        st = {"n": 0, "pending": 0, "failed": 0}
        while time.time() < deadline:
            self.page.wait_for_timeout(800)
            st = self.page.evaluate(
                """(sel) => {
                  const list = document.querySelector(sel);
                  if (!list) return {n: 0, pending: 0, failed: 0};
                  const cards = [...list.children];
                  let pending = 0, failed = 0;
                  for (const c of cards) {
                    const t = c.innerText || '';
                    if (t.includes('解析中') || t.includes('上传中')) pending++;
                    else if (t.includes('解析失败') || t.includes('上传失败')) failed++;
                  }
                  return {n: cards.length, pending: pending, failed: failed};
                }""", SEL_FILE_CHIP)
            if st["n"] >= want and not st["pending"]:
                break
        if st["n"] < want:
            raise RuntimeError(
                f"上传芯片不足：期望 {want} 个，实际 {st['n']} 个"
                f"（route={'fc' if done else 'none'}）")
        if st["failed"]:
            raise RuntimeError(
                f"附件解析失败 {st['failed']} 个（服务端 /files/parse 未就绪）")
        if st["pending"]:
            raise RuntimeError(
                f"附件解析超时：仍有 {st['pending']} 个停在「解析中」")
        # 解析刚完成时前端还要把文本挂到消息体上，留一点余量再发送
        self.page.wait_for_timeout(1500)
        log(f"attached {len(abs_paths)} file(s)")
        return len(abs_paths)

    # -- 读取当前回答状态 -----------------------------------------

    @staticmethod
    def _read(page) -> dict:
        return page.evaluate(
            """() => {
              const boxes = document.querySelectorAll('.qwen-chat-message-assistant');
              const last = boxes.length ? boxes[boxes.length - 1] : null;
              if (!last) {
                const c = document.querySelector('.exceed-limit-error-card');
                return {answer: '', thinking: '', refs: [], images: [], videos: [],
                        files: [], attachments: [], loading: false, present: false,
                        notice: c ? (c.innerText || '').trim() : ''};
              }
              const md = last.querySelector('.qwen-markdown');
              let answer = md ? (md.innerText || '').trim() : '';
              // web_dev 类产物：正文在 iframe 预览里，HTML 源码在 srcdoc（2026-10-19 实测：
              // 选「网页开发」后消息体无 markdown，只有预览卡+预览图）
              if (!answer) {
                let html = '';
                last.querySelectorAll('iframe').forEach(f => {
                  try { if (f.srcdoc && f.srcdoc.length > html.length) html = f.srcdoc; } catch (e) {}
                });
                if (html) answer = html.slice(0, 80000);
              }
              // 引用来源：正文里的 citation 主机名 + 文本里出现的 URL
              const refs = [];
              const seen = new Set();
              last.querySelectorAll('.qwen-chat-markdown-tokens-hostname').forEach(e => {
                const h = (e.innerText || '').trim();
                if (h && !seen.has(h)) { seen.add(h); refs.push({title: h, url: 'https://' + h}); }
              });
              // 尾随标点（中英文）不算 URL 的一部分，截掉
              const urls = (answer.match(/https?:\\/\\/[^\\s\\)\\]>,，。；;、！？：）】》」』]+/g) || []);
              urls.forEach(u => {
                if (!seen.has(u)) { seen.add(u); refs.push({title: u, url: u}); }
              });
              // 思考：默认折叠（只剩「已经完成思考」卡片），点开后才在
              // .qwen-chat-thinking-and-sources-content 里出现正文（2026-10-19 实测）
              let thinking = '';
              let th = last.querySelector('.qwen-chat-thinking-and-sources-content')
                    || last.querySelector('[class*=thinking-content]');
              // 思考面板可能是**挂在消息体外的浮层**（实测就这么干的）——退回全页取最后一个
              if (!th) {
                const all = document.querySelectorAll(
                    '.qwen-chat-thinking-and-sources-content, [class*=thinking-content]');
                th = all.length ? all[all.length - 1] : null;
              }
              if (th) thinking = (th.innerText || '').trim();
              // 生成物：t2i/t2v 的图与视频（排除头像/图标类小图）
              const images = [];
              const videos = [];
              last.querySelectorAll('img').forEach(i => {
                const s = i.currentSrc || i.src || '';
                if (/^https?:/.test(s) && !/avatar|icon|emoji/i.test(s)
                    && !/\\.mp4(\\?|$)/i.test(s)) images.push(s);  // poster 也是 .mp4，剔掉
              });
              last.querySelectorAll('video').forEach(v => {
                let s = v.currentSrc || v.src || '';
                if (!s) { const so = v.querySelector('source'); if (so) s = so.src || ''; }
                if (/^https?:/.test(s)) videos.push(s);
              });
              // 文件类产物：幻灯片 PDF/PPTX、压缩包等（slides 模式给 pdf_url + 每页 PNG）
              const files = [];
              const seenF = new Set();
              last.querySelectorAll('a[href], [data-url], [data-href]').forEach(a => {
                const s = a.getAttribute('href') || a.getAttribute('data-url')
                          || a.getAttribute('data-href') || '';
                if (/^https?:/.test(s) && /\\.(pdf|pptx?|docx?|zip|xlsx?)(\\?|$)/i.test(s)
                    && !seenF.has(s)) { seenF.add(s); files.push(s); }
              });
              // 附件卡片：网页开发/幻灯片等把产物做成卡片（无正文、无直链，只有预览按钮）
              const attachments = [];
              last.querySelectorAll('.attachment-card-container').forEach(c => {
                attachments.push({
                  title: (c.querySelector('.attachment-card-title')?.innerText || '').trim(),
                  time: (c.querySelector('.attachment-card-time')?.innerText || '').trim(),
                  preview: !!c.querySelector('[class*=attachment-card-botton]'),
                });
              });
              // 系统提示/错误卡（额度耗尽 `.exceed-limit-error-card`、繁忙、登录失效）：
              // 这类消息**没有 .qwen-markdown 正文**，旧逻辑当成"还没出文本"→空转到超时。
              let notice = '';
              const card = last.querySelector('.exceed-limit-error-card');
              if (card) notice = (card.innerText || '').trim();
              if (!notice) {
                for (const el of last.querySelectorAll(
                      '[class*=error], [class*=limit], [class*=notice]')) {
                  const t = (el.innerText || '').trim();
                  if (t && t.length < 240) { notice = t; break; }
                }
              }
              if (!notice && !answer) {
                // 兜底：末条既无正文也无错误卡类名 —— 只有命中关键词的短文案才当真
                const t = (last.innerText || '').trim();
                if (t && t.length < 240
                    && /上限|额度|升级会员|频繁|繁忙|登录|失效|过期|网络|连接|断开|异常|错误|quota|limit/i.test(t)) notice = t;
              }
              if (!notice && !answer) {
                // 文档级扫描：`网络错误，重新连接中...` 这类 toast/横幅挂在 body 上，
                // **不在 assistant 气泡里** → 只看 last 会漏检，然后干等到超时
                // （2026-11-27 实测：SSE 断流时页面只剩这句浮层，被误报成"无正文疑似被拒"）。
                const kw = /上限|额度|升级会员|频繁|繁忙|登录|失效|过期|网络|连接|断开|异常|错误|quota|limit/i;
                for (const el of document.querySelectorAll(
                      '[class*=error], [class*=Error], [class*=toast], [class*=alert],'
                      + '[role=alert], [class*=notice], [class*=retry]')) {
                  const t = (el.innerText || '').trim();
                  if (t && t.length < 240 && kw.test(t)) { notice = t; break; }
                }
              }
              return {
                answer: answer,
                thinking: thinking,
                refs: refs,
                images: images,
                videos: videos,
                files: files,
                attachments: attachments,
                loading: !!last.querySelector('.response-loading'),
                present: true,
                notice: notice,
              };
            }"""
        )

    # -- 无正文时的现场取证 ----------------------------------------

    def _diagnose_no_text(self, notice: str = "") -> str:
        """「流已结束但页面没有正文」时抓现场：截图 + 节点计数 + 可见短文案。

        2026-11-27 首次遇到这个分支时只报了一句"无系统提示"，事后无从定位到底是
        额度、风控还是前端弹层 —— 证据必须留在错误信息里，不能靠重跑复现。
        """
        SHOTS.mkdir(exist_ok=True)
        p = SHOTS / f"notext_{int(time.time())}.png"
        try:
            self.page.screenshot(path=str(p))
        except Exception:  # noqa: BLE001
            p = None
        try:
            diag = self.page.evaluate(
                """() => {
                  const body = (document.body.innerText || '').replace(/\\n{2,}/g, '\\n');
                  const cards = [...document.querySelectorAll(
                      '.exceed-limit-error-card, [class*=limit], [class*=error],'
                      + '[class*=toast], [role=dialog], [class*=modal]')]
                    .map(e => (e.innerText || '').trim())
                    .filter(t => t && t.length < 200);
                  return {
                    assistants: document.querySelectorAll('.qwen-chat-message-assistant').length,
                    last_text: (() => {
                      const b = document.querySelectorAll('.qwen-chat-message-assistant');
                      return b.length ? (b[b.length - 1].innerText || '').trim().slice(0, 300) : '';
                    })(),
                    loading: !!document.querySelector('.response-loading'),
                    cards: cards.slice(0, 4),
                    body_tail: body.slice(-400),
                  };
                }"""
            )
        except Exception as e:  # noqa: BLE001
            diag = {"err": str(e)[:200]}
        return (f"notice={notice[:120]!r}; 截图={p}; "
                f"POST端点={self._post_paths_tail()}; "
                f"现场={json.dumps(diag, ensure_ascii=False)[:700]}")

    # -- 产物读取（不发送消息）-------------------------------------

    def assets(self) -> dict:
        """读**当前对话**最后一条回答里的产物（图/视频/文件/附件卡）。

        t2v 是异步的：提交后页面只回「生成好后会通知你」，视频要过一阵才落到对话里。
        调用方据此反复调用本方法轮询即可（不发新消息，风控压力≈0）。
        """
        st = self._read(self.page)
        return {
            "url": self.page.url,
            "mode": self.current_mode(),
            "answer": (st.get("answer") or "")[:400],
            "images": st.get("images") or [],
            "videos": st.get("videos") or [],
            "files": st.get("files") or [],
            "attachments": st.get("attachments") or [],
        }

    # -- 提问 -----------------------------------------------------

    def ask(self, text: str, timeout: int = ASK_TIMEOUT, *,
            new_chat: bool = False, search: bool = False,
            files: list | None = None, model: str = "",
            think: bool | str = False, mode: str = "",
            vparams: dict | None = None) -> dict:
        t0 = time.time()
        if new_chat:
            self.new_chat()
        self.guard("ask:pre")
        # 页面配置先行：模式 → 模型 → 思考档 → 附件。
        # 模式与「联网搜索」在页面上是同一个东西（都是模式芯片），显式 mode 优先。
        want_mode = mode or ("search" if search else "")
        if want_mode:
            self.set_mode(want_mode)
        elif self.current_mode():
            self.ensure_chat_mode()
        if model:
            self.set_model(model)
            # 切模型后复核模式芯片还在（模型下拉是全局的，理论上不碰对话模式）
            if want_mode and self.current_mode() != PLUS_MODES.get(want_mode, want_mode):
                log("⚠️ 切模型后模式丢失，重设")
                self.set_mode(want_mode)
        elif new_chat:
            # 新对话的初始模型在页面侧不确定（2026-11-27 实测：账号 pin 在 3.7-Plus，
            # 新建对话却时而是 4-Max、时而是 3.7-Plus，10 分钟内三个对话两种结果）。
            # 对齐到账号级 pin 保证确定性；账号有 pin 时强制（对齐失败直接报错，
            # 不静默沿用不可控默认——那等于把用户送回随机 quota 的陷阱）。
            self._align_to_pinned_default()
        # 思考档：True→思考；str→按档名；False→自动（当前代码语义：显式归位）
        if think is True:
            self.set_thinking("thinking")
        elif isinstance(think, str) and think:
            self.set_thinking(think)
        elif self._applied.get("think") == "思考" and self._think_label():
            # 只在**页面上真有该下拉**时归位：生成类模式没有思考档，
            # 硬归位会抛错（实测 t2i 下必挂）
            self.set_thinking("auto")
        if files:
            self._upload_files(files)
        # 视频参数（清晰度/编辑模式/比例/时长）：必须在**点发送前**设好，
        # 页面按当前参数计费（消耗 N 积分会随之变）。
        video_cfg = {}
        if vparams:
            video_cfg = self.set_video_params(vparams)
        configs = self.read_config()

        prev = self._read(self.page)
        prev_len = len(prev.get("answer") or "")
        prev_n = self.page.evaluate("() => document.querySelectorAll('.qwen-chat-message-assistant').length")

        box = self.page.locator(SEL_INPUT).first
        box.click()
        if len(text) > PASTE_THRESHOLD:
            # 长文本：fill = 一次性写入 + 派发 input 事件（textarea 语义等价），
            # 逐字敲则会在 30s 默认动作超时处被杀（2026 实测：7000 字必挂）。
            pasted = False
            try:
                box.fill(text)
                pasted = (box.input_value() or "") == text
            except Exception:  # noqa: BLE001
                pasted = False
            if pasted:
                log(f"pasted len={len(text)}")
            else:                                    # 兜底：放大动作超时后逐字敲
                box.fill("")
                self.page.set_default_timeout(max(30000, len(text) * 60 + 60000))
                try:
                    box.press_sequentially(text, delay=random.randint(18, 45))
                finally:
                    self.page.set_default_timeout(30000)
        else:
            box.press_sequentially(text, delay=random.randint(18, 45))
        self.page.wait_for_timeout(random.randint(400, 900))
        self._stream_reset()            # 必须在按下 Enter 前清空，才能只数本次的流
        # ⚠️ send_ts 必须在 press 之前取：press 同步派发 keydown，请求可能紧接着就发出；
        # 若在其后取时间戳，请求时刻会早于 send_ts，`_stream_seen()` 的 `t >= since`
        # 永不成立（2026-11-27 实测：端点明明命中却报「流事件=N」）。
        send_ts = time.time()
        box.press("Enter")
        log(f"sent len={len(text)}")

        # ── 完成判定（2026-11-27 重做 = 反思58 的根治）──────────────────
        # 签名 = 「正文 + 产物(图/视频/文件)」：t2i/t2v/slides 这类**没有正文只有产物**
        # 的模式只盯正文会一路空转到超时（实测 t2i 因此白等 420s）。
        # 结束条件 = SSE 流结束（肯定式信号）+ DOM 再稳 SETTLE_POLLS 轮 + 页面不自认在生成。
        # 只有观测不到流事件（页面改版）才退回稳定窗口兜底，且窗口比旧 3 轮保守得多。
        def _sig(st: dict) -> str:
            ans = (st.get("answer") or "").strip()
            assets = (st.get("images") or []) + (st.get("videos") or []) + (st.get("files") or [])
            return ans + "||" + "|".join(assets)

        prev_sig = _sig(prev)
        last_sig, stable, polls = "", 0, 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.page.wait_for_timeout(int(POLL_EVERY * 1000))
            polls += 1
            if polls % 2 == 0:            # 每隔一轮查一次风控（人机验证立刻停手）
                self.guard("ask:poll")
            cur_st = self._read(self.page)
            notice = (cur_st.get("notice") or "").strip()
            sig = _sig(cur_st)
            if notice and not sig.strip("|"):
                raise self._notice_error(notice, "ask:poll")   # 额度/繁忙/登录 → 立刻抛
            n = self.page.evaluate(
                "() => document.querySelectorAll('.qwen-chat-message-assistant').length")
            fresh = cur_st.get("present") and (n > prev_n or sig != prev_sig)
            if not (fresh and sig.strip("|")):
                # 还没有新正文/产物。若服务端已经结束 → 被拒/页面异常，别白等满 timeout。
                # ⚠️ 必须给渲染留余地：markdown 渲染/大代码块落 DOM 有滞后（实测长回答
                # 2~5s），只等 3 轮就判死会把"正在渲染"错杀成"被拒"。故再要求超过
                # STREAM_PROBE_SEC；判定前先抓现场（截图+DOM 摘要）再抛。
                if (self._stream_finished(send_ts) and polls >= 3
                        and time.time() - send_ts > STREAM_PROBE_SEC):
                    raise RuntimeError(
                        "qwen 服务端已结束但页面没有正文/产物（疑似被拒/额度/页面异常）: "
                        + self._diagnose_no_text(notice))
                stable, last_sig = 0, sig
                continue
            if sig != last_sig:
                stable, last_sig = 0, sig
                continue
            stable += 1
            if cur_st.get("loading"):
                continue                  # 页面自己还认为在生成，宁可信它
            if self._stream_finished(send_ts):
                if stable >= SETTLE_POLLS:
                    break
            elif (not self._stream_seen(send_ts)
                  and time.time() - send_ts > STREAM_PROBE_SEC
                  and stable >= FALLBACK_STABLE_POLLS):
                log("⚠️ 未观测到 completions 流事件，退回稳定窗口兜底；"
                    f"POST端点={self._post_paths_tail()}")
                break

        if think:
            self._expand_thinking()
        final = self._read(self.page)
        # 生成类模式兜底：正文/产物都还没出来时多等一会儿（渲染滞后）。
        # ⚠️ t2v 是**异步**：页面只回「生成好后会通知你」，实测 ~11 分钟后视频才落进
        # 对话里 → 这里只短等 30s，剩下的交给 backend.py assets 轮询，别白等。
        if mode in ("t2i", "t2v") and not (final.get("images") or final.get("videos")):
            want_key = "images" if mode == "t2i" else "videos"
            until = time.time() + (240 if mode == "t2i" else 30)
            while time.time() < until and not final.get(want_key):
                self.page.wait_for_timeout(3000)
                self.guard("ask:assets")
                final = self._read(self.page)
        answer = (final.get("answer") or "").strip()
        elapsed = round(time.time() - t0, 1)
        # 每次成功都留一行端点快照：流端点改版时，上一行日志就能给出真端点
        log(f"done len={len(answer)} 流事件={'Y' if self._stream_seen(send_ts) else 'N'} "
            f"POST端点={self._post_paths_tail(5)}")

        if not answer and not (final.get("images") or final.get("videos")
                                or final.get("files") or final.get("attachments")):
            SHOTS.mkdir(exist_ok=True)
            p = SHOTS / f"timeout_{int(time.time())}.png"
            try:
                self.page.screenshot(path=str(p))
            except Exception:  # noqa: BLE001
                pass
            if self._waf_hits and not self._ok_seen:
                raise RuntimeError(
                    "qwen 触发风控人机验证（RGV587），自动接住失败——"
                    "请在浏览器里过一次验证后重试。截图: %s" % p
                )
            raise RuntimeError(f"等待回答超时(未观测到新文本), 截图: {p}")

        st = _load_state()
        st["last_ask"] = time.time()
        st["count"] = int(st.get("count", 0)) + 1
        _save_state(st)

        return {
            "answer": answer,
            "thinking": (final.get("thinking") or "").strip(),
            "references": final.get("refs") or [],
            "images": final.get("images") or [],
            "videos": final.get("videos") or [],
            "files_out": final.get("files") or [],
            "attachments": final.get("attachments") or [],
            "model": self.current_model(),
            "mode": self.current_mode(),
            "configs": [c.get("label", "") for c in configs],
            # 创建视频的实参（点发送前设的）：{"clarity","vmode","ratio","duration"} + 摘要
            "vparams": (video_cfg or {}).get("current") or {},
            "video_summary": (video_cfg or {}).get("summary") or self.video_params_summary(),
            "elapsed": elapsed,
            "search": bool(search) or self.current_mode() == PLUS_MODES["search"],
            # 异步产物（t2v）：提交成功但视频还在生成，用 backend.py assets 轮询
            "pending": bool(mode == "t2v" and not (final.get("videos") or [])),
            "waf_retries": len(self._waf_hits),
            "files": len(files or []),
            "url": self.page.url,
        }

    def close(self):
        try:
            self.ctx.close()
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


# ---------------- daemon ----------------


def cmd_daemon() -> int:
    if SOCK.exists():
        SOCK.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCK))
    srv.listen(4)
    srv.settimeout(60.0)
    log(f"daemon up, sock={SOCK} idle={IDLE_SEC}s")
    backend = None
    last_act = time.time()
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                if time.time() - last_act > IDLE_SEC:
                    log(f"idle {IDLE_SEC}s, auto shutdown")
                    break
                continue
            try:
                req = json.loads(conn.recv(1 << 20).decode())
                cmd = req.get("cmd")
                if cmd in ("ask", "probe", "models", "pin", "configs", "assets",
                           "vparams", "vset", "setcfg", "mode", "think"):
                    last_act = time.time()
                if cmd == "stop":
                    conn.sendall(json.dumps({"ok": True}).encode())
                    conn.close()
                    break
                if cmd == "ping":
                    conn.sendall(json.dumps({"ok": True, "alive": True}).encode())
                    conn.close()
                    continue
                if cmd == "probe":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        conn.sendall(json.dumps({"ok": True, **backend.probe()},
                                                ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                        if backend is not None:
                            backend.close()
                            backend = None
                    conn.close()
                    continue
                if cmd == "models":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        ms = [{**m, "capabilities": sorted(m.get("capabilities") or set())}
                              for m in backend.list_models(force=bool(req.get("force")))]
                        conn.sendall(json.dumps({"ok": True, "models": ms,
                                                 "current": backend.current_model()},
                                                 ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                        if backend is not None:
                            backend.close()
                            backend = None
                    conn.close()
                    continue
                if cmd == "assets":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        conn.sendall(json.dumps({"ok": True, **backend.assets()},
                                                ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                    conn.close()
                    continue
                if cmd == "pin":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        got = backend.pin_model(req.get("model", ""))
                        conn.sendall(json.dumps({"ok": True, "pinned": got},
                                                ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                    conn.close()
                    continue
                if cmd == "configs":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        out = {"mode": backend.current_mode(), "thinking": backend._think_label(),
                               "configs": backend.mode_config_options(),
                               "semantic": backend.configs_semantic(),
                               "video": backend.read_video_params()}
                        conn.sendall(json.dumps({"ok": True, **out},
                                                ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                    conn.close()
                    continue
                if cmd == "mode":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        got = backend.set_mode(req.get("mode", ""))
                        conn.sendall(json.dumps({"ok": True, "mode": got,
                                                 "configs": backend.configs_semantic(),
                                                 "video": backend.read_video_params()},
                                                ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                    conn.close()
                    continue
                if cmd == "think":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        got = backend.set_thinking(req.get("level", "auto"))
                        conn.sendall(json.dumps({"ok": True, "thinking": got},
                                                ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                    conn.close()
                    continue
                if cmd == "vparams":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        out = backend.read_video_params()
                        conn.sendall(json.dumps({"ok": True, **out},
                                                ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                    conn.close()
                    continue
                if cmd == "vset":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        out = backend.set_video_params(req.get("want") or {},
                                                       allow_vip=bool(req.get("allow_vip")))
                        conn.sendall(json.dumps({"ok": True, **out},
                                                ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                    conn.close()
                    continue
                if cmd == "setcfg":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        got = backend.set_config(req.get("value", ""),
                                                 key=req.get("key", ""),
                                                 index=int(req.get("index") or 0))
                        conn.sendall(json.dumps({"ok": True, "value": got,
                                                 "semantic": backend.configs_semantic()},
                                                ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                    conn.close()
                    continue
                if cmd == "ask":
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        res = backend.ask(req["text"], int(req.get("timeout", ASK_TIMEOUT)),
                                          new_chat=bool(req.get("new_chat")),
                                          search=bool(req.get("search")),
                                          files=req.get("files") or None,
                                          model=req.get("model", ""),
                                          think=req.get("think", False),
                                          mode=req.get("mode", ""),
                                          vparams=req.get("vparams") or None)
                        audit({"q_len": len(req["text"]), "ok": True,
                               "mode": res.get("mode", ""),
                               "model": res.get("model", ""),
                               "n_files": len(req.get("files") or []),
                               "elapsed": res["elapsed"],
                               "waf_retries": res.get("waf_retries", 0)})
                        conn.sendall(json.dumps({"ok": True, **res}, ensure_ascii=False).encode())
                    except Exception as e:  # noqa: BLE001
                        audit({"q_len": len(req.get("text", "")), "ok": False,
                               "err": str(e)[:300]})
                        conn.sendall(json.dumps({"ok": False, "err": str(e)},
                                                ensure_ascii=False).encode())
                        # 出错时回收会话, 下次重开(避免脏状态)
                        if backend is not None:
                            backend.close()
                            backend = None
                    conn.close()
                    continue
                conn.sendall(json.dumps({"ok": False, "err": "unknown cmd"}).encode())
                conn.close()
            except Exception as e:  # noqa: BLE001
                try:
                    conn.sendall(json.dumps({"ok": False, "err": str(e)}).encode())
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        if backend is not None:
            backend.close()
        if SOCK.exists():
            SOCK.unlink()
        _shutdown_xvfb_if_owned()
        log("daemon exit")
    return 0


# ---------------- Xvfb ----------------


def _xvfb_alive() -> bool:
    return Path("/tmp/.X11-unix/X99").exists()


def _xvfb_owned_pid() -> int | None:
    try:
        pid = int(XVFB_PIDFILE.read_text().strip())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if cmd and b"Xvfb" in cmd[0] and Path(f"/proc/{pid}").exists():
            return pid
    except Exception:  # noqa: BLE001
        pass
    return None


def _ensure_xvfb() -> bool:
    """按需拉起虚拟屏 :99。返回 True = 本次持有所有权(关闭时应回收)。"""
    if _xvfb_alive():
        return _xvfb_owned_pid() is not None
    proc = subprocess.Popen(
        ["Xvfb", XVFB_DISPLAY, "-screen", "0", XVFB_SCREEN, "-ac", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        XVFB_PIDFILE.write_text(str(proc.pid))
    except Exception:  # noqa: BLE001
        pass
    for _ in range(30):
        time.sleep(0.2)
        if _xvfb_alive():
            return True
    raise RuntimeError("Xvfb :99 启动超时")


def _shutdown_xvfb_if_owned() -> None:
    if os.environ.get("QWEN_XVFB_OWNED") != "1":
        return
    pid = _xvfb_owned_pid()
    if pid:
        try:
            os.kill(pid, 15)
        except Exception:  # noqa: BLE001
            pass
    try:
        XVFB_PIDFILE.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------- client ----------------


def _connect(retry: int = 20) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(SOCK))
        return s
    except OSError:
        s.close()
    if not _daemon_alive():
        owned = _ensure_xvfb()
        env = dict(os.environ)
        env["DISPLAY"] = XVFB_DISPLAY
        env.pop("WAYLAND_DISPLAY", None)   # 见 BrowserBackend 里同款注释
        env["QWEN_XVFB_OWNED"] = "1" if owned else ""
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "daemon"],
            stdout=DAEMON_LOG.open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(HERE),
            start_new_session=True,
        )
        log(f"daemon spawned (DISPLAY={XVFB_DISPLAY}, xvfb_owned={owned}), waiting...")
    for _ in range(retry):
        time.sleep(1)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(SOCK))
            return s
        except OSError:
            s.close()
    raise RuntimeError("daemon 启动超时, 查看 daemon.log")


def _daemon_alive() -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(SOCK))
        s.sendall(b'{"cmd":"ping"}')
        json.loads(s.recv(4096).decode())
        s.close()
        return True
    except Exception:  # noqa: BLE001
        s.close()
        return False


def rpc(payload: dict, wait: int = 600) -> dict:
    s = _connect()
    s.sendall(json.dumps(payload).encode())
    s.settimeout(wait)
    buf = b""
    try:
        while True:
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
            try:
                return json.loads(buf.decode())
            except json.JSONDecodeError:
                continue
    finally:
        s.close()
    raise RuntimeError("空响应")


# ---------------- CLI ----------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Qwen 安全后端（真实浏览器通道）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ask = sub.add_parser("ask")
    a_ask.add_argument("text")
    a_ask.add_argument("--timeout", type=int, default=ASK_TIMEOUT)
    a_ask.add_argument("--search", action="store_true", help="打开网页搜索开关并返回引用来源")
    a_ask.add_argument("--new-chat", action="store_true", help="先开新对话再提问")
    a_ask.add_argument("--files", nargs="+", default=None, help="附件路径(页面内真实上传)")
    a_ask.add_argument("--model", default="", help="左上角下拉选模型(页面名或旧 API id, 模糊匹配)")
    a_ask.add_argument("--think", nargs="?", const="thinking", default="",
                       help="思考档位: thinking/auto/fast（不带值 = thinking）")
    a_ask.add_argument("--mode", default="",
                       help="加号菜单模式: t2i/t2v/slides/web_dev/deep_research/"
                            "preview/study/travel/search，或页面文本")
    a_ask.add_argument("--vparams", default="",
                       help='创建视频参数(JSON): {"clarity":"720P","ratio":"1:1",'
                            '"duration":"10s","vmode":"多参考图"}')
    sub.add_parser("status")
    sub.add_parser("probe", help="打开会话自检, 不发消息")
    a_models = sub.add_parser("models", help="读模型下拉全部条目(含「展开更多模型」二级, 不发消息)")
    a_models.add_argument("--force", action="store_true", help="绕过缓存重读")
    a_pin = sub.add_parser("pin", help="把某模型设为默认(账号级, 会改设置)")
    a_pin.add_argument("model")
    sub.add_parser("configs", help="读当前模式的子配置(比例/分辨率/深度)+全部选项")
    a_mode = sub.add_parser("mode", help="切加号菜单模式(不发消息)")
    a_mode.add_argument("mode", help="t2i/t2v/slides/web_dev/deep_research/preview/"
                                     "study/travel/search，或页面文本；chat=回普通对话")
    a_think = sub.add_parser("think", help="切思考档位(不发消息)")
    a_think.add_argument("level", help="auto|thinking|fast（或 自动/思考/快速）")
    sub.add_parser("vparams", help="读创建视频的参数(清晰度/编辑模式/比例/时长)")
    a_vset = sub.add_parser("vset", help="改创建视频的参数")
    a_vset.add_argument("--clarity", default="", help="720P|1080P(VIP)")
    a_vset.add_argument("--ratio", default="", help="16:9|4:3|1:1|3:4|9:16|原比例")
    a_vset.add_argument("--duration", default="", help="5s|10s|15s")
    a_vset.add_argument("--vmode", default="", help="视频生成|多参考图")
    a_vset.add_argument("--allow-vip", action="store_true", help="允许选 VIP 档(会弹升级)")
    a_setcfg = sub.add_parser("setcfg", help="改输入区子配置(按语义键或序号)")
    a_setcfg.add_argument("value")
    a_setcfg.add_argument("--key", default="", help="ratio|model|depth")
    a_setcfg.add_argument("--index", type=int, default=0)
    sub.add_parser("assets", help="读当前对话最后一条回答的产物(图/视频/文件卡, 不发消息)")
    sub.add_parser("stop")
    sub.add_parser("daemon", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.cmd == "daemon":
        return cmd_daemon()
    if a.cmd == "stop":
        if _daemon_alive():
            rpc({"cmd": "stop"})
            log("daemon stopped")
        else:
            log("daemon not running")
        return 0
    if a.cmd == "status":
        print(json.dumps({
            "daemon": _daemon_alive(),
            "profile": PROFILE.exists(),
            "xvfb": _xvfb_alive(),
            "state": _load_state(),
        }, ensure_ascii=False))
        return 0
    if a.cmd == "probe":
        res = rpc({"cmd": "probe"}, wait=240)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "models":
        res = rpc({"cmd": "models", "force": a.force}, wait=240)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "pin":
        res = rpc({"cmd": "pin", "model": a.model}, wait=240)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "assets":
        res = rpc({"cmd": "assets"}, wait=240)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "configs":
        res = rpc({"cmd": "configs"}, wait=240)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "mode":
        res = rpc({"cmd": "mode", "mode": a.mode}, wait=300)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "think":
        res = rpc({"cmd": "think", "level": a.level}, wait=240)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "vparams":
        res = rpc({"cmd": "vparams"}, wait=240)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "vset":
        want = {k: v for k, v in (("clarity", a.clarity), ("ratio", a.ratio),
                                  ("duration", a.duration), ("vmode", a.vmode)) if v}
        if not want:
            print(json.dumps({"ok": False, "err": "至少给一个参数"}, ensure_ascii=False))
            return 2
        res = rpc({"cmd": "vset", "want": want, "allow_vip": a.allow_vip}, wait=240)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "setcfg":
        res = rpc({"cmd": "setcfg", "value": a.value, "key": a.key, "index": a.index},
                  wait=240)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "ask":
        vp = {}
        if a.vparams:
            try:
                vp = json.loads(a.vparams)
            except json.JSONDecodeError as e:
                print(json.dumps({"ok": False, "err": f"--vparams 不是合法 JSON: {e}"},
                                 ensure_ascii=False))
                return 2
        res = rpc({"cmd": "ask", "text": a.text, "timeout": a.timeout,
                   "search": a.search, "new_chat": a.new_chat, "files": a.files,
                   "model": a.model, "think": a.think, "mode": a.mode,
                   "vparams": vp or None},
                  wait=a.timeout + 300)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
