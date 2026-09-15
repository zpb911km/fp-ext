#!/usr/bin/env python3
"""webai 登录层离线测试 —— 不联网、不启动浏览器、不 spawn 子进程。

跑法::

    cd <data>/public/plugins/webai/lib && python3 tests/test_login_layer.py

覆盖：
  * SPEC 与注册表一致；每家都有登录脚本
  * 四家都有 verify()，且**网络异常 → unknown**（绝不误报 dead → 绝不误触发登录）
  * 认证类文本能被识别（judge 升级 unknown→dead 的那一刀）
  * silent_refresh 的护栏：开关 / 未知 provider / 并发锁
  * 自愈代理：只拦 ask/search/upload/poll；AUTH→刷新+重试一次；非 AUTH 不拦
"""

import importlib
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

_ok = _fail = 0
_failed = []


def chk(cond, msg):
    global _ok, _fail
    if cond:
        _ok += 1
    else:
        _fail += 1
        _failed.append(msg)
        print(f"  ✗ {msg}")


def load():
    for k in [k for k in list(sys.modules) if k == "webai" or k.startswith("webai.")]:
        sys.modules.pop(k, None)
    spec = importlib.util.spec_from_file_location(
        "webai", os.path.join(PKG, "__init__.py"), submodule_search_locations=[PKG]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webai"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Boom:
    """任何调用都抛指定异常的替身（模拟网络故障 / 鉴权失败）。"""

    def __init__(self, exc):
        self._exc = exc

    def __call__(self, *a, **k):
        raise self._exc

    def __getattr__(self, _k):
        def _raise(*a, **k):
            raise self._exc
        return _raise


def main():
    if PKG not in sys.path:
        sys.path.insert(0, PKG)          # login.py 是脚本式（import qwen / import core）
    webai = load()
    login = importlib.import_module("login")

    print("== 1. SPEC 与注册表一致 ==")
    chk(set(login.SPEC) == set(webai.names()),
        f"SPEC 覆盖全部 provider（{sorted(login.SPEC)} vs {sorted(webai.names())}）")
    for n, (script, _label) in login.SPEC.items():
        chk(os.path.isfile(os.path.join(PKG, script)), f"{n} 的登录脚本存在：{script}")

    print("== 2. 四家都有 verify()，且离线异常 → unknown（不误报 dead）==")
    for n in webai.names():
        raw = webai.raw(n)
        chk(callable(getattr(raw, "verify", None)), f"{n}.verify() 存在")

    # qwen / glm 走只读 GET
    for n in ("qwen", "glm"):
        raw = webai.raw(n)
        if not hasattr(raw, "_session"):
            continue
        old = raw._session
        raw._session = lambda *a, **k: _Boom(RuntimeError("net down"))
        try:
            st, msg = raw.verify()
        except Exception as e:  # noqa: BLE001
            st, msg = "EXC", str(e)
        finally:
            raw._session = old
        chk(st == "unknown", f"{n}.verify() 网络异常 → unknown（得到 {st}）")

    # deepseek / stepfun 走 new_session 探针
    for n in ("deepseek", "stepfun"):
        raw = webai.raw(n)
        old = raw.new_session
        raw.new_session = _Boom(RuntimeError("network unreachable"))
        try:
            st, msg = raw.verify()
        except Exception as e:  # noqa: BLE001
            st, msg = "EXC", str(e)
        finally:
            raw.new_session = old
        chk(st == "unknown", f"{n}.verify() 非鉴权失败 → unknown（得到 {st}）")

    print("== 3. 认证类文本识别（unknown → dead 的那一刀）==")
    for t in ("Token has expired, please log in again.",
              "登录已过期，请重新登录",
              "401 Unauthorized"):
        chk(login._looks_like_auth(t), f"识别为凭据失效：{t[:28]!r}")
    for t in ("network unreachable", "Read timed out", "", "HTTP 500: oops"):
        chk(not login._looks_like_auth(t), f"不误判为凭据失效：{t!r}")

    print("== 4. silent_refresh 的护栏 ==")
    os.environ["FP_WEBAI_NO_AUTOLOGIN"] = "1"
    chk(login.silent_refresh("qwen") is False, "开关打开时不刷新")
    del os.environ["FP_WEBAI_NO_AUTOLOGIN"]
    chk(login.silent_refresh("不存在的后端") is False, "未知 provider 不刷新")
    # 并发锁：同一时刻只允许一个刷新在跑
    fd = login._acquire_lock()
    chk(fd is not None, "能拿到刷新锁")
    chk(login._acquire_lock() is None, "锁已持有时第二次获取失败")
    chk(login.silent_refresh("qwen") is False, "已有刷新在跑时不叠加")
    login._release_lock(fd)
    fd2 = login._acquire_lock()
    chk(fd2 is not None, "释放后能重新拿到锁")
    login._release_lock(fd2)

    print("== 5. 自愈代理：转发 + 只拦指定方法 ==")

    class FakeMod:
        capabilities = {"chat", "search"}
        HELLO = "hi"

        def __init__(self):
            self.calls = 0

        def available(self):
            return True, ""

        def ask(self, *a, **k):
            self.calls += 1
            raise RuntimeError("Token has expired, please log in again.")

        def search(self, *a, **k):
            self.calls += 1
            raise ValueError("nothing to do with auth")

    fake = FakeMod()
    webai._cache["qwen"] = webai._HealingProvider("qwen", fake)
    p = webai.get("qwen")

    chk(p.capabilities == {"chat", "search"}, "普通属性原样转发")
    chk(p.HELLO == "hi", "常量也转发")

    heals = []
    real_heal = webai._heal          # 记下真的 —— 最后要还原（stub 会绕过开关）
    webai._heal = lambda n: (heals.append(n), True)[1]
    try:
        p.ask("s", "hi")
        chk(False, "AUTH 错误重试后仍失败 → 应抛出")
    except RuntimeError:
        chk(True, "AUTH 错误刷新后仍失败 → 照原样抛出")
    chk(heals == ["qwen"], "AUTH 错误触发了一次刷新")
    chk(fake.calls == 2, "且只重试一次（共调用 2 次）")

    # AUTH 错误 + 刷新成功 → 返回重试结果
    heals.clear()
    fake2 = FakeMod()

    def ask_ok_once(self, *a, **k):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Token has expired, please log in again.")
        return "second-ok"
    fake2.ask = types.MethodType(ask_ok_once, fake2)
    webai._cache["qwen"] = webai._HealingProvider("qwen", fake2)
    p = webai.get("qwen")
    out = p.ask("s", "hi")
    chk(out == "second-ok", "刷新成功 → 重试拿到结果")
    chk(heals == ["qwen"], "恰好刷新一次")

    # 非 AUTH 错误：不该刷新（用全新替身，只走 search —— 别让 ask 的 AUTH 计数混进来）
    heals.clear()
    fake3 = FakeMod()
    webai._cache["qwen"] = webai._HealingProvider("qwen", fake3)
    try:
        webai.get("qwen").search("q")
        chk(False, "非 AUTH 错误应原样抛出")
    except ValueError:
        chk(True, "非 AUTH 错误原样抛出")
    chk(heals == [], "非 AUTH 错误不触发刷新")
    chk(fake3.calls == 1, "非 AUTH 错误不重试")

    # 开关能关掉自愈 —— 必须用**真的** _heal 才测得到（上面的 stub 会绕过开关）
    webai._heal = real_heal
    os.environ["FP_WEBAI_NO_AUTOLOGIN"] = "1"
    chk(webai._heal("qwen") is False,
        "FP_WEBAI_NO_AUTOLOGIN=1 时 _heal 直接返回 False（不 spawn 登录子进程）")
    os.environ.pop("FP_WEBAI_NO_AUTOLOGIN", None)

    print(f"\n{'✅' if not _fail else '❌'} {_ok} passed, {_fail} failed")
    if _failed:
        for m in _failed:
            print("   -", m)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
