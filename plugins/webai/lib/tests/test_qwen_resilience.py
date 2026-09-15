#!/usr/bin/env python3
"""qwen 抗断联离线测试 —— 不联网、不发任何真实请求（socket 都没碰）。

跑法::

    cd <data>/public/plugins/webai/lib && python3 tests/test_qwen_resilience.py

覆盖（2026-09 用户反馈"copilot 里 qwen 老是断联"之后补的）：
  * §1 WAF 挑战页的识别：认得挑战页，且不误伤正常 SSE
  * §2 流完整性：`[DONE]` 到底来没来必须能区分（以前从不检查）
  * §3 错误收口：WAF 页 / 非 JSON / 5xx / 401 → 一律带归类的 WebAIError
  * §4 `new_session()` 不再把 WAF 页冒泡成 JSONDecodeError
  * §5 `ask()` 半截流 → 保留内容 + 标 truncated（**不丢东西**）
  * §6 `ask()` 啥都没收到 → 自动重发一次（且复用同一份请求体）
  * §7 WAF 错误的文案**不许**再提"cookie 失效"（会把排查引向重登，实测无关）
  * §8 `classify()` 把 WAF 判成 transient 而不是 auth
  * §9 自愈代理的 TRANSIENT 退避重试（与 AUTH 刷新是两条独立通道）
"""

import importlib.util
import json
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


# ── 替身 ────────────────────────────────────────────────────────

CHALLENGE = (
    '<!doctype html> <meta charset="UTF-8"> '
    '<meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> '
    '<meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c">'
)

SSE_OK = [
    'data: {"response_id":"r-1","choices":[{"delta":{"phase":"answer","content":"你"}}]}',
    'data: {"response_id":"r-1","choices":[{"delta":{"phase":"answer","content":"好"}}]}',
    'data: [DONE]',
]
SSE_CUT = SSE_OK[:2]        # 少了 [DONE]：连接被掐断的样子


class FakeResp:
    def __init__(self, status=200, text="", lines=None, exc=None):
        self.status_code = status
        self.text = text
        self._lines = list(lines or [])
        self._exc = exc
        self.closed = False

    def json(self):
        return json.loads(self.text)

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln
        if self._exc is not None:
            raise self._exc

    def close(self):
        self.closed = True


class FakeSess:
    """按调用次序发响应；最后一次可重复使用。"""

    def __init__(self, resps):
        self._resps = list(resps)
        self.calls = 0
        self.last_kw = None

    def _next(self):
        self.calls += 1
        return self._resps[min(self.calls - 1, len(self._resps) - 1)]

    def post(self, url, **kw):
        self.last_kw = kw
        return self._next()

    def get(self, url, **kw):
        self.last_kw = kw
        return self._next()


def install(webai, resps):
    """把 qwen 的 `_session()` 换成替身，返回替身（供断言 calls / 请求体）。"""
    q = webai.get("qwen")
    sess = FakeSess(resps)
    saved = webai.raw("qwen")._session
    webai.raw("qwen")._session = lambda: sess
    return sess, saved


# ── §1 WAF 识别 ─────────────────────────────────────────────────

def test_waf_detect(webai):
    print("== 1. WAF 挑战页识别 ==")
    q = webai.raw("qwen")
    chk(q._is_waf(CHALLENGE) is True, "认得 aliyun_waf 挑战页")
    chk(q._is_waf("<!DOCTYPE html>\n<html>") is True, "认得裸 HTML（大小写不敏感）")
    chk(q._is_waf("\n  <html><body>502</body></html>") is True, "前导空白也认得出")
    chk(q._is_waf('data: {"choices":[]}\n') is False, "正常 SSE 不误判")
    chk(q._is_waf("") is False, "空串不误判")
    chk(q._is_waf('{"success":true}') is False, "正常 JSON 不误判")


# ── §2 流完整性 ─────────────────────────────────────────────────

