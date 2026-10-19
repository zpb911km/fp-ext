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
| 页面弹人机验证 | **DOM 级检测（文案 + 选择器）命中即抛错停手**：不再点、不再发、不再重试，落 `shots/risk_*.png`；检查点覆盖点击前/发送前/每轮轮询/等产物 |
| 浮层挡住点击（如「创作额度」弹层） | `_click_safe()`：Escape → 退化为 `force=True` 重点，避免 47 次重试空转 |

## 用法

```bash
python3 backend.py status            # daemon/profile/Xvfb 状态（不开浏览器）
python3 backend.py probe             # 打开会话自检（不发消息，出截图）
python3 backend.py ask "问题"        # 提问 → JSON
python3 backend.py ask "问题" --search --new-chat --files a.png
python3 backend.py ask "问题" --mode t2i --timeout 420     # 生成图像
python3 backend.py ask "问题" --mode slides                # 幻灯片（首轮大纲→回「确认」）
python3 backend.py ask "问题" --model Qwen3.6-Plus --think  # 选模型 + 思考档
python3 backend.py models            # 多级模型清单（7 个，tier 标注一级/二级）
python3 backend.py pin Qwen3.8-Max   # 设为默认（改账号设置，慎用）
python3 backend.py mode t2v          # 切模式（不发消息）；chat=回普通对话
python3 backend.py think thinking    # 切思考档（不发消息）；生成类模式会明确报错
python3 backend.py configs           # 当前模式的子配置 + 全部可选值（含 semantic/video）
python3 backend.py setcfg 1:1 --key ratio          # 按语义键改子配置（ratio/model/depth）
python3 backend.py vparams           # 创建视频参数（清晰度/编辑模式/比例/时长）+ 全部选项
python3 backend.py vset --ratio 1:1 --duration 10s # 改视频参数（1080P 是 VIP，默认拒绝）
python3 backend.py ask "海边日落" --mode t2v --vparams '{"ratio":"1:1","duration":"10s"}'
python3 backend.py stop              # 停止 daemon（顺手回收自己起的 Xvfb）
```

daemon 常驻（unix socket `qwen_backend.sock`），浏览器会话跨提问复用；日志
`daemon.log` / `asks.log` / `state.json`；异常截图落 `shots/`。

> `qwen.py`（provider）通过 CLI 调它：`QWEN_QUIET=1` 保证 stdout 只有 JSON。

## ❗ 与登录脚本互斥

daemon 持有 `~/.local/share/fp/webai/qwen/profile` 时，**不要再跑 `qwen_login.py`**
（chromium 的 profile 锁会打架）。要先登录就先 `backend.py stop`，登录完再让后端自己起来。

## 页面逆向档案（2026-10-19 活体实测，报告在 `/tmp/qshots/report{5..18}.json`）

### 1. 左上角模型下拉：**多级**
```
.wms-trigger（aria-expanded 判开合）
└── .wms-popup             一级 3 个：Qwen3.7-Plus / Qwen3.8-Max / Qwen3.8-Omni-Flash
    └── .wms-more-entry  「展开更多模型」（hover，不是 click）
        └── .wms-popup--secondary   二级 4 个：Qwen3.7-Max / Qwen3.6-Plus /
                                    Qwen3.5-Plus / Qwen3.5-Omni-Plus
```
- 条目内 `.wms-list__pin-action`「设为默认」= **账号级**默认模型（`pin_model()` 显式才动）。
- 「模型比较」开关在 `.wms-intro__battle`，实测 `disabled` + `aria-checked=false` → **不可用**。
- 二级条目**没有** hover 就不会进 DOM，所以 `list_models()` 必须 hover `.wms-more-entry`。

### 2. 输入框加号：模式菜单（一层 + 「更多」二层）
| 条目 | 语义 | 实测 |
|---|---|---|
| 上传附件 | filechooser 入口（非模式） | ✅ 芯片计数校验 |
| 生成图像 / 创建视频 / 幻灯片 / 深入研究 / 网页开发 | 模式 | ✅ 可选中，产出见下 |
| 网页搜索 | **模式**（不是 toggle！） | ✅ 出 citation 主机名 |
| 更多 → 预览模式 / 学习 / 旅行规划师 | 模式（二级） | ✅ 可选中 |
| 工具 | **空入口** | ❌ 无 `has-submenu` 类、hover 不出子菜单、点击不产生芯片、不改 localStorage、无新弹层（report14/15）→ 不接线 |

输入条右侧还有一枚图标按钮，弹层是「语音通话 / 视频通话」（WebRTC，要麦克风/摄像头）
——不在本后端范围，`probe()['call']` 如实记 `supported=False`。

**模式状态只认芯片** `.mode-select-current-mode`（文案=模式名）。菜单条目有没有
check 图标都说明不了问题。换模式 = 直接点另一个；**退出** = hover 芯片 → 点
`.mode-select-current-mode-close`（X 不 hover 不渲染），或新建对话。

### 3. 模式子配置

**a) 输入区下拉**（`.qwen-chat-v2-dropdown-menu-select`，按钮文案就是当前值）
| 模式 | 子配置 |
|---|---|
| 生成图像 | 模型 `Qwen-Image 3.0 / 2.0` + 比例 `1:1 2:3 3:2 3:4 4:3 16:9 9:16` |
| 创建视频 | 模型 `HappyHorse 1.1 / 1.0`（单条 30 积分） |
| 深入研究 | 深度 `正常 / 高级` |

