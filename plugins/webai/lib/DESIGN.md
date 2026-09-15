# webai 能力层设计

> 目的：把 webai 从「chat / search / vision 三个能力」扩展到「任意生成类能力」，
> 并且**加一个新能力不需要改调用方**。
>
> 本文所有形态描述均来自真实抓包，未验证的部分单独标注。

---

## 1. 实测：三种生成能力的形态

| | Qwen 生图 `t2i` | GLM 生图 | Qwen 生视频 `t2v` |
|---|---|---|---|
| 提交端点 | `POST /api/v2/chat/completions` | `POST /chatglm/backend-api/assistant/stream` | 同 `chat/completions` |
| 是否流式 | `stream: true` | SSE | **`stream: false`** |
| 能力标识 | `chat_type`/`sub_chat_type = "t2i"` | **`assistant_id`** | `chat_type = "t2v"` |
| 能力参数 | `extra.meta{size,model}` + 顶层 `size` | `meta_data.cogview{aspect_ratio,style,scene,rm_label_watermark}` | `extra.meta{size}` + 顶层 `size` |
| 产物位置 | **`delta.content` 里的一段 URL 文本** | `content[].type=="image"` → `image[].image_url` | `GET /api/v2/task/status/<id>` 的 `content` |
| 产物数量 | 1 | **4**（`answer_type: "multi"`） | 1 |
| 耗时 | ~20s | 13.1s | **~145s**（15 次轮询，~10s 间隔） |
| URL 形态 | JWT 签名（**含 user_id**），会过期 | `sfile.chatglm.cn/testpath/…`，无签名参数 | JWT 签名，同 t2i |

### 异步任务的状态端点（Qwen）

```jsonc
GET /api/v2/task/status/496149f7-03a7-401d-8b4e-de7a036cb254
{
  "success": true,
  "data": {
    "chat_type": "t2v",
    "task_status": "success",      // 任务状态机
    "message": "",                 // 失败原因
    "remaining_time": "",          // 可直接做进度反馈
    "content": "<视频 URL>"         // 产物，复用 content 字段名
  }
}
```

### 交互式能力：Qwen `slides`（第三种形态 —— 多轮工作流）

`slides` 不是"一次调用拿产物"，而是**两阶段交互**：

| 轮次 | 输入 | 输出 | 耗时 |
|---|---|---|---|
| 1 | "做一份关于量子计算的3页幻灯片" | 草案（`phase:"answer"`）+ **结构化预案** `extra.draft_plan`，末尾要求回复"确认"或"开始" | ~75s |
| 2 | "确认" | `phase:"slides"` → `extra.slides`：`slide_pages[]`（3 张 PNG 2688×1536）+ **`pdf_url`**（3 页 PDF） | **252s** |

`extra.draft_plan` 是**机器可读**的（不是纯自然语言）：

```jsonc
{"version":1, "topic":"…", "topic_slug":"quantum-computing-popularization",
 "big_idea":"…", "target_audience":"…", "density_hint":"minimal", "slide_count":3,
 "slide_headlines":[…], "language":"zh-Hans", "master_style":"glassmorphism",
 "must_include":[…], "must_avoid":[…], "cover_title":"…", "cover_subtitle":"…"}
```

→ **平台自己已经把"提案"结构化了**，不需要发明 `Plan` 抽象，原样搬运即可。

三个后果：

1. **`phase` 不能做白名单** —— 已知就有：`answer` / `thinking*` / `thinking_summary` / `image_gen` / `video_gen` / `slides` / `web_search`。漏掉一个就是**静默丢产物**（我们已经犯过一次）。
2. **`Reply`/`Asset` 需要"原样透传 provider 元数据"的槽位**（`extra`）。能力语义千变万化，但平台已经结构化了 —— **别建模，搬运**。
3. **一次调用可能要等 4 分钟**，工具层超时得覆盖。

### 另外两个 Qwen 能力（第四、第五种形态）

**`web_dev`（网页开发）—— 产物是源码，不是资产**

| 项 | 值 |
|---|---|
| 菜单名 | 网页开发 |
| `chats/new` 的 `chat_type` | **`artifacts`** ← 注意不是 `web_dev` |
| 消息的 `sub_chat_type` | `web_dev` |
| 产物 | **`phase:"answer"` 的正文本身** —— 11,186 字符 HTML，包在代码围栏里 |
| 有无 URL | **没有**。前端自己渲染成 iframe 预览（DOM 里有「预览 / 代码 / 部署」） |
| 耗时 | ~15s，单轮 |