def test_stream_completeness(webai):
    print("== 2. 流完整性（[DONE] 到底来没来）==")
    q = webai.raw("qwen")

    raw, done, exc = q._collect_stream(FakeResp(lines=SSE_OK))
    chk(done is True, "见到 [DONE] → saw_done=True")
    chk('"content":"你"' in raw and '"content":"好"' in raw and exc is None, "内容完整收到")

    # ⚠️ 关键一条（2026-09 联网实测才发现的）：qwen **根本不发 [DONE]**。
    #    "没见到 [DONE]" 绝不能当截断 —— 那会把每次正常回答都误报成 truncated。
    #    这条断言就是用那次真实流（无 DONE、正常收尾）钉住的。
    raw, done, exc = q._collect_stream(FakeResp(lines=SSE_CUT))
    chk(done is False, "没有 [DONE] → saw_done=False")
    chk(exc is None, "但流是**正常收尾**的（分块编码结束，无异常）→ 调用方不应判截断")
    chk('"content":"你"' in raw, "内容照样保留（未知的东西不能丢）")

    from requests.exceptions import ChunkedEncodingError
    raw, done, exc = q._collect_stream(
        FakeResp(lines=SSE_CUT, exc=ChunkedEncodingError("Connection broken: IncompleteRead")))
    chk(done is False, "断流 → saw_done=False")
    chk(isinstance(exc, ChunkedEncodingError), "断流异常被如实带出（不吞）—— 这才是真截断信号")
    chk('"content":"你"' in raw, "断流前收到的内容不丢")


# ── §3 错误收口 ─────────────────────────────────────────────────

def test_error_funnel(webai):
    print("== 3. 错误收口：不再冒泡 JSONDecodeError / HTTPError ==")
    q = webai.raw("qwen")
    EK = webai.ErrorKind

    try:
        q._json(FakeResp(text=CHALLENGE), "建会话")
        chk(False, "WAF 页应当抛错")
    except Exception as e:
        chk(isinstance(e, webai.WebAIError), "WAF 页 → WebAIError（不是 JSONDecodeError）")
        chk(e.kind == EK.TRANSIENT, "WAF 归类 transient（可重试、不刷凭据）")

    try:
        q._json(FakeResp(text="<html>oops</html>"), "建会话")
        chk(False, "非 JSON 应当抛错")
    except Exception as e:
        chk(not isinstance(e, ValueError), "不再抛 ValueError/JSONDecodeError")
        chk(isinstance(e, webai.WebAIError) and e.kind == EK.TRANSIENT,
            "非 JSON → transient（多半是网关吐了 HTML）")

    try:
        q._json(FakeResp(status=401, text='{"code":"Unauthorized"}'), "建会话")
        chk(False, "401 应当抛错")
    except Exception as e:
        chk(e.kind == EK.AUTH, "401 → auth（老行为保住：仍能触发凭据自愈）")

    try:
        q._json(FakeResp(status=503, text="upstream busy"), "建会话")
        chk(False, "503 应当抛错")
    except Exception as e:
        chk(e.kind == EK.TRANSIENT, "503 → transient")


# ── §4 new_session 不再吐乱码 ───────────────────────────────────

def test_new_session_waf(webai):
    print("== 4. new_session() 遇到 WAF 页 ==")
    sess, saved = install(webai, [FakeResp(text=CHALLENGE)])
    try:
        webai.get("qwen").new_session()
        chk(False, "WAF 页应当抛错")
    except Exception as e:
        chk(isinstance(e, webai.WebAIError),
            f"是 WebAIError 而不是 JSONDecodeError（实测修前就是 JSONDecodeError）")
        chk("aliyun" in str(e).lower() or "WAF" in str(e), "错误文案说得出是 WAF")
        chk("cookie 可能失效" not in str(e), "不许再说「cookie 可能失效」（实测与凭据无关）")
    finally:
        webai.raw("qwen")._session = saved


# ── §5 / §6 / §7 ask() 的三条路径 ───────────────────────────────