`read_config()` / `set_config(value, key='ratio'|'model'|'depth')` 已接
（`_cfg_key()` 按取值形态推语义键，页面本身不给 key）。
⚠️ 实测坑：**Qwen-Image 3.0 不提供比例**，切到 3.0 后比例下拉消失 → 要比例就先选 2.0。

**b) 创建视频参数弹层**（`report15-18`，它**不是下拉**，所以上面那套读不到）
```
.video-params-summary           输入条上的摘要芯片（文案 "720P · 5s"）
└── .video-params-popover
    └── .video-params-group × 4   .video-params-group-title = 清晰度|视频编辑模式|比例|时长
        └── button[role=radio][aria-checked=true]
            └── .video-params-segment-label（1080P 上挂 .video-params-vip-badge）
```
| 组 | 取值 | 备注 |
|---|---|---|
| 清晰度 | 720P / 1080P | 1080P 带 **VIP** 徽章 → 点它只会弹升级，`set_video_params` 默认**拒绝**（`allow_vip=True` 才试） |
| 视频编辑模式 | 视频生成 / 多参考图 | |
| 比例 | 16:9 / 4:3 / 1:1 / 3:4 / 9:16 / 原比例 | |
| 时长 | 5s / 10s / 15s | 积分随档位变（30 积分 = 720P·5s） |

`video_params()` 读（含每组全部选项 + VIP 标记）、`set_video_params(**kw)` 改、
`ask(..., vparams={...})` 在**点发送前**生效（页面按当前参数计费）。

### 4. 思考档（自动/思考/快速）
- **页面级**设置（localStorage），**新建对话不重置**（实测：切「思考」→ 新对话仍是「思考」）。
- ⚠️ **生成类模式不提供思考档**：切到生成图像/创建视频等模式后，输入区换成模式参数条，
  「自动/思考/快速」下拉**不渲染** → `set_thinking()` 会给出明确错误（先 `set_mode('')` 退出）。
- 思考正文默认折叠成「已经完成思考」卡片，点开才在
  `.qwen-chat-thinking-and-sources-content` 出现，而且**挂在消息体外的浮层里**（不在
  `.qwen-chat-message-assistant` 内）——`_read()` 因此有全页兜底查找。

### 5. 产物
| 模式 | 产物形态 | 取法 |
|---|---|---|
| 生成图像 | cdn 图片 URL | `assets[].kind=image` ✅ 实测 45s 出图 |
| 幻灯片 | 附件卡 + 预览图（交互式：先给大纲，回「确认」才生成） | `attachments` + `images` ✅ 实测 |
| 网页开发 | 只有一张附件卡（无直链/无 iframe srcdoc/点预览无反应） | 只能给卡片信息 → **未声明** |

## 风控自保（硬约束）

`risk_state()` 查 DOM 文案（人机验证/滑动验证/请完成验证/哎哟喂…）+ 选择器
（`[class*=captcha]`、`[id*=punish]`…）；`guard()` 命中即**抛错停手**，不再点、不再
发、不再重试，并落 `shots/risk_*.png`。检查点：每个点击前、`ask` 发送前、等待回答
的每一轮轮询、生成类模式等产物时。文档里说的 `RGV587` 响应体检测仍在（从没成功过时
才报风控错误）。

## 已验证（2026-10-19 活体，本机实测）

| 项 | 结果 |
|---|---|
| `probe` | `logged_in=true`、`model=Qwen3.7-Plus`、`thinking=思考`、`risk=[]` |
| `models` | 7 个（一级 3 + 二级 4），`tier` 正确 |
| `set_model` | 二级 `Qwen3.6-Plus` 选中并回切一级 `Qwen3.7-Plus` ✅ |
| `ask --search` | 40.6s，答案 + `references`（issinfo.net）✅ |
| `ask --files`（vision） | 22.5s，答对图内「红方块 + 蓝圆」✅ |
| `ask --think` | 思考正文 500+ 字 ✅ |
| `ask --mode t2i` | 45.1s，`images[0]` = cdn URL ✅ |
| `ask --mode slides` | 首轮大纲（`needs_input`）→ 回「确认」→ 附件卡 + 预览图 ✅ |
| `ask --mode web_dev` | 92s，只有附件卡 + 预览图，正文取不到（诚实记 partial） |
| `mode t2v` → `vparams` | 读全四组：清晰度 720P/1080P(VIP) · 编辑模式 视频生成/多参考图 · 比例 6 档 · 时长 5s/10s/15s ✅ |
| `vset --ratio 3:4 --duration 10s` | 摘要 `720P · 10s`、`current.ratio=3:4` ✅；改回 16:9/5s 也 ✅ |
| `vset --clarity 1080P` | **拒绝**（VIP 徽章）并说清原因，不瞎点 ✅ |
| `mode t2i` → `setcfg --key model/ratio` | `Qwen-Image 2.0 ↔ 3.0`、`16:9 ↔ 9:16` 来回切 ✅；3.0 无比例 → 报错带提示 ✅ |
| `think` | 普通对话 `自动 ↔ 思考` ✅；生成类模式明确报「该模式没有思考档」✅ |
| 「工具」语义 | 空入口（无子菜单/无芯片/无弹层/localStorage 不变）→ 如实记 `supported=False` ✅ |
| 加号菜单穷尽 | 一/二级共 12 条全枚举（report6/11/14），与 `PLUS_MODES` 一一对齐 ✅ |
| 离线契约测试 | `python3 tests/test_qwen_backend.py` → **99 passed** |