→ **第四种产物载体：正文文本本身。** `Asset` 要能装"无 URL、内联源码"。

**`deep_research`（深入研究）—— 强制澄清循环**

| 项 | 值 |
|---|---|
| 菜单名 | 深入研究 |
| `chat_type` | `deep_research` |
| `sub_chat_type` | **`deep_thinking`** |
| 首轮行为 | **只反问，不出报告** |
| 能否跳过 | **否** —— 明说"无需再确认"仍继续追问；连答 3 轮，3 轮都继续追问 |

→ 不是"模型可能问也可能不问"，是**设计上的交互门控**。
→ 工程后果：调用方必须能识别"它在问我"，且**必须设最大轮数**，否则会无限对话下去。

---

## 2. 结论一：**不存在"独立端点"**

Qwen 用 `chat_type` 参数、GLM 用 `assistant_id`，两家的生成能力都走**和普通聊天完全相同的端点**。

> 曾经以为 GLM 有 `drawing-api` / `video-api` 独立端点 —— 错。
> `drawing-api/v1/drawing/config` 只是个**配置** GET，生成走 `assistant/stream`。

→ **抽象必须落在「能力」层，不能落在「端点」层**，否则每家一套，永远统一不了。

---

## 3. 结论二：**存在两种提交形态**，且服务端已给出信号

| 形态 | 判别信号 | 取结果方式 |
|---|---|---|
| **同步流式** | `stream: true` / SSE | 同一个流里内联，直接从 delta 里捞 |
| **异步任务** | **`stream: false`** | 返回 `message_id` → 轮询 `task/status` |

关键：**`stream: false` 是显式信号**，客户端不必去猜"这个能力是不是长子任务"。
provider 层读到它就能决定走哪条路。

---

## 4. 结论三：**产物形状每家都不同** → `Asset` 必须在 provider 内完成归一

三条链路，三种拿产物的方式（见 §1 表格）。

尤其注意 Qwen t2i：**URL 是混在 `delta.content` 文本里返回的** ——
任何"提取正文"的解析器都会把 URL 当成回答文本交给用户。

→ 归一化只能发生在 provider 内部，工具层拿到的必须是同一个 `Asset`。

## 4.5 结论四：能力可能是**有状态的 agent**，不只是函数

`slides` 会**中断并要求确认**，还用 `slides_milestone` 报进度。它的行为更像一个 sub-agent，而不是一个函数。

但这对协议层**不构成新问题** —— 它只是多轮对话：

- `chat_type` 在 `chats/new` 时就确定 → **能力是会话级属性**，多轮能力天然待在同一个会话里
- "确认" 就是下一轮 `ask`

→ **编排（要不要发第二轮）属于上层，不属于协议层。**
协议层如实返回文本 + `extra` 即可；要不要确认由调用方决定。

→ 这也再次否掉了 `run()` 这个动词：如果它只是"自动发确认"，那是一个**helper**，不是协议原语。

## 4.6 结论五：能力的命名有**三层，且互不相等**

| 菜单名 | `chat_type` | `sub_chat_type` |
|---|---|---|
| 生成图像 | `t2i` | `t2i` |
| 创建视频 | `t2v` | `t2v` |
| 幻灯片 | `slides` | `slides` |
| **网页开发** | **`artifacts`** | `web_dev` |
| **深入研究** | `deep_research` | **`deep_thinking`** |

**没有哪一层等于另一层。** 且 `artifacts` 是**容器型 chat_type**，可承载多个 sub 能力。

→ 这**证伪了"枚举能力"的可行性**：连命名都对不上，根本列不全。
→ 也否掉了上轮"能力 = chat_type"的简化（错的）。

**所以能力名只能是开放字符串，靠探测/试错得到。**

---

## 5. 抽象设计

### 5.1 核心区分：契约 vs 事实

| | 内容 | 特性 |
|---|---|---|
| **契约（稳定）** | 5 个动词、6 类错误、3 种返回（Reply / Asset / Job） | 天然少且收敛 |
| **事实（易变）** | 能力名、模型名、参数、限额、账号权限 | 无限、善变 |

**错误做法**：把事实写进契约（如写死 `capabilities = {"chat","search","vision"}`）——
会得到一个永远在过期、且要人工维护的 capability × provider 大表。

**正确做法**：能力是**运行时探测出的事实**，不是承诺。

### 5.2 三个公共词汇