def test_ask_paths(webai):
    print("== 5/6/7. ask()：截断 / 全空重发 / WAF ==")

    # 5) 正常收尾但没有 [DONE]（= qwen 的真实形态）→ **不许**标 truncated
    sess, saved = install(webai, [FakeResp(lines=SSE_CUT)])
    try:
        out = webai.get("qwen").ask("sid", "hi")
        chk(out.get("text") == "你好", "正常收尾的回答照常返回")
        chk(out.get("truncated") is None,
            "没见到 [DONE] 也**不算**截断（qwen 压根不发，联网实测）")
        chk(sess.calls == 1, "有内容就不重发（重发会污染会话）")
    finally:
        webai.raw("qwen")._session = saved

    # 5b) 真·传输中断且已收到部分内容 → 保留内容 + 标 truncated + **不**重发
    from requests.exceptions import ChunkedEncodingError
    sess, saved = install(webai, [FakeResp(lines=SSE_CUT,
                                           exc=ChunkedEncodingError("IncompleteRead(5 bytes)"))])
    try:
        out = webai.get("qwen").ask("sid", "hi")
        chk(out.get("text") == "你好", "断流前的半截回答照样返回（不丢）")
        chk(out.get("truncated") is True, "真截断 → 打标记")
        chk("ChunkedEncodingError" in (out.get("truncated_reason") or ""), "留下原因")
        chk(sess.calls == 1, "已有内容就不重发（重发会让对方收到两条相同的提问）")
    finally:
        webai.raw("qwen")._session = saved

    # 6) 一个字都没收到 → 重发一次，且**复用同一份请求体**
    empty = FakeResp(lines=[])
    ok = FakeResp(lines=SSE_OK)
    sess, saved = install(webai, [empty, ok])
    try:
        out = webai.get("qwen").ask("sid", "hi")
        chk(sess.calls == 2, "全空 → 自动重发一次")
        chk(out.get("text") == "你好", "重发拿到真结果")
        chk(out.get("truncated") is None, "重发成功就不标 truncated")
    finally:
        webai.raw("qwen")._session = saved

    # 6b) 两次都空 → 抛 transient（交给上层退避，而不是拿空回答糊弄）
    sess, saved = install(webai, [FakeResp(lines=[]), FakeResp(lines=[])])
    try:
        webai.get("qwen").ask("sid", "hi")
        chk(False, "两次都空应当抛错")
    except Exception as e:
        chk(isinstance(e, webai.WebAIError) and e.kind == webai.ErrorKind.TRANSIENT,
            "两次都空 → transient（可被上层退避重试）")
    finally:
        webai.raw("qwen")._session = saved

    # 7) WAF 挑战页当成流返回 → 明确报 WAF
    sess, saved = install(webai, [FakeResp(lines=[CHALLENGE])])
    try:
        webai.get("qwen").ask("sid", "hi")
        chk(False, "WAF 流应当抛错")
    except Exception as e:
        chk(isinstance(e, webai.WebAIError) and e.kind == webai.ErrorKind.TRANSIENT,
            "WAF → transient")
        msg = str(e)
        chk("cookie 可能失效" not in msg, "不许再甩锅给 cookie")
        chk("与登录态无关" in msg, "文案点明『与登录态无关』")
    finally:
        webai.raw("qwen")._session = saved

    # 7b) **不重复重发**：ask() 内层已经重发过 → 标记 no_retry，
    #     否则外层 _wrap_heal 再退避 2 次 = 一个用户请求打 4~6 发。
    sess, saved = install(webai, [FakeResp(lines=[]), FakeResp(lines=[])])
    try:
        webai.get("qwen").ask("sid", "hi")
        chk(False, "应当抛错")
    except Exception as e:
        chk(getattr(e, "no_retry", False) is True,
            "内层重发用尽的异常带 no_retry（外层不再叠加）")
    finally:
        webai.raw("qwen")._session = saved
    chk(sess.calls == 2, f"一共只打 2 发（内层 2 次尝试），实际 {sess.calls}")

    sess, saved = install(webai, [FakeResp(lines=[CHALLENGE])])
    try:
        webai.get("qwen").ask("sid", "hi")
    except Exception as e:
        chk(getattr(e, "no_retry", False) is True,
            "WAF 带 no_retry（重发治不了，还会抬高风险分）")
    finally:
        webai.raw("qwen")._session = saved
    chk(sess.calls == 1, f"WAF 只打 1 发，实际 {sess.calls}")


# ── §8 classify 方言 ────────────────────────────────────────────

