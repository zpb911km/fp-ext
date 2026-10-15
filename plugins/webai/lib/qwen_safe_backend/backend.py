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
STABLE_POLLS = 3           # 连续 N 次文本不变判定完成
POLL_EVERY = 2.0           # 轮询间隔(秒)

SEL_INPUT = "textarea.message-input-textarea"
SEL_ASSISTANT = ".qwen-chat-message-assistant"
SEL_ANSWER = ".qwen-markdown"
SEL_LOADING = ".response-loading"
SEL_NEWCHAT = "button:has-text('新建对话'), [role=button]:has-text('新建对话')"

# 风控指纹：WAF 挑战页/拦截体里出现的标记
_WAF_MARKS = ("RGV587", "FAIL_SYS_USER_VALIDATE", "x5secdata", "punish")


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

        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            args=[
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
        self.page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(4000)

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

    # -- 自检 -----------------------------------------------------

    def probe(self) -> dict:
        st = {
            "url": self.page.url,
            "logged_in": bool(self._user_token()),
            "input_box": self.page.locator(SEL_INPUT).count() > 0,
            "model": self._model_label(),
        }
        SHOTS.mkdir(exist_ok=True)
        p = SHOTS / f"probe_{int(time.time())}.png"
        try:
            self.page.screenshot(path=str(p))
            st["screenshot"] = str(p)
        except Exception:  # noqa: BLE001
            pass
        return st

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
        try:
            btn = self.page.locator(SEL_NEWCHAT).first
            if btn.count():
                btn.click()
                self.page.wait_for_timeout(1500)
                return
        except Exception:  # noqa: BLE001
            pass
        # 兜底：直接回首页（等价于新对话）
        self.page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(3000)

    # -- 附件 -----------------------------------------------------

    def _upload_files(self, paths: list) -> int:
        abs_paths = [str(Path(p).expanduser().resolve()) for p in paths]
        for p in abs_paths:
            if not Path(p).is_file():
                raise RuntimeError(f"文件不存在: {p}")
        li = self.page.locator("input[type=file]").first
        if li.count() == 0:
            raise RuntimeError("页面未找到文件选择口 input[type=file]")
        li.set_input_files(abs_paths)
        # 等文件芯片出现（前端上传+解析完成才会解锁发送）
        deadline = time.time() + 120
        while time.time() < deadline:
            self.page.wait_for_timeout(500)
            n = self.page.evaluate(
                """() => document.querySelectorAll(
                     '[class*=file-item],[class*=file-card],[class*=upload-item]').length"""
            )
            if n >= len(abs_paths):
                break
        self.page.wait_for_timeout(1200)
        log(f"attached {len(abs_paths)} file(s)")
        return len(abs_paths)

    # -- 读取当前回答状态 -----------------------------------------

    @staticmethod
    def _read(page) -> dict:
        return page.evaluate(
            """() => {
              const boxes = document.querySelectorAll('.qwen-chat-message-assistant');
              const last = boxes.length ? boxes[boxes.length - 1] : null;
              if (!last) return {answer: '', thinking: '', refs: [], loading: false, present: false};
              const md = last.querySelector('.qwen-markdown');
              const answer = md ? (md.innerText || '').trim() : '';
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
              // 思考：默认折叠，只有展开了才有正文
              let thinking = '';
              const th = last.querySelector('.qwen-chat-thinking-content, [class*=thinking-content]');
              if (th) thinking = (th.innerText || '').trim();
              return {
                answer: answer,
                thinking: thinking,
                refs: refs,
                loading: !!last.querySelector('.response-loading'),
                present: true,
              };
            }"""
        )

    # -- 提问 -----------------------------------------------------

    def ask(self, text: str, timeout: int = ASK_TIMEOUT, *,
            new_chat: bool = False, search: bool = False,
            files: list | None = None) -> dict:
        t0 = time.time()
        if new_chat:
            self.new_chat()
        if files:
            self._upload_files(files)

        prev = self._read(self.page)
        prev_len = len(prev.get("answer") or "")
        prev_n = self.page.evaluate("() => document.querySelectorAll('.qwen-chat-message-assistant').length")

        box = self.page.locator(SEL_INPUT).first
        box.click()
        box.press_sequentially(text, delay=random.randint(18, 45))
        self.page.wait_for_timeout(random.randint(400, 900))
        box.press("Enter")
        log(f"sent len={len(text)}")

        last_txt, stable, polls = "", 0, 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.page.wait_for_timeout(int(POLL_EVERY * 1000))
            polls += 1
            cur_st = self._read(self.page)
            n = self.page.evaluate("() => document.querySelectorAll('.qwen-chat-message-assistant').length")
            fresh = cur_st.get("present") and (
                n > prev_n or len(cur_st.get("answer") or "") != prev_len)
            cur = (cur_st.get("answer") or "").strip()
            if fresh and cur and cur == last_txt and not cur_st.get("loading"):
                stable += 1
                if stable >= STABLE_POLLS:
                    break
            else:
                stable = 0
                last_txt = cur

        final = self._read(self.page)
        answer = (final.get("answer") or "").strip()
        elapsed = round(time.time() - t0, 1)

        if not answer:
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
            "elapsed": elapsed,
            "search": bool(search),
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
    a_ask.add_argument("--search", action="store_true", help="返回引用来源")
    a_ask.add_argument("--new-chat", action="store_true", help="先开新对话再提问")
    a_ask.add_argument("--files", nargs="+", default=None, help="附件路径(页面内真实上传)")
    sub.add_parser("status")
    sub.add_parser("probe", help="打开会话自检, 不发消息")
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
    if a.cmd == "ask":
        res = rpc({"cmd": "ask", "text": a.text, "timeout": a.timeout,
                   "search": a.search, "new_chat": a.new_chat, "files": a.files},
                  wait=a.timeout + 300)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
