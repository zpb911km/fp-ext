#!/usr/bin/env python3
"""webai 统一登录 / 刷新入口
================================================================

把四个后端的凭据维护收敛到一个入口 —— 凭据过期时不用再逐个跑 *_login.py。

用法::

    python3 <fp数据目录>/public/webai/login.py              # 检查全部；只刷"确认过期"的（需要时弹窗）
    python3 <fp数据目录>/public/webai/login.py qwen glm     # 只处理指定几家
    python3 <fp数据目录>/public/webai/login.py --check      # 只查状态，不开浏览器（一次轻量请求/家）
    python3 <fp数据目录>/public/webai/login.py --all        # 强制全部刷新（没过期也刷）

状态三态（诚实优先，宁可不刷也不误刷）::

    ok        服务端认可凭据
    dead      服务端明确拒绝 → 需要刷新
    unknown   网络不通 / 端点不确定 / 无 verify() → **不触发自动刷新**

`silent_refresh()` 是给 provider 层用的自愈钩子：headless + 不等人 + 硬超时 +
进程级隔离（跑在子进程里，超时直接 kill，不会挂住调用方）。

⚠️ 与 provider 层是**单向**依赖：本模块 import provider 只为调 verify()；
   provider 只在异常路径上 `from .login import silent_refresh`，且是延迟 import。
"""
import argparse
import importlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# provider → (登录脚本, 显示名)
SPEC = {
    "qwen":     ("qwen_login.py",     "Qwen"),
    "deepseek": ("deepseek_login.py", "DeepSeek"),
    "stepfun":  ("stepfun_login.py",  "StepFun"),
    "glm":      ("glm_login.py",      "GLM"),
}

SILENT_TIMEOUT = 45        # 静默刷新的硬上限（秒）：playwright 启动 + 短轮询
INTERACTIVE_TIMEOUT = 700  # 人工登录的上限（略大于脚本自身的 600s 等待）
_LOCK = os.path.join(os.path.expanduser("~/.local/share/fp"), "webai_login.lock")


# ── 状态检查 ────────────────────────────────────────────────────

def _provider_mod(name: str):
    return importlib.import_module(name)


def _looks_like_auth(text: str) -> bool:
    """用**公共**错误词表再判一次凭据失效。

    为什么需要：provider 的局部 ``classify()`` 只认自家方言（刻意不 import core），
    认不出来就回 ""=unknown；而 deepseek/stepfun 的 verify 是"试一次调用"，
    失败原因只能从异常文本里读 —— 不在这一层用通用词表补一刀，
    ``--check`` 就永远报 unknown，也就永远不会去刷新它们。
    """
    if not text:
        return False
    try:
        core = importlib.import_module("core")          # 脚本模式（sys.path[0]=本目录）
    except Exception:  # noqa: BLE001
        try:
            core = importlib.import_module(".core", __package__ or "webai")   # 包模式
        except Exception:  # noqa: BLE001
            return False
    try:
        return core.classify_text(str(text)) == core.ErrorKind.AUTH
    except Exception:  # noqa: BLE001
        return False


def check(name: str) -> tuple[str, str]:
    """→ (state, message)，state ∈ ok / dead / unknown / nocred"""
    try:
        mod = _provider_mod(name)
    except Exception as e:  # noqa: BLE001
        return "unknown", f"无法加载 provider：{type(e).__name__}: {e}"

    av = getattr(mod, "available", None)
    if callable(av):
        try:
            ok, why = av()
            if not ok:
                return "nocred", why or "本地无凭据"
        except Exception as e:  # noqa: BLE001
            return "unknown", f"available() 异常：{e}"

    vf = getattr(mod, "verify", None)
    if not callable(vf):
        return "unknown", "该后端暂无 verify()，无法判断凭据是否有效"
    try:
        state, msg = vf()
    except Exception as e:  # noqa: BLE001
        return "unknown", f"verify() 异常：{type(e).__name__}: {e}"
    if state == "unknown" and _looks_like_auth(msg):
        return "dead", msg
    return state, msg


# ── 刷新 ────────────────────────────────────────────────────────

def _run_login(name: str, headless: bool, wait: int, timeout: int) -> int:
    """跑登录脚本子进程。返回其返回码；超时/异常返回 -1。"""
    script = os.path.join(HERE, SPEC[name][0])
    if not os.path.isfile(script):
        print(f"⚠️ 找不到登录脚本：{script}")
        return -1
    cmd = [sys.executable, script, "--wait", str(wait)]
    if headless:
        cmd.append("--headless")
    try:
        r = subprocess.run(cmd, timeout=timeout, cwd=HERE)
        return r.returncode
    except subprocess.TimeoutExpired:
        print(f"⚠️ {SPEC[name][1]} 登录超时（>{timeout}s），已终止")
        return -1
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ {SPEC[name][1]} 登录异常：{type(e).__name__}: {e}")
        return -1


