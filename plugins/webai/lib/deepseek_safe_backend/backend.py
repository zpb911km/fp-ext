#!/usr/bin/env python3
"""DeepSeek 安全后端（隔离开发版 v0）——真实浏览器驱动，无任何模拟请求。

设计原则（长期安全）:
  1. 真 Chrome (Google Chrome 154, user-local 解包) + --disable-blink-features=AutomationControlled
     → navigator.webdriver=false, UA/uaData 品牌一致
  2. 持久化 profile, 单 tab, headed, 不隐藏不切走 → SSE 心跳/rAF 不受后台节流
  3. 深度思考默认强制开启（发送前校验 aria-pressed, 未开则点击并复验）
  4. 打字机输入(jitter)。（旧"人类节奏" MIN_INTERVAL/DAILY_CAP 已于 2026-09-25 按用户决定移除：
     封号问题已彻底修复，不再限流/限次）
  5. 全量审计日志: 每次提问/每次异常落 asks.log
  6. 提取锚定: 发送前快照 → 等待文本确实变化 → 连续稳定才判定完成（防截断/防取到历史）

子命令:
  status               报告会话状态(不发消息, 不唤醒)
  ask "问题"           提问并返回 JSON {thinking, answer, elapsed}
  stop                 停止 daemon(含其持有的 Xvfb)

生命周期(懒加载): 第一次调用时检查并拉起 Xvfb(:99) + daemon + Chrome;
无操作 IDLE_SEC(1h) 后 daemon 自动关闭浏览器会话与自持有的 Xvfb, 下次调用再起。

禁止: 参考/复用任何旧模拟后端代码; 伪造 API/PoW 请求。（"高频连发"禁令随节奏保护一并移除）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHROME = Path.home() / ".local/opt/google-chrome/opt/google/chrome/chrome"
DS_DIR = Path.home() / ".local/share/fp/webai/deepseek"
PROFILE = DS_DIR / "profile"
SOCK = HERE / "ds_backend.sock"
DAEMON_LOG = HERE / "daemon.log"
ASKS_LOG = HERE / "asks.log"
STATE = HERE / "state.json"

ASK_TIMEOUT = 300          # 单问等待上限(秒)
PASTE_THRESHOLD = 600      # 超过这么多字改用 fill 一次性写入：逐字敲 18~45ms/字
                           # 会在 ~900 字处撞 Playwright 默认 30s 动作超时（2026-10 实测）
IDLE_SEC = 3600            # 无操作自动关闭(秒)
XVFB_DISPLAY = ":99"
XVFB_SCREEN = "1920x1080x24"
XVFB_PIDFILE = HERE / "xvfb.pid"
STABLE_POLLS = 4           # 连续 N 次文本不变才算完（仅在观测不到流事件时兜底用）
POLL_EVERY = 2.5           # 轮询间隔(秒)
# ── 完成判定的肯定式信号（2026-11-27 重做，反思58 的同源根治）────────────
# 「文本没变 N 次」只是否定式证据：流式生成中 ≥6s 的间隙会被误判成结束（反思58 的
# 截断），而读数取错范围时又会永不成立（本家 2026-11-26 的假超时）。真信号在网络层：
# SSE `POST /api/v0/chat/completion` 的 requestfinished = 服务端把正文流写完了。
# 故判定 = 流结束 + DOM 再稳 SETTLE_POLLS 轮；观测不到流事件(页面改版)才退回稳定窗口，
# 且兜底窗口比旧值更保守。
STREAM_URL_KEY = "/chat/completion"
SETTLE_POLLS = 1           # 流结束后再多稳几轮（等 markdown/产物渲染落定）
FALLBACK_STABLE_POLLS = 5  # 兜底：未观测到流事件时 N 次不变才算完（≈12.5s）
STREAM_PROBE_SEC = 15      # 发出后多久仍没看到流请求 → 视为端点不可观测

SELECTOR_INPUT = "textarea[placeholder*='DeepSeek']"
SELECTOR_THINK = "div.ds-toggle-button:has-text('深度思考')"
SELECTOR_SEARCH = "div.ds-toggle-button:has-text('智能搜索')"
SELECTOR_NEWCHAT = "button:has-text('开启新对话'), [role=button]:has-text('开启新对话')"


def log(msg: str) -> None:
    if os.environ.get("DS_QUIET"):
        return  # 调用方(插件)需要纯净 JSON, 不混入日志行
    line = f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)


def audit(rec: dict) -> None:
    rec["ts"] = dt.datetime.now().isoformat(timespec="seconds")
    with ASKS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------- 节奏控制（已移除） ----------------
# 旧 pacing_check/pacing_commit（间隔+每日配额）已按用户 2026-09-25 决定移除。
# state.json 仅保留作审计读数，不再对 ask 施加任何限制。
def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"last_ask": 0.0, "day": "", "count": 0}


# ---------------- 浏览器会话 ----------------
class BrowserBackend:
    def __init__(self):
        from playwright.sync_api import sync_playwright

        if not CHROME.exists():
            raise RuntimeError(f"找不到真 Chrome: {CHROME}")
        if not PROFILE.exists():
            raise RuntimeError(f"找不到登录 profile: {PROFILE}")
        # 虚拟屏自愈: 若 Xvfb 中途挂掉则按需重启(重启者即持有者)
        if not _xvfb_alive():
            if _ensure_xvfb():
                os.environ["DS_XVFB_OWNED"] = "1"
        os.environ["DISPLAY"] = XVFB_DISPLAY
        # ⚠️ KDE/Wayland 会话里 WAYLAND_DISPLAY 会被继承下来, chrome 优先走 wayland
        # ozone → 连不上 Xvfb(:99) → 秒退, playwright 只报 "Target page, context or
        # browser has been closed"(2026-10-18 实测根因)。
        os.environ.pop("WAYLAND_DISPLAY", None)
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(PROFILE),
            executable_path=str(CHROME),
            headless=False,
            args=[
                # KDE/Wayland 会话: chrome 见 WAYLAND_DISPLAY/XDG_SESSION_TYPE=wayland
                # 就去连 wayland(虚拟屏 :99 上没有) → 秒退。钉死 x11 ozone 后端。
                "--ozone-platform=x11",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        # 只允许单 tab, 防止隐藏态
        for pg in self.ctx.pages[1:]:
            pg.close()
        # 流式完成观察器：只记时间戳，**不在回调里做 IO**
        # （sync API 的回调跑在事件泵线程，回调里调 r.text() 会阻塞/死锁）。
        self._stream_reqs: list = []      # completions 请求开始时刻
        self._stream_done: list = []      # completions 响应结束时刻（= 服务端写完）
        # 端点自我发现：记录所有 POST 路径，流端点改版时能从日志直接读出真端点
        self._post_paths: list = []
        self.page.on("request", self._rec_post_path)
        self.page.on("request", self._on_stream_request)
        self.page.on("requestfinished", self._on_stream_finished)
        self.page.on("requestfailed", self._on_stream_finished)
        self.page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")
        self.page.wait_for_selector(SELECTOR_INPUT, timeout=30000)
        self.page.wait_for_timeout(2000)

    def thinking_state(self) -> dict:
        t = self.page.locator(SELECTOR_THINK).first
        if t.count() == 0:
            return {"present": False, "active": False}
        aria = t.get_attribute("aria-pressed")
        return {"present": True, "active": aria == "true", "aria": aria}

    def ensure_thinking(self) -> dict:
        st = self.thinking_state()
        if not st.get("present"):
            # 聚焦输入框让开关出现
            self.page.locator(SELECTOR_INPUT).first.click()
            self.page.wait_for_timeout(800)
            st = self.thinking_state()
        if st.get("present") and not st.get("active"):
            self.page.locator(SELECTOR_THINK).first.click()
            self.page.wait_for_timeout(800)
            st = self.thinking_state()
        if not st.get("active"):
            raise RuntimeError(f"深度思考开关无法开启: {st}")
        return st

    # -- 流式完成观察器（肯定式信号，见文件头 STREAM_URL_KEY 注释）--------

    def _rec_post_path(self, req) -> None:
        """记 POST 路径（去 query、去域名），诊断流端点改版用。回调里不做 IO。"""
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
        """系统提示 → 立刻抛（不再空转到超时）。分类词表见 core.classify_text。"""
        t = (notice or "").strip()
        low = t.lower()
        if any(k in t for k in ("上限", "额度", "升级会员", "积分", "次数", "配额")) \
                or "quota" in low or "limit" in low:
            return RuntimeError(f"deepseek 额度/频率已用完（quota）：{t}")
        if any(k in t for k in ("登录", "失效", "过期")) or "login" in low:
            return RuntimeError(f"deepseek 登录态失效（auth）：{t}")
        if any(k in t for k in ("繁忙", "稍后", "失败", "重试", "网络")) \
                or "busy" in low or "retry" in low:
            return RuntimeError(f"deepseek 服务端异常（transient）：{t}")
        return RuntimeError(f"deepseek 页面系统提示（{where}）：{t}")

    # -- 读数 -------------------------------------------------------

    # 助手正文的**语义类名**（非哈希，2026-11-27 实测稳定）：
    #   div.ds-message > div.ds-markdown.ds-assistant-message-main-content
    # 思考块在 div.ds-think-content 里另有一份 .ds-markdown —— 别混进来。
    SEL_ANSWER = ".ds-assistant-message-main-content"

    @classmethod
    def _read_answer(cls, page) -> dict:
        """读**最后一条** assistant 回答（返回 {n, text, notice}）。

        旧实现 `_best_markdown` 在 `[class*=markdown]` 里挑 innerText **最长**的那个：
        同一会话里只要存在一条比新回答更长的历史回答（实测：前面一轮输出了 120 行
        `L{n}: ok`），"最长文本"就**永不变化** → 完成判定里的 `cur != pre` 永假 →
        死等到 300s 报"未观测到新文本"，而页面其实早就答完了（2026-11-26 截图实证）。
        """
        return page.evaluate(
            """() => {
              const els = document.querySelectorAll('.ds-assistant-message-main-content');
              const n = els.length;
              const last = n ? els[n - 1] : null;
              const text = last ? (last.innerText || '').trim() : '';
              // 系统提示/错误卡（额度耗尽、服务繁忙、登录失效）：这类消息**没有正文**，
              // 旧逻辑当作"还没出文本"，一路空转到超时。这里捞出来交给上层立刻抛错。
              let notice = '';
              const kw = /上限|额度|升级会员|频繁|繁忙|登录|失效|过期|验证|网络|连接|断开|异常|错误|quota|limit|expire/i;
              for (const el of document.querySelectorAll(
                    '[class*=error], [class*=Error], [class*=limit], [class*=notice], [class*=toast]')) {
                const t = (el.innerText || '').trim();
                if (t && t.length < 240 && kw.test(t)) { notice = t; break; }
              }
              if (!notice && !text && last) {
                // 兜底：末条既无正文、也无错误卡类名 —— 只有命中关键词的短文案才当真
                const t = (last.innerText || '').trim();
                if (t && t.length < 240 && kw.test(t)) notice = t;
              }
              return {n, text, notice};
            }"""
        )

    # -- 新对话 -----------------------------------------------------

    def new_chat(self) -> str:
        """切到全新对话，并**校验真的切了**。

        旧实现只点不验：`SELECTOR_NEWCHAT` 命中的是嵌套 div，点到被遮挡的外层等于没点，
        仍留在旧会话里 → 下一问的读数被上一条长回答遮蔽（症状 = 永久超时）。
        2026-11-27 截图实证（"点了新对话、同一会话里已累积 2 轮问答"）。
        """
        for attempt in (1, 2, 3):
            if self._read_answer(self.page)["n"] == 0:
                return self.page.url
            btns = self.page.locator(SELECTOR_NEWCHAT)
            if btns.count():
                try:
                    btns.last.click(timeout=8000)     # 最内层节点才是真按钮
                except Exception as e:  # noqa: BLE001
                    log(f"new_chat click failed: {str(e)[:80]}")
            else:
                # 兜底：回首页 = 新对话
                self.page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")
            try:
                self.page.wait_for_function(
                    "() => document.querySelectorAll("
                    "'.ds-assistant-message-main-content').length === 0",
                    timeout=8000)
                log("new chat opened")
                return self.page.url
            except Exception:  # noqa: BLE001
                log(f"new_chat 未生效(第{attempt}次)，页面上仍有历史回答")
        raise RuntimeError("无法开启新对话：页面上仍留有历史回答（--new-chat 失效）")

    def _upload_files(self, paths: list) -> int:
        """页面内上传: 只做 set_input_files, PoW/FormData/refFileIds 全部由前端
        自己完成(对话附件与搜索互斥由前端 v9 规则处理)。返回成功上传数。"""
        abs_paths = [str(Path(p).resolve()) for p in paths]
        for p in abs_paths:
            if not Path(p).is_file():
                raise RuntimeError(f"文件不存在: {p}")
        li = self.page.locator("input[type=file]").first
        if li.count() == 0:
            raise RuntimeError("页面未找到文件选择口 input[type=file]")
        got = []

        def on_resp(r):
            try:
                if "/api/v0/file/upload_file" in r.url and r.request.method == "POST":
                    got.append(r)
            except Exception:
                pass

        self.page.on("response", on_resp)
        try:
            li.set_input_files(abs_paths)   # input[multiple] 一次全部触发
            deadline = time.time() + 90
            while len(got) < len(abs_paths) and time.time() < deadline:
                self.page.wait_for_timeout(300)
        finally:
            try:
                self.page.remove_listener("response", on_resp)
            except Exception:
                pass
        if len(got) < len(abs_paths):
            raise RuntimeError(
                f"上传未完成: 只收到 {len(got)}/{len(abs_paths)} 个 upload_file 响应")
        bad = []
        for r in got:
            try:
                if r.status != 200:
                    bad.append(str(r.status))
            except Exception:
                pass
        if bad:
            raise RuntimeError(f"上传失败 HTTP {','.join(bad)}")
        # 等前端把文件芯片/refFileIds 落进状态, 再允许发送
        self.page.wait_for_timeout(1200)
        log(f"uploaded {len(got)} file(s)")
        return len(got)


    @staticmethod
    def _bulk_input(box, text: str) -> str:
        """长文本一次性写入，返回实际生效的方式（fill / js / insert / type）。"""
        try:
            box.fill(text)
            if (box.input_value() or "") == text:
                return "fill"
        except Exception:  # noqa: BLE001
            pass
        try:
            box.evaluate(
                """(el, t) => {
                  const p = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                  Object.getOwnPropertyDescriptor(p, 'value').set.call(el, t);
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                }""",
                text,
            )
            if (box.input_value() or "") == text:
                return "js"
        except Exception:  # noqa: BLE001
            pass
        try:
            box.click()
            box.page.keyboard.insert_text(text)
            if (box.input_value() or "") == text:
                return "insert"
        except Exception:  # noqa: BLE001
            pass
        try:
            box.fill("")
        except Exception:  # noqa: BLE001
            pass
        return "type"

    def ask(self, text: str, timeout: int = ASK_TIMEOUT, *,
            new_chat: bool = False, search: bool = False,
            files: list | None = None) -> dict:
        t0 = time.time()
        if new_chat:
            self.new_chat()          # 带校验：点了没生效会重试/报错，绝不静默留在旧会话
        self.ensure_thinking()
        search_blocked = False
        if files:
            # 前端同款互斥规则: 文件在场 → 搜索禁用(uploadBlockedBySearch)
            if search:
                search = False
                search_blocked = True
            self._upload_files(files)
        if search:
            # 智能搜索: 与深度思考同款 ds-toggle-button, 按需点亮
            t = self.page.locator(SELECTOR_SEARCH).first
            if t.count() == 0:
                self.page.locator(SELECTOR_INPUT).first.click()
                self.page.wait_for_timeout(600)
                t = self.page.locator(SELECTOR_SEARCH).first
            if t.count() and t.get_attribute("aria-pressed") != "true":
                t.click()
                self.page.wait_for_timeout(600)
        pre_st = self._read_answer(self.page)
        pre, pre_n = pre_st["text"], pre_st["n"]

        box = self.page.locator(SELECTOR_INPUT).first
        box.click()
        # 长文本一次性写入；逐字敲 18~45ms/字 会撞 30s 动作超时
        sent_via = "type"
        if len(text) > PASTE_THRESHOLD:
            sent_via = self._bulk_input(box, text)
        if sent_via == "type":
            # 打字机输入, jitter 更接近人类；超时按长度放大
            box.press_sequentially(
                text, delay=random.randint(18, 45),
                timeout=max(30_000, int(len(text) * 60)),
            )
        self.page.wait_for_timeout(random.randint(400, 900))
        self._stream_reset()            # 必须在按下 Enter 前清空，才能只数本次的流
        # ⚠️ send_ts 必须在 press 之前取：press 会同步派发 keydown，请求可能在
        # `press()` 返回后的几毫秒内就发出，若在其后再取时间戳，请求时刻会**早于**
        # send_ts，`_stream_seen()` 的 `t >= since` 就永远不成立（2026-11-27 实测：
        # 日志出现「流事件=N」但端点明明命中，兜底路径被误触发）。
        send_ts = time.time()
        box.press("Enter")
        sent_at = time.strftime("%H:%M:%S")
        log(f"sent @{sent_at} len={len(text)} via={sent_via}")

        # ── 完成判定 ────────────────────────────────────────────────
        # 读数永远取"最后一条 assistant 回答"（不是全页最长），新鲜度靠节点计数增长；
        # 结束靠 SSE 流结束（肯定式信号），只有观测不到流事件时才退回稳定窗口兜底。
        changed = False
        last, stable, polls = "", 0, 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.page.wait_for_timeout(int(POLL_EVERY * 1000))
            polls += 1
            st = self._read_answer(self.page)
            cur, notice = st["text"], (st["notice"] or "").strip()
            if notice and not cur:
                raise self._notice_error(notice, "ask:poll")     # 额度/繁忙/登录 → 立刻抛
            fresh = st["n"] > pre_n or (cur and cur != pre)
            if not (fresh and cur):
                # 流已结束却还没正文：给足宽限（STREAM_PROBE_SEC）再下结论，
                # 否则短问答里"流 2s 结束、DOM 稍后才渲染"会被误判成"被拒"。
                if (self._stream_finished(send_ts) and polls >= 3
                        and time.time() - send_ts > STREAM_PROBE_SEC):
                    raise RuntimeError(
                        "deepseek 服务端已结束但没有回答正文（疑似被拒/页面异常）: "
                        + (notice or "无系统提示")[:200]
                        + f"; POST端点={self._post_paths_tail()}")
                stable, last = 0, cur
                continue
            changed = True
            if cur != last:
                stable, last = 0, cur
                continue
            stable += 1
            if self._stream_finished(send_ts):
                if stable >= SETTLE_POLLS:
                    break
            elif (not self._stream_seen(send_ts)
                  and time.time() - send_ts > STREAM_PROBE_SEC
                  and stable >= FALLBACK_STABLE_POLLS):
                log("⚠️ 未观测到 completion 流事件，退回稳定窗口兜底；"
                    f"POST端点={self._post_paths_tail()}")
                break

        answer = last if changed else ""
        if answer:
            # 每次成功都留一行端点快照：流端点改版时，上一行日志就能给出真端点
            log(f"done len={len(answer)} "
                f"流事件={'Y' if self._stream_seen(send_ts) else 'N'} "
                f"POST端点={self._post_paths_tail(5)}")
        if not answer:
            # 超时诊断: 截图留证
            shots = HERE / "shots"
            shots.mkdir(exist_ok=True)
            p = shots / f"timeout_{int(time.time())}.png"
            self.page.screenshot(path=str(p))
            raise RuntimeError(f"等待回答超时(未观测到新文本), 截图: {p}; "
                               f"POST端点={self._post_paths_tail()}")

        elapsed = round(time.time() - t0, 1)
        tb = self.page.evaluate(
            """() => {
              const out = [];
              document.querySelectorAll('[class*=think], [class*=reasoning], [class*=thought]').forEach(el => {
                const t = (el.innerText || '').trim();
                if (t.length > 40 && !t.includes('智能搜索')) out.push({len: t.length, head: t.slice(0, 60)});
              });
              return out.slice(0, 5);
            }"""
        )
        refs: list = []
        if search:
            refs = self.page.evaluate(
                """() => {
                  // 取**最后一条**回答里的外链（旧实现取全页最长 markdown → 会取到历史回答）。
                  const els = document.querySelectorAll('.ds-assistant-message-main-content');
                  const best = els.length ? els[els.length - 1] : null;
                  if (!best) return [];
                  const out = [], seen = new Set();
                  best.querySelectorAll('a[href]').forEach(a => {
                    const h = a.href || '';
                    if (!/^https?:/.test(h) || h.includes('deepseek.com')) return;
                    if (seen.has(h)) return;
                    seen.add(h);
                    out.push({title: (a.innerText || '').trim().slice(0, 120), url: h});
                  });
                  return out.slice(0, 20);
                }"""
            )
        return {"thinking": True, "thinking_blocks": tb,
                "answer": answer, "elapsed": elapsed,
                "references": refs, "search": bool(search),
                "files": len(files or []),
                "search_blocked_by_files": search_blocked}

    def close(self):
        try:
            self.ctx.close()
            self._pw.stop()
        except Exception:
            pass


# ---------------- daemon ----------------
def cmd_daemon() -> int:
    # 清理旧 socket
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
                if cmd in ("ask", "probe", "models"):
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
                        st = backend.ensure_thinking()
                        tb = backend.page.evaluate(
                            """() => {
                              const out = [];
                              document.querySelectorAll('[class*=think], [class*=reasoning], [class*=thought]').forEach(el => {
                                const t = (el.innerText || '').trim();
                                if (t.length > 40 && !t.includes('智能搜索')) {
                                  out.push({cls: String(el.className).slice(0, 60), len: t.length, head: t.slice(0, 80)});
                                }
                              });
                              return out.slice(0, 5);
                            }"""
                        )
                        shots = HERE / "shots"
                        shots.mkdir(exist_ok=True)
                        sp = shots / f"probe_{int(time.time())}.png"
                        backend.page.screenshot(path=str(sp))
                        conn.sendall(json.dumps(
                            {"ok": True, "thinking": st, "thinking_blocks": tb,
                             "screenshot": str(sp),
                             "url": backend.page.url}, ensure_ascii=False).encode())
                    except Exception as e:
                        conn.sendall(json.dumps({"ok": False, "err": str(e)}, ensure_ascii=False).encode())
                        if backend is not None:
                            backend.close()
                            backend = None
                    conn.close()
                    continue
                if cmd == "models":
                    # 模型清单枚举：只读页面自己写下的配置（localStorage 缓存优先，
                    # 缺了才在页面上下文里让页面自己 fetch——不伪造任何请求）。
                    try:
                        if backend is None:
                            backend = BrowserBackend()
                            log("browser session opened")
                        data = backend.page.evaluate(
                            """async () => {
                              const KEY = '__ds_remote_feature_store_model';
                              const BASE = '__ds_remote_feature_store';
                              const parseStore = (s) => {
                                if (!s) return null;
                                try {
                                  const j = JSON.parse(s);
                                  const e = j && j.entries;
                                  if (e && e.model_configs) return e.model_configs;
                                } catch (err) {}
                                return null;
                              };
                              let configs = parseStore(localStorage.getItem(KEY))
                                         || parseStore(localStorage.getItem(BASE));
                              let via = 'localStorage';
                              if (!configs) {
                                // 页面自身数据缺失 → 让页面自己去拉（带 cookie 的同源 fetch）
                                const did = localStorage.getItem('__ds_remote_feature_did') || '';
                                const r = await fetch('/api/v0/client/settings?did=' + encodeURIComponent(did),
                                                      {credentials: 'include'});
                                if (r.ok) {
                                  const j = await r.json();
                                  const walk = (o) => {
                                    if (!o || typeof o !== 'object') return null;
                                    if (o.model_configs) return o.model_configs;
                                    for (const v of Object.values(o)) {
                                      const hit = walk(v);
                                      if (hit) return hit;
                                    }
                                    return null;
                                  };
                                  configs = walk(j);
                                }
                                via = 'page-fetch';
                              }
                              return {configs: configs, via: via};
                            }"""
                        )
                        conn.sendall(json.dumps({"ok": True, **data}, ensure_ascii=False).encode())
                    except Exception as e:
                        conn.sendall(json.dumps({"ok": False, "err": str(e)}, ensure_ascii=False).encode())
                        if backend is not None:
                            backend.close()
                            backend = None
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
                                          files=req.get("files") or None)
                        audit({"q_len": len(req["text"]), "ok": True,
                               "n_files": len(req.get("files") or []),
                               "elapsed": res["elapsed"]})
                        conn.sendall(json.dumps({"ok": True, **res}, ensure_ascii=False).encode())
                    except Exception as e:
                        audit({"q_len": len(req.get("text", "")), "ok": False, "err": str(e)[:300]})
                        conn.sendall(json.dumps({"ok": False, "err": str(e)}, ensure_ascii=False).encode())
                        # 出错时回收会话, 下次重开(避免脏状态)
                        if backend is not None:
                            backend.close()
                            backend = None
                    conn.close()
                    continue
                conn.sendall(json.dumps({"ok": False, "err": "unknown cmd"}).encode())
                conn.close()
            except Exception as e:
                try:
                    conn.sendall(json.dumps({"ok": False, "err": str(e)}).encode())
                    conn.close()
                except Exception:
                    pass
    finally:
        if backend is not None:
            backend.close()
        if SOCK.exists():
            SOCK.unlink()
        _shutdown_xvfb_if_owned()
        log("daemon exit")
    return 0


# ---------------- client ----------------
def _xvfb_alive() -> bool:
    return Path("/tmp/.X11-unix/X99").exists()


def _xvfb_owned_pid() -> int | None:
    """pidfile 证明是本后端此前懒启动的 Xvfb → 可接管所有权。"""
    try:
        pid = int(XVFB_PIDFILE.read_text().strip())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if cmd and b"Xvfb" in cmd[0] and Path(f"/proc/{pid}").exists():
            return pid
    except Exception:
        pass
    return None


def _ensure_xvfb() -> bool:
    """首次调用时按需拉起虚拟屏 :99。返回 True = 本次持有所有权(关闭时应回收)。"""
    if _xvfb_alive():
        return _xvfb_owned_pid() is not None
    proc = subprocess.Popen(
        ["Xvfb", XVFB_DISPLAY, "-screen", "0", XVFB_SCREEN, "-ac", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        XVFB_PIDFILE.write_text(str(proc.pid))
    except Exception:
        pass
    for _ in range(30):
        time.sleep(0.2)
        if _xvfb_alive():
            return True
    raise RuntimeError("Xvfb :99 启动超时")


def _shutdown_xvfb_if_owned() -> None:
    if os.environ.get("DS_XVFB_OWNED") != "1":
        return
    pid = _xvfb_owned_pid()
    if pid:
        try:
            os.kill(pid, 15)
        except Exception:
            pass
    try:
        XVFB_PIDFILE.unlink(missing_ok=True)
    except Exception:
        pass


def _connect(retry: int = 20) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(SOCK))
        return s
    except OSError:
        s.close()
    # 第一次调用: 检查并按需启动 Xvfb + daemon(daemon 再懒加载 Chrome)
    if not _daemon_alive():
        owned = _ensure_xvfb()
        env = dict(os.environ)
        env["DISPLAY"] = XVFB_DISPLAY
        env.pop("WAYLAND_DISPLAY", None)   # 见 BrowserBackend 里同款注释
        env["DS_XVFB_OWNED"] = "1" if owned else ""
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
    except Exception:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="DeepSeek 安全后端(隔离版)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ask = sub.add_parser("ask")
    a_ask.add_argument("text")
    a_ask.add_argument("--timeout", type=int, default=ASK_TIMEOUT)
    a_ask.add_argument("--search", action="store_true", help="开启智能搜索并返回 references")
    a_ask.add_argument("--new-chat", action="store_true", help="先开新对话再提问")
    a_ask.add_argument("--files", nargs="+", default=None,
                       help="附件路径(页面内真实上传, PoW 由前端计算)")
    sub.add_parser("status")
    sub.add_parser("probe", help="打开会话验证深度思考开关, 不发消息")
    sub.add_parser("models", help="枚举网页版可选模型清单(读页面自身配置, 不发聊天请求)")
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
        alive = _daemon_alive()
        st = _load_state()
        print(json.dumps({"daemon": alive, "state": st, "chrome": CHROME.exists()},
                         ensure_ascii=False))
        return 0
    if a.cmd == "probe":
        res = rpc({"cmd": "probe"}, wait=180)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    if a.cmd == "models":
        res = rpc({"cmd": "models"}, wait=180)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 2
    if a.cmd == "ask":
        res = rpc({"cmd": "ask", "text": a.text, "timeout": a.timeout,
                   "search": a.search, "new_chat": a.new_chat,
                   "files": a.files},
                  wait=a.timeout + 120)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