```python
@dataclass
class Asset:            # 万能产物：调用方只认它，不认平台
    kind: str           # image | video | pdf | ppt | webpage | code | file
    url: str | None     # ← 可空：web_dev 的产物根本没有 URL
    content: str | None # ← 内联内容（如 HTML 源码），与 url 二选一
    path: str | None    # 已落盘（URL 会过期，重要）
    meta: dict          # 宽高 / 时长 / 页数 / 引用
    expires_at: int | None

@dataclass
class Job:              # 所有异步长任务的统一形状（Qwen t2v 已验证）
    id: str
    status: str         # pending | running | done | failed
    assets: list[Asset]
    progress: float     # 可由 remaining_time 推

@dataclass
class Reply:            # 同步返回
    text: str
    thinking: str
    assets: list[Asset] # 注意是 list —— GLM 一次给 4 张；slides 给 3 图 + 1 PDF
    references: list
    meta: dict          # ← provider 的 extra 原文，原样透传，不做解释
```

### 5.3 接口（**4 个动词** —— 原写"5 个"，实测后 `run()` 被删掉）

```python
def new_session(capability="", model="") -> str   # ⚠️ 能力是**会话级**属性
def ask(session_id, text, *, files, model, **opts) -> Reply | Job
def poll(job) -> Job
def models() -> list[ModelInfo]                   # 运行时枚举，拿不到就 []
def probe(capability) -> CapabilityInfo           # 尽力而为，允许 supported=None
```

**修订记录（为什么少了一个）**：

1. **`run(task, inputs)` 删掉。** 原设计假设"生成类调用"和"聊天"结构不同。实测三家生成能力
   （Qwen `t2i`/`t2v`、GLM 生图）**输入都是文本、返回都是"一条带资产的消息"**，走的还是 chat 端点。
   所以能力是 `ask()` 的**参数**，不是独立动词。
2. **能力被提到 `new_session()` 上。** 实测 `chat_type` 在 `POST /api/v2/chats/new` 时就定了，
   不是每条消息能切 —— 能力是**会话级属性**。
3. **`run()` 若存在，会是个"会做错产品决策的 helper"**：`slides` / `deep_research` 实测会追问
   （幻灯片给 3 个受众方向 × 3 种风格让选），自动发"确认"等于替用户锁死默认值。

**能力名是开放字符串**：`t2i` / `image_gen` / `cogview` 都合法。
跨家匹配靠 `core.CAPABILITY_ALIASES` 折叠成通用名（**只用于路由，不用于调用**）；
发起调用时一律用**这家自己的键**（`resolve_capability()` 负责翻译）。

### 5.4 错误归一化（客户端不要建模限额）

```python
ErrorKind = AUTH | QUOTA | UNSUPPORTED | TRANSIENT | CONTENT_POLICY | UNKNOWN
```

限额是**服务端状态**，穷举不完；但错误**类别**有限且稳定。
归一化之后才能做真降级：一家 `QUOTA` / `UNSUPPORTED` → 换一家。

### 5.5 模型选择

三件事要分开：

1. **对话模型** —— Qwen `model: "qwen3.7-plus"`
2. **能力模型** —— Qwen `extra.meta.model: "qwen-image-2.0-pro"`（UI 里的「Qwen-Image 2.0」）
3. **推理档** —— GLM `am_effort: low|high|max`（快速/深度/极致）；现在是布尔 `think`，**应升级为档位字符串**

⚠️ 会话绑模型：换模型 = 新模型接旧上下文树。语义危险，需显式记录 + 提示。

---

## 6. 各 provider 能力映射（实测）

### Qwen

| 能力 | 映射 |
|---|---|
| 能力清单 | `GET /api/v2/configs/` → `feature_feature` / `feature_file` / `limits` / `permissions` |
| 模型清单 | `GET /api/v2/models` → 6 模型 + 每模型能力矩阵 |
| 建会话 | `POST /api/v2/chats/new`（带 `chat_type`） |
| 生图 | `completions` + `chat_type="t2i"` + `extra.meta.model="qwen-image-2.0-pro"` |
| 生视频 | `completions` + `chat_type="t2v"` + `stream:false` → 轮询 `task/status` |
| 历史 | `GET /api/v2/chats/<id>?direction=up&limit=10` |

能力菜单全集：`t2i` `t2v` `search` `deep_research` `web_dev` `slides` `artifacts` `learn` `travel`
（另有两个未在菜单出现的上下文相关能力：`image_edit`、`agent_mode`）

### GLM

