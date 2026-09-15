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
    kind: str           # image | video | ppt | webpage | report | file
    url: str
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
    assets: list[Asset] # 注意是 list —— GLM 一次给 4 张
    references: list
```

### 5.3 接口（5 个动词封顶）

```python
def ask(text, *, session, files, model, **opts) -> Reply
def run(task: str, inputs: dict, *, model, **opts) -> Job | Reply   # task 是开放字符串
def poll(job) -> Job
def models() -> list[ModelInfo]                 # 运行时枚举
def probe(capability) -> CapabilityInfo         # 运行时探测
```

`task` 为**开放字符串** —— 无限性只有一个入口，而不是 N 个 if 分支。

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

---

## 8. 未验证 / 待办

- [ ] GLM 生视频（`assistant_id=668d03b2e99d661ed3c32516`）是否同为同步 SSE？
- [ ] GLM 的 PPT / 深度研究 / 海报 / 数据分析（`engine_*` 模式）形态未知
- [ ] Qwen `slides` / `deep_research` / `web_dev` 形态未知
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
