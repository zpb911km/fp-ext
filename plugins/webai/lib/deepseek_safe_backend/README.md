# DeepSeek 安全后端（随插件 lib 内嵌版）

真实浏览器驱动的 DeepSeek 网页版后端。**无任何模拟 API / PoW 请求**，全部交互通过可见的真 Chrome 完成。

> **位置**：本目录已迁入 webai 插件 `lib/deepseek_safe_backend/`（2026-09-25，原 `/media/zpb/data/codes/AI/agent/deepseek-safe-backend/` 删除）。
> `deepseek.py` 以相对模块路径引用 `BACKEND`（`DS_BACKEND` 环境变量可覆盖）；日志/socket/state 均基于 `HERE` 相对，迁移无需改代码。

## 安全设计（长期可用）

| 风险 | 对策 |
|---|---|
| 模拟 HTTP 请求被风控反击（旧后端封号根因） | 彻底禁用：只操作真实浏览器 DOM |
| `navigator.webdriver=true` 等自动化指纹 | 真 Google Chrome 154（user-local）+ `--disable-blink-features=AutomationControlled` → webdriver=false，UA/uaData 品牌一致（复测通过） |
| 连续请求压力被集中检测 | 节奏保护：两问间隔 ≥60s，每日 ≤20 问，打字机输入带抖动 |
| 切 tab 触发心跳超时/渲染挂起 | 单 tab、headed、不隐藏不切走 |
| 深度思考漏开 | 发送前强制校验 `aria-pressed`，未开则点击复验，失败即报错 |
| 提取截断/串台 | 只取**最后一条** `.ds-assistant-message-main-content`（不是"全页最长 markdown"），新鲜度靠 assistant 节点计数增长 |
| 流式生成被当成结束（截断） | 完成判定用**肯定式信号**：SSE `POST /api/v0/chat/completion` 的 `requestfinished` = 服务端写完；未见流事件才退回稳定窗口兜底（5 轮 ≈12.5s，旧值 4 轮） |
| 读数取错范围 → 假超时 | 旧实现挑"全页最长 markdown"：会话里存在更长的历史回答时读数永不变化 → 死等到超时（2026-11-26 截图实证）。现按 DOM 顺序取末条 |
| `--new-chat` 点了没生效 | 点击后校验"assistant 节点数 == 0 + URL 变更"，不成立则重试 3 次，仍失败即报错（绝不静默留在旧会话） |
| 配额/系统提示伪装成"页面没反应" | 读数带 `notice` 字段：命中「已达上限/额度/繁忙/登录失效」等短文案且无正文 → **立刻抛分类错误**（quota/auth/transient），不再空转到超时 |
| 误问敏感问题 | 全量审计 `asks.log`（长度/成败/耗时），人工审题 |

## 用法

```bash
export DISPLAY=:0   # daemon 需要
python3 backend.py status            # 状态（不发消息）
python3 backend.py probe             # 会话+思考开关验证（不发消息）
python3 backend.py ask "问题"        # 提问，返回 JSON {thinking, thinking_blocks, answer, elapsed}
python3 backend.py stop              # 停止 daemon
```

daemon 常驻（unix socket `ds_backend.sock`），浏览器会话跨提问复用；日志 `daemon.log` / `asks.log` / `state.json`（配额状态）。

## 已完成的验证（2026-09-24）

1. 指纹复测：webdriver=false、Chrome/154 品牌一致、无泄漏标记、登录态完好
2. 深度思考：probe + vision 双重确认 `aria-pressed=true` 且 UI 点亮
3. 三次良性实测：提取正确（思考 9340 字与最终回答分离）、节奏保护实际拦截过一次
4. 静态复核：本目录无任何请求伪造代码

## 禁令（对所有维护者）

- **禁止**参考/复用 webai 插件中的旧 deepseek 模拟后端（封号来源）
- **禁止** ask_llm / copilot / vision 留空默认后端（默认是 deepseek，已封）
- **禁止**提高频率或绕过节奏保护；问题保持日常多样性
- 替换插件后端前，需用户确认本目录验证结论