def _acquire_lock() -> int | None:
    """并发保护：两个工具同时发现过期时，只允许一个去开浏览器。"""
    try:
        os.makedirs(os.path.dirname(_LOCK), exist_ok=True)
        fd = os.open(_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except FileExistsError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _release_lock(fd) -> None:
    try:
        os.close(fd)
        os.unlink(_LOCK)
    except Exception:  # noqa: BLE001
        pass


def silent_refresh(name: str, timeout: int = SILENT_TIMEOUT) -> bool:
    """**给 provider 层自愈用**：无头、不等人、硬超时、带并发锁。

    只在凭据"确实过期"时才有意义（调用方应确认是 AUTH 类错误再调）。
    成功回来 True —— 调用方据此重试一次。
    """
    if os.environ.get("FP_WEBAI_NO_AUTOLOGIN"):
        return False
    if name not in SPEC:
        return False

    fd = _acquire_lock()
    if fd is None:
        return False                      # 已有刷正在跑，别叠加
    try:
        print(f"[webai] 凭据疑似过期，尝试静默刷新 {SPEC[name][1]}…", file=sys.stderr)
        rc = _run_login(name, headless=True, wait=0, timeout=timeout)
        if rc != 0:
            print(f"[webai] 静默刷新失败（rc={rc}）—— 需要人工登录：\n"
                  f"    python3 {os.path.join(HERE, SPEC[name][0])}", file=sys.stderr)
            return False
        state, msg = check(name)
        if state == "ok":
            print(f"[webai] {SPEC[name][1]} 凭据已刷新", file=sys.stderr)
            return True
        print(f"[webai] 刷新后仍未通过校验（{state}: {msg}）", file=sys.stderr)
        return False
    finally:
        _release_lock(fd)


def refresh(name: str, headless: bool = False) -> bool:
    """交互式刷新（可能弹浏览器等你登录）。"""
    print(f"\n──── {SPEC[name][1]} ────")
    rc = _run_login(name, headless=headless,
                    wait=0 if headless else 600, timeout=INTERACTIVE_TIMEOUT)
    if rc == 0:
        state, msg = check(name)
        print(f"  → 校验：{state} {msg}")
        return state == "ok"
    return False


# ── CLI ─────────────────────────────────────────────────────────

_MARK = {"ok": "✅", "dead": "❌", "unknown": "❔", "nocred": "⭕"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="login.py",
        description="webai 统一登录 / 刷新入口（检查凭据并按需刷新）")
    ap.add_argument("providers", nargs="*", help="只处理这几家（默认全部）")
    ap.add_argument("--check", action="store_true", help="只查状态，不开浏览器")
    ap.add_argument("--all", action="store_true", help="强制全部刷新（不过期也刷）")
    ap.add_argument("--headless", action="store_true", help="刷新时也用无头（不弹窗）")
    a = ap.parse_args(argv)

    names = [n.lower() for n in a.providers] or list(SPEC)
    bad = [n for n in names if n not in SPEC]
    if bad:
        print(f"未知 provider：{', '.join(bad)}（可用：{', '.join(SPEC)}）")
        return 1

    print("检查凭据状态…")
    states = {n: check(n) for n in names}
    for n, (st, msg) in states.items():
        print(f"  {_MARK.get(st, '?')} {SPEC[n][1]:<9} {st:<8} {msg}")

    if a.check:
        return 0

    todo = names if a.all else [n for n, (st, _) in states.items() if st in ("dead", "nocred")]
    # unknown 一律不刷：网络问题/端点不确定时，刷了也是白弹窗
    skipped = [n for n, (st, _) in states.items() if st == "unknown"]
    if skipped:
        print(f"\n（{'/'.join(SPEC[n][1] for n in skipped)} 状态未知，不主动刷新）")

    if not todo:
        print("\n无需刷新 ✅")
        return 0

    print(f"\n需要刷新：{', '.join(SPEC[n][1] for n in todo)}")
    ok = []
    for n in todo:
        try:
            if refresh(n, headless=a.headless):
                ok.append(n)
        except KeyboardInterrupt:
            print("\n已中断。")
            return 130

    print(f"\n──── 结果：{len(ok)}/{len(todo)} 成功 ────")
    for n in ok:
        print(f"  ✅ {SPEC[n][1]}")
    for n in todo:
        if n not in ok:
            print(f"  ❌ {SPEC[n][1]}（仍未通过，可重跑或手动登录）")
    return 0 if len(ok) == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
