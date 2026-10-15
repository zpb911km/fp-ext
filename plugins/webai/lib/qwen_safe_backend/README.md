# Qwen 安全后端（真实浏览器通道）

真实浏览器驱动的 Qwen 网页版后端。**无任何伪造 / 模拟 API 请求**，全部交互通过有头
chromium 操作真实 DOM 完成。

## 为什么要它（根因档案）

`chat.qwen.ai` 的 `/api/v2/chat/completions` 在 **2026-10** 后上了阿里 WAF 风控：

| 通道 | 结果 |
|---|---|
| 纯 `requests` | `{"success":false,"code":"Bad_Request","details":"Internal error..."}` |
| 连续发几次 | 升级为 `FAIL_SYS_USER_VALIDATE / RGV587_ERROR::SM::哎哟喂,被挤爆啦` + `punish?x5secdata=...` |
| 补 cookie / 重登 | **无效**（凭据本身有效：`chats/new`、`/models` 都 200） |
| `curl_cffi` impersonate=chrome | 过得了 `chats/new`，过不了 `completions` |
| 再补 `bx-ua` / `bx-umidtoken` | 仍被拦（bx-ua 与 UA 绑定，且 `sec-ch-ua` 必须严格一致） |
| **playwright chromium，headless** | ❌ 页面永远停在 `response-loading`（bx 挑战跑不完） |
| **playwright chromium，headed（Xvfb）** | ✅ 首次请求被 punish，**前端 JS 自动接住并重发**，第二次即成功 |

结论：**只有真实浏览器能过**，而且**必须有头**。这与 `deepseek_safe_backend` 的
“拒绝伪造请求”结论完全一致。

## 安全设计

| 风险 | 对策 |
|---|---|
| 模拟 HTTP 被风控反击 | 彻底禁用：只操作真实浏览器 DOM |
| `navigator.webdriver=true` 等自动化指纹 | `--disable-blink-features=AutomationControlled`；有头渲染 |
| headless 拿不到渲染器 → bx 挑战卡死 | **强制 `headless=False` + Xvfb `:99`**（懒启动、自愈、持有者才会回收） |
| 反复请求把账号推到人机验证 | 间隔由插件层 `_NS_INTERVAL['qwen']=30s` 节流；出错即回收浏览器会话重开 |
| 提取截断/串台 | 只取**最后一条** `.qwen-chat-message-assistant`；文本连续 `STABLE_POLLS` 次不变且无 `.response-loading` 才算完 |
| 风险页被误当“空回答” | 监听 `/chat/completions` 响应体，命中 `RGV587/x5secdata/punish` 且从未成功 → 报**说人话**的错误（不甩锅 cookie） |
| 静默丢东西 | 拿不到就留空（thinking / queries 不猜）；未实测的能力不声明（见 `qwen.probe()`） |

## 用法

```bash
python3 backend.py status            # daemon/profile/Xvfb 状态（不开浏览器）
python3 backend.py probe             # 打开会话自检（不发消息，出截图）
python3 backend.py ask "问题"        # 提问 → JSON
python3 backend.py ask "问题" --search --new-chat --files a.png
python3 backend.py stop              # 停止 daemon（顺手回收自己起的 Xvfb）
```

daemon 常驻（unix socket `qwen_backend.sock`），浏览器会话跨提问复用；日志
`daemon.log` / `asks.log` / `state.json`；异常截图落 `shots/`。

> `qwen.py`（provider）通过 CLI 调它：`QWEN_QUIET=1` 保证 stdout 只有 JSON。

## ❗ 与登录脚本互斥

daemon 持有 `~/.local/share/fp/webai/qwen/profile` 时，**不要再跑 `qwen_login.py`**
（chromium 的 profile 锁会打架）。要先登录就先 `backend.py stop`，登录完再让后端自己起来。

## 已验证（2026-10-15，本机实测）

1. `probe`：daemon + Xvfb + 有头 chromium 从零拉起，`logged_in=true`、`input_box=true`
2. `ask --search --new-chat`：37.1s 返回答案 + 引用，`waf_retries=0`
3. 子进程内用插件包路径 `webai.search('qwen', ...)`：`status=answered` + 引用非空
4. 离线契约测试：`python3 tests/test_qwen_backend.py` → 27 passed
5. 负例：headless 模式下同一个问题**必卡死**（对照组）

## 尚未实现（诚实声明）

- 生成类能力（`t2i` / `t2v` / `slides` / `deep_research`）：旧 API 通道时通过
  `chat_type` 选，浏览器通道需要在页面「选择模式」下拉里驱动——**未做**，
  故 `qwen.CAPABILITY_MAP = {}`、`capabilities = {chat, search}`
- 附件/图片（`attach` / `vision`）：页面内 `input[type=file]` 已接线，但**未实测验收**，
  所以 `probe()` 里 `supported=False` —— 验收通过后才提到 `capabilities` 里
- thinking 正文：页面默认折叠，只有展开了才拿得到；`qwen.ask()` 的 `thinking` 可能为空
