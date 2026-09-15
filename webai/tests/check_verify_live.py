#!/usr/bin/env python3
"""verify() 的**负向**测试 —— 凭据换成垃圾，断言它报 dead。

为什么必须做这一步
----------------------------------------------------------------
"凭据有效时 verify() 返回 ok" 什么也证明不了：一个**游客也能访问**的接口
同样返回 200。必须反过来测 —— 把凭据换成垃圾，看它能不能报 dead。

2026-09 实测踩到的坑：qwen 的 verify() 原本打的是 `/api/v2/models`，
该接口游客可访问 → 凭据全坏也回 200 → verify() 永远 ok → `--check` 形同虚设、
AUTH 自愈永不触发。换成 `new_session()` 探针后才正常。

**新增/修改 provider 的 verify() 后，必须跑一次本脚本。**

用法（会短暂替换凭据文件，全程 try/finally 保证还原）::

    cd <data>/public/webai && python3 tests/check_verify_live.py --yes

⚠️ 需要联网；会带"垃圾凭据"发一次请求（等价于 cookie 自然过期时的情形）。
⚠️ 全程只改凭据文件、不改别的；异常/中断也会还原。
"""
import argparse
import importlib
import json
import os
import pathlib
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
if PKG not in sys.path:
    sys.path.insert(0, PKG)

try:
    from fp_core.platform_utils import get_data_dir
    DATA = pathlib.Path(get_data_dir())
except Exception:  # noqa: BLE001
    DATA = pathlib.Path.home() / ".local" / "share" / "fp"

# provider → (凭据文件, 破坏方式)
CASES = {
    "qwen":     (pathlib.Path(os.path.expanduser("~/.qwen_cookie")), "text"),
    "deepseek": (pathlib.Path(os.path.expanduser("~/.deepseek_token")), "text"),
    "stepfun":  (DATA / "stepfun" / "cookies.json", "json_values"),
    "glm":      (DATA / "glm" / "cookies.json", "json_values"),
}


def break_it(cf: pathlib.Path, how: str) -> None:
    if how == "text":
        cf.write_text("x" * 200)                      # 够长，但无效
        return
    d = json.loads(cf.read_text())
    if "cookies" in d:                                # glm：{"cookies": {...}, "device_id": ...}
        d["cookies"] = {k: "garbage_garbage_garbage" for k in (d["cookies"] or {})}
    else:                                             # stepfun：{"Oasis-Token": "...", ...}
        d = {k: "garbage_garbage_garbage" for k in d}
    cf.write_text(json.dumps(d))


def main() -> int:
    ap = argparse.ArgumentParser(description="verify() 负向测试（会把凭据临时换成垃圾）")
    ap.add_argument("--yes", action="store_true", help="确认执行（会临时改动凭据文件）")
    ap.add_argument("providers", nargs="*", help="只测这几家（默认全部）")
    a = ap.parse_args()
    if not a.yes:
        print(__doc__)
        print("加上 --yes 才会真的执行。")
        return 2

    login = importlib.import_module("login")
    names = [n.lower() for n in a.providers] or list(CASES)
    fails = []
    for name in names:
        cf, how = CASES[name]
        if not cf.exists():
            print(f"{name:9} 跳过（无凭据文件）")
            continue
        bak = cf.with_suffix(cf.suffix + ".bak_verifytest")
        shutil.copy2(cf, bak)
        try:
            before = login.check(name)[0]
            break_it(cf, how)
            bad = login.check(name)
        finally:
            shutil.move(str(bak), str(cf))
            try:
                os.chmod(cf, 0o600)
            except Exception:  # noqa: BLE001
                pass
        after = login.check(name)[0]

        good = (bad[0] == "dead")
        if not good:
            fails.append(name)
        print(f"{name:9} 原始={before:<8} 垃圾凭据={bad[0]:<8} 还原={after:<8} "
              f"{'✅ 能识别' if good else '❌ 假阳性（verify() 探针不需要鉴权）'}")
        if not good:
            print(f"          detail: {str(bad[1])[:120]}")
        if before != "ok" or after != "ok":
            print(f"          ⚠️ 原始/还原状态非 ok —— 凭据本身可能有问题，结果不足采信")

    print()
    if fails:
        print(f"❌ {len(fails)} 家未通过：{', '.join(fails)} —— 它们的 verify() 需要用"
              f"**真的需要鉴权**的调用做探针（参考 qwen 改用 new_session()）。")
        return 1
    print("✅ 全部通过：四家的 verify() 都能识破无效凭据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
