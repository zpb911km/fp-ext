#!/usr/bin/env python3
"""完成判定 / 系统提示识别 —— 离线回归测试（不联网、不开浏览器）。

背景（2026-11-26/27 两次故障，同一个病根的两副面孔）：

* **假完成（反思58，qwen）**：旧判据 = 「连续 3 次文本不变 ≈ 6s」→ 长代码流式
  生成里 ≥6s 的间隙被当成结束，模型自报 402 行、返回只有 31 行。
* **假超时（deepseek）**：读数取「全页最长 [class*=markdown]」→ 会话里存在一条更长
  的历史回答时，读数永不变化 → 死等 300s 报「未观测到新文本」，页面其实早答完了。
* **配额伪装成"页面没反应"（qwen）**：额度耗尽时页面只渲染系统提示、没有正文，
  旧逻辑把它当"还没出文本"，空转到 300s —— 报错文案把配额问题伪装成后端坏了。

这些测试锁住修复后的**结构**（源码级守卫）与**归类行为**（词表级验证）。
"""
import os
import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent                                   # …/webai/lib
BACKENDS = {
    "qwen": PKG / "qwen_safe_backend" / "backend.py",
    "deepseek": PKG / "deepseek_safe_backend" / "backend.py",
}
# 2026-11-27 实测的页面原文（qwen 额度耗尽）
QUOTA_PAGE_TEXT = "Qwen4-Max 今日已达上限，请升级会员或在上方切换其他模型继续使用。"


# ── 一、系统提示必须被归成 quota（而不是 unknown）────────────────────────

def test_core_classifies_quota_page_text(webai):
    """页面原文里没有任何旧词表词（无 rate limit / 额度 / 429），必须仍然判 quota。

    判错的代价：调用方不知道该换一家，只会拿到"未知错误"。
    """
    kind = webai.core.classify_text(QUOTA_PAGE_TEXT)
    assert kind.name == "QUOTA", f"今日已达上限 应归 QUOTA，实际 {kind}"


@pytest.mark.parametrize("provider", ["qwen", "deepseek"])
def test_provider_classify_quota_page_text(webai, provider):
    """两家自己的 classify() 也要认这条文案（provider 先跑自家方言，再落通用词表）。"""
    mod = webai.raw(provider)
    assert mod.classify(text=QUOTA_PAGE_TEXT) == "quota"


# ── 二、完成判定必须有"肯定式信号"，不能只靠"文本不变" ──────────────────

@pytest.mark.parametrize("provider", sorted(BACKENDS))
def test_backend_has_stream_finish_signal(provider):
    """两个安全后端都必须挂 requestfinished 观察器 + 有兜底常数。

    只靠"连续 N 次文本不变"= 纯否定式证据：流式间隙 → 假完成（反思58）；
    读数范围错 → 永不成立（deepseek 假超时）。肯定式信号在网络层：SSE 流结束。
    """
    src = BACKENDS[provider].read_text(encoding="utf-8")
    assert "STREAM_URL_KEY" in src, f"{provider}: 缺少流端点常量"
    assert 'on("requestfinished"' in src, \
        f"{provider}: 未监听 requestfinished（肯定式完成信号）"
    assert "FALLBACK_STABLE_POLLS" in src, f"{provider}: 缺少兜底稳定窗口"
    # 兜底窗口必须比旧的 3~4 轮更保守（反思58 的 6s 太短）
    ns = {}
    for line in src.splitlines():
        if line.startswith(("STABLE_POLLS", "FALLBACK_STABLE_POLLS", "SETTLE_POLLS")):
            head, _, tail = line.partition("=")
            try:
                ns[head.strip()] = int(str(tail).split("#")[0].strip())
            except ValueError:
                pass
    assert ns.get("FALLBACK_STABLE_POLLS", 0) > ns.get("STABLE_POLLS", 0), \
        f"{provider}: 兜底窗口没放大（{ns}）—— 反思58 的 6s 间隙还会被误判"


def test_deepseek_reads_last_answer_not_longest_markdown():
    """deepseek 读数必须是"末条 assistant 回答"，不能回退到"全页最长的 markdown"。

    旧实现 `_best_markdown` 挑全页最长：会话里有一个更长的历史回答时，
    读数永远不变 → 300s 假超时（2026-11-26 截图实证）。
    """
    src = (BACKENDS["deepseek"]).read_text(encoding="utf-8")
    assert ".ds-assistant-message-main-content" in src, "末条回答的选择器丢了"
    assert "els[n - 1]" in src or "els[els.length - 1]" in src, \
        "读数没有取最后一条（回退成取最长会让旧长回答遮蔽新回答）"


@pytest.mark.parametrize("provider", sorted(BACKENDS))
def test_backend_surfaces_page_notice_immediately(provider):
    """页面系统提示（额度/繁忙/登录）必须在轮询里立刻抛错，而不是空转到超时。"""
    src = BACKENDS[provider].read_text(encoding="utf-8")
    assert "_notice_error" in src, f"{provider}: 缺系统提示→异常 的转换器"
    assert '"notice"' in src or "notice:" in src, f"{provider}: 读数没带 notice 字段"


def test_deepseek_new_chat_is_verified():
    """--new-chat 必须校验"真的开了新会话"（旧实现只点不验，静默留在旧会话）。"""
    src = BACKENDS["deepseek"].read_text(encoding="utf-8")
    i = src.find("def new_chat")
    assert i > 0, "new_chat 方法不存在"
    body = src[i:i + 2500]
    assert "raise RuntimeError" in body, "new_chat 点不中时没有报错（会静默留在旧会话）"
    assert "length === 0" in body or "== 0" in body, "new_chat 没有校验会话已空"