| 能力 | 映射 |
|---|---|
| 能力清单 | `GET /chatglm/operation-api/config/operation_data?tag=…`（含 `text2img_list` 等） |
| 模型清单 | `GET /chatglm/agent-api/operation/detail?tag=available_models` |
| 普通聊天 | `assistant/stream` + `assistant_id=65940acff94777010aa6b796` |
| 生图 | `assistant/stream` + `assistant_id=65a232c082ff90a2ad2f15e2` + `meta_data.cogview` |
| 生视频 | `assistant_id=668d03b2e99d661ed3c32516`（**未实测**） |
| 其他助手 | AI阅读 `658a7988b8a9a98d38725745` · 学习搭子 `68f0b8c110eea3e78b0e0e5e` |

### DeepSeek / StepFun

- **DeepSeek**：只有 chat + search + TTS。**无能力清单接口** —— 能力只能靠"试一次看报什么错"。
- **StepFun**：是"运行时环境"（cron / deploy / bots / skills），**不是能力超市**。
  套不进 `run()`，**不要硬统一**。

---

## 7. 已知坑

| 坑 | 说明 |
|---|---|
| `stream: false` 的响应含义 | 不代表失败，是"已受理，去轮询" |
| Qwen 产物 URL 含 `resource_user_id` | JWT payload 里有用户 ID → **落盘后别把 URL 写进日志/仓库** |
| GLM 图片到达顺序 ≠ 编号顺序 | 4 张并行生成，`_0 _3 _1 _2` 乱序到达 |
| GLM `one_to_more_finish` | 末帧哨兵 content type，别当正文 |
| GLM 双态流 | 中间帧是 delta，末帧是全量快照（拼正文会重复） |
| Qwen 搜索需 `think=true` | `auto_search=True` 在 `think=false` 时被静默忽略 → 强制 think 后 **76s**（deepseek 仅 5s） |
| GLM `output_image_hw` 是 `[高,宽]` | 名字骗人 |
| GLM 没有建会话 API | 传 `""` 让服务端在首帧返回 `conversation_id`；自造 id → HTTP 200 但响应 0 字节 |
| **`phase` 白名单漏项 = 静默丢产物** | 已知 phase 就有 7 个；`slides` 的产物全在 `phase:"slides"` 帧里 |
| **Qwen 有阿里云 WAF** | 纯 HTTP 客户端直连 `completions` 会被 `_____tmd_____/punish?x5secdata=…` 拦（浏览器有反爬 token）。轮询类 GET 反而没事 |
| slides 第二轮 252s | 加草案轮共约 5.5 分钟，超时设置要够 |

---

## 8. 未验证 / 待办

- [ ] GLM 生视频（`assistant_id=668d03b2e99d661ed3c32516`）是否同为同步 SSE？
- [ ] GLM 的 PPT / 深度研究 / 海报 / 数据分析（`engine_*` 模式）形态未知
- [ ] `web_dev` 的「部署」按钮走什么接口（可能产出真实 URL）—— 未抓
- [ ] `artifacts` 容器下还有哪些 sub 能力 —— 未枚举
- [ ] slides 的 `pdf_url` 与 `slide_pages` 是否总是同时给 —— 单样本
- [x] Qwen `slides` 形态已实测 → **多轮交互**（见 §1）
- [x] Qwen `web_dev` 形态已实测 → 产物是**源码**（无 URL）
- [x] Qwen `deep_research` → **强制澄清循环**，未拿到报告（连追 3 轮）
- [ ] deep_research 的澄清循环有无深度上限 —— 未知（3 轮未见底）
- [ ] `slides` 的第二轮能否跳过（直接一次拿产物）—— 未试
- [ ] `probe()` 只在 Qwen / GLM 有配置接口可依；DeepSeek 无
- [ ] 产物 URL 的实际过期时间（Qwen JWT 有 exp；GLM `testpath` 未观察）

---

## 9. 落地顺序

| 步 | 内容 | 风险 |
|---|---|---|
| 1 | `core.py`：Asset / Job / Reply / ErrorKind | 无（纯新增） |
| 2 | 接口收成 5 动词，现有能力走 `run(task, inputs)` | 低 |
| 3 | 错误归一化（每家一个映射函数） | 低 |
| 4 | `models()` 枚举 + `model` 透传 | 低 |
| 5 | `probe()` —— 先只做 GLM / Qwen | 中 |
| 6 | 接生图（GLM 或 Qwen），验证 `Asset`/`Job` 够用 | 中 |

**原则：目标不是"兼容无限"，而是"不挡路"。**
前者要无限复杂度，后者接近零 —— 我们不必预见平台明天上什么功能，只需它上线时不用改代码。