def test_classify(webai):
    print("== 8. classify：WAF 是 transient，不是 auth ==")
    EK = webai.ErrorKind
    chk(webai.classify("qwen", None, text=CHALLENGE) == EK.TRANSIENT,
        "挑战页文案 → transient")
    chk(webai.classify("qwen", None, text="acw_sc__v2 challenge") == EK.TRANSIENT,
        "acw_sc 挑战 → transient")
    # 老行为不能被这次改动碰坏
    chk(webai.classify("qwen", None, text="Token has expired, please log in again.") == EK.AUTH,
        "真正的过期文案仍然是 auth（没被 WAF 规则抢走）")
    chk(webai.classify("qwen", None, text="该模型不可用") == EK.UNSUPPORTED,
        "本家方言之『该模型不可用』仍然是 unsupported")


# ── §9 自愈代理：TRANSIENT 退避重试 ─────────────────────────────

def test_transient_retry(webai):
    print("== 9. 自愈代理：TRANSIENT 退避重试（与 AUTH 刷新分家）==")

    class FakeMod:
        capabilities = {"chat", "search"}

        def __init__(self, err, fail_times=99):
            self.calls = 0
            self.err = err
            self.fail_times = fail_times

        def available(self):
            return True, ""

        def ask(self, *a, **k):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise self.err
            return "ok"

        def search(self, *a, **k):
            return self.ask()

    heals = []
    real_heal = webai._heal
    real_backoff = webai._retry_backoff
    webai._heal = lambda n: (heals.append(n), True)[1]
    webai._retry_backoff = lambda: (0.001, 0.001)      # 别真等 3.3 秒
    try:
        import requests

        # ① 抖动 → 重试到成功
        fake = FakeMod(requests.exceptions.ConnectionError("Connection reset by peer"),
                       fail_times=2)
        webai._cache["qwen"] = webai._HealingProvider("qwen", fake)
        out = webai.get("qwen").ask("s", "hi")
        chk(out == "ok", "抖动重试后拿到结果（以前直接抛给用户 = 『断联』）")
        chk(fake.calls == 3, "重试两次（首次 + 2 次退避）")
        chk(heals == [], "抖动用的是重试通道，**不去刷凭据**")

        # ② 一直抖 → 退避次数用完就抛（不打转）
        fake2 = FakeMod(requests.exceptions.ReadTimeout("Read timed out"), fail_times=99)
        webai._cache["qwen"] = webai._HealingProvider("qwen", fake2)
        try:
            webai.get("qwen").ask("s", "hi")
            chk(False, "抖动不收敛应当抛错")
        except requests.exceptions.ReadTimeout:
            chk(True, "退避用尽 → 原样抛出")
        chk(fake2.calls == 3, "总共只试 3 次（1 + 2），不无限打转")

        # ③ 非 TRANSIENT（UNKNOWN）→ 不重试、不刷新（老行为）
        fake3 = FakeMod(ValueError("nothing to do with auth"))
        webai._cache["qwen"] = webai._HealingProvider("qwen", fake3)
        try:
            webai.get("qwen").search("q")
            chk(False, "UNKNOWN 应当抛")
        except ValueError:
            chk(True, "UNKNOWN 原样抛出")
        chk(fake3.calls == 1, "UNKNOWN 不重试")
        chk(heals == [], "UNKNOWN 不刷新")

        # ④ AUTH 仍然只刷一次（且**不**走退避通道）
        fake4 = FakeMod(RuntimeError("Token has expired, please log in again."),
                        fail_times=99)
        webai._cache["qwen"] = webai._HealingProvider("qwen", fake4)
        try:
            webai.get("qwen").ask("s", "hi")
            chk(False, "AUTH 刷新后仍失败 → 抛")
        except RuntimeError:
            chk(True, "AUTH 刷新后仍失败 → 原样抛出")
        chk(heals == ["qwen"], "AUTH 触发刷新")
        chk(fake4.calls == 2, "AUTH 只重试一次（刷新那次），不叠加退避")

        # ⑤ 开关：FP_WEBAI_NO_RETRY=1 关掉退避重试
        os.environ["FP_WEBAI_NO_RETRY"] = "1"
        webai._retry_backoff = real_backoff
        chk(webai._retry_backoff() == (), "FP_WEBAI_NO_RETRY=1 → 退避表为空")
        fake5 = FakeMod(requests.exceptions.ConnectionError("Connection reset"), 99)
        webai._cache["qwen"] = webai._HealingProvider("qwen", fake5)
        try:
            webai.get("qwen").ask("s", "hi")
            chk(False, "关掉重试后应当立刻抛")
        except requests.exceptions.ConnectionError:
            chk(True, "关掉开关 → 抖动直接抛（不多花时间）")
        chk(fake5.calls == 1, "关掉开关 → 只调 1 次")
    finally:
        os.environ.pop("FP_WEBAI_NO_RETRY", None)
        webai._heal = real_heal
        webai._retry_backoff = real_backoff


# ── §10 copilot 层：拿不到续接指针必须出声 ──────────────────────

def test_copilot_warnings():
    """copilot 的"静默降级"是断联体感的另一半：不想让人猜，就得说话。"""
    print("== 10. copilot：拿不到续接指针 / 回答被截断 → 出警告 ==")
    import tempfile
    import types

    # 注意目录层级：本文件在 <webai>/lib/tests/，PKG 是 <webai>/lib，
    # 而插件根是 <webai>、plugins 根是它的上一级。
    plugin_root = os.path.dirname(PKG)
    plugins_root = os.path.dirname(plugin_root)
    plug = types.ModuleType("plugins")
    plug.__path__ = [plugins_root]
    sys.modules["plugins"] = plug
    spec = importlib.util.spec_from_file_location(
        "plugins.webai", os.path.join(plugin_root, "__init__.py"),
        submodule_search_locations=[plugin_root])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plugins.webai"] = mod
    spec.loader.exec_module(mod)
    cp = importlib.import_module("plugins.webai.tools.copilot")

    cp._STORE_FILE = os.path.join(tempfile.mkdtemp(), "sessions.json")   # 别碰真 store

    class P:
        default_model = "m1"

        def available(self):
            return True, ""

        def new_session(self, model=""):
            return "chat-1"

        def ask(self, *a, **k):
            return {"text": "hi", "message_id": None}        # ← 续接指针丢了

    cp._get_provider = lambda n: P()
    r = cp.copilot_say("hello", "s1", provider="qwen")
    chk(r.get("success") is True, "回答照常返回（不因为缺指针就吞掉）")
    chk(bool(r.get("warnings")), "拿不到 message_id → 出警告，而不是静默沿用旧指针")
    chk(any("续接指针" in w for w in r.get("warnings") or []),
        f"警告要说清问题：{r.get('warnings')}")
    chk(cp._get_session_rec("s1").get("last_response_id") is None, "不伪造指针")

    class P2(P):
        def ask(self, *a, **k):
            return {"text": "hi", "message_id": "m", "truncated": True,
                    "truncated_reason": "ChunkedEncodingError: IncompleteRead"}

    cp._get_provider = lambda n: P2()
    r2 = cp.copilot_say("hello", "s2", provider="qwen")
    chk(any("截断" in w for w in r2.get("warnings") or []), "truncated 也出声")
    chk(cp._get_session_rec("s2").get("last_response_id") == "m", "能拿到指针就正常落库")

    class P3(P):
        def ask(self, *a, **k):
            return {"text": "hi", "message_id": "m"}      # 指针有、流完整

    cp._get_provider = lambda n: P3()
    r3 = cp.copilot_say("hello", "s3", provider="qwen")
    chk(not r3.get("warnings"), f"一切正常时**不出**警告（不狼来了）：{r3.get('warnings')}")


def main():
    webai = load()
    test_waf_detect(webai)
    test_stream_completeness(webai)
    test_error_funnel(webai)
    test_new_session_waf(webai)
    test_ask_paths(webai)
    test_classify(webai)
    test_transient_retry(webai)
    test_copilot_warnings()

    print()
    if _failed:
        print(f"❌ {_fail} failed / {_ok + _fail} total")
        for m in _failed:
            print("   -", m)
        return 1
    print(f"✅ {_ok} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
