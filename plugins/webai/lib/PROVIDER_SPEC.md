# webai provider 契约（新增部分）

> 本文是 `__init__.py` 文档里"provider 契约"的**补充**，只讲**新增的可选钩子**。
> 老契约（`ask() -> dict` 且返回 `text`/`thinking`/`message_id`/`references`/`queries`）
> **一个字都不能改** —— 工具层在用它。

## 0. 铁律（违反即视为失败）

1. **老 key 必须原样保留**：`text` / `thinking` / `message_id` / `references` / `queries`。
   `ask()` 的签名和返回类型（dict）不变。
2. **未知的东西不能丢**。这条已经被踩过三次：
   - deepseek 的 `response/search_status` 也回 `FINISHED` → 提前断流，正文全丢；
   - qwen 的 `phase == "image_gen"` → 图片 URL 被当垃圾丢掉；
   - qwen 的 `phase == "slides"` → 幻灯片产物全在那一帧里，被丢掉。
   **一律不准用"已知 phase 白名单"去筛流。** 见 §2。
3. **不准新增第三方依赖**（只用标准库 + 该文件已有的 import）。
4. **不准联网跑测试**（凭据可能过期，且会烧额度）。用 `§6 自测` 的离线方式。

---

## 1. `CAPABILITY_MAP: dict[str, dict]`

声明"本家怎么请求某个能力"，方言由各 provider 自己填。

```python
CAPABILITY_MAP = {
    # 能力名（开放字符串，用平台自己的叫法） → 建会话/发消息时要带的参数
    "t2i":  {"chat_type": "t2i"},
    "t2v":  {"chat_type": "t2v"},
}
```

GLM 用的是 `assistant_id` 而不是 `chat_type`，那就填 `{"assistant_id": "..."}` ——
**这一层只负责"如实描述各家方言"，不做统一。**

若本家没有生成类能力，写 `CAPABILITY_MAP = {}` 即可。

---

## 2. `ask()` 返回 dict 的**新增可选 key**

```python
{
    # ── 老 key（必须保留）──────────────────────
    "text": str, "thinking": str, "message_id": str,
    "references": list, "queries": list,

    # ── 新 key（有就填，没有不影响）────────────
    "assets":  [ {"kind": "image", "url": "https://..."} , ... ],
               # 内联内容（无 URL）用 {"kind": "code", "content": "<html>…"}
    "model":   str,          # **实际**使用的模型（服务端可能静默替换）
    "phases":  {phase: content},   # ⚠️ 见下，所有出现过的 phase 都要收进来
    "extra":   dict,         # 原样透传的关键 extra（不要加工）
    "status":  str,          # 仅在"交互式能力在追问"时填 "needs_input"
    "task_id": str, "task_status": str,   # 异步任务：提交后立刻返回
    "truncated": bool,       # ⚠️ 见 §12：流**被中断**（不是正常收尾）时填 True
    "truncated_reason": str, # 可选，中断的原始异常（便于排查）
}
```

### `phases` 的要求（最重要）

流里**每一个** `delta` 帧，只要带 `phase`，就把它记进 `phases`：

```python
phases.setdefault(phase, "")
phases[phase] += content        # delta 帧
```

不允许 `if phase == "answer": ... elif phase == "thinking": ...` 之后**丢掉 else**。
已实测存在的 phase：`answer` / `thinking` / `thinking_summary` / `image_gen` /
`video_gen` / `slides` / `web_search` —— **将来还会长出新的，所以不能枚举。**

正文（`text`）仍然只取你原来取的那些（`answer`），这保证了老行为不变；
**多出来的是"不丢"，不是"改行为"。**

### `assets` 的要求

从流里认出产物，归一成 `{"kind","url"}` 或 `{"kind","content"}` 的形状（**别自己建对象**，
`core.coerce_assets()` 会转成 `Asset`）。已知的对应（实测）：

| provider | 产物在哪 | kind |
|---|---|---|
| qwen | `phase=="image_gen"` 帧的 `content` 是**图片 URL 文本** | `image` |
| qwen | `phase=="slides"` 帧的 `extra` 里有图片/PDF | `image` / `pdf` |
| qwen | `phase=="video_gen"` 帧的 `content` 是视频 URL | `video` |
| glm | `content[].type=="image"` → `image[].image_url`（一次 4 张，到达乱序） | `image` |
| deepseek/stepfun | 暂无 | — |

**URL 一律不要写进日志/注释/测试固件**（qwen 的 URL 带 JWT，含 `resource_user_id`）。

### 交互式能力 → `status="needs_input"`

实测 `slides` / `deep_research` 会**主动追问**（"请确认" / 连续多轮反问）。
识别到"这一轮是它在问我"时填 `"status": "needs_input"`。
拿不准就别填 —— 宁可漏报也不要误报。

---

## 3. `models() -> list[dict]`

```python
[{"id": "qwen3.7-plus", "name": "Qwen3.7-Plus",
  "capabilities": {"vision","search","thinking"}, "ctx": 1000000, "default": True}]
```

- **有权威来源就去拿**（qwen: `GET /api/v2/models`；glm: `available_models` 配置）；
- **没有就返回 `[]`** —— 这是诚实的选择，别硬编一堆可能过期的名字；
- 任何异常都 `return []`，不要往外抛（调用方不该为一个可选能力崩）。

## 4. `probe(capability="") -> dict`

```python
{"supported": True|False|None, "via": "config_api", "note": "…"}
```

`None` = 不知道。**宁可 None 也不要猜。** 没实现的 provider 干脆别定义这个函数，
`__init__.probe()` 会自动降级。

## 5. `poll(job) -> dict`

只有**真异步**的才需要（目前只有 qwen 的 `t2v`）。返回：

```python
{"task_id": job.id, "task_status": "success|processing|error",
 "content": "https://…", "remaining_time": "…", "message": "失败原因"}
```

`core.coerce_job()` 会做状态名归一（`success`→`done`、`processing`→`running`）。

## 6. `classify(text="", status=0, raw=None) -> str`

返回 `"auth"|"quota"|"unsupported"|"transient"|"content_policy"|"unknown"` 之一。
`core.classify_text()` 已经有一份通用中文/英文词表，本家**只在有特殊方言时**才需要覆盖。

已知的真实方言（请务必收进去）：

| provider | 服务端文案 | 应归类 |
|---|---|---|
| deepseek | `rate_limit_reached` | `quota` |
| stepfun | 「换个话题聊聊」 | `content_policy` |
| glm | `40014`（签名过期） | `auth` |
| glm | 「权限不足」 | `unsupported` |
| qwen | 「该模型不可用」 | `unsupported` |

签名是 `classify(text="", status=0, raw=None, exc=None) -> str`（多余参数用 `**_` 吃掉也行，
`__init__.classify()` 传的是这四个关键字）。

## 7. `new_session(model="", capability="", **dialect) -> str`

必须能接受**多余的关键字**（`__init__.new_session()` 会按 `CAPABILITY_MAP` 展开传入）。
最省事的写法：

```python
def new_session(model: str = "", capability: str = "", **dialect) -> str:
    ...
```

不支持 capability 就**忽略它**（`**dialect` 已经吃掉），绝不要抛异常。

---

## 8. 自测（必须跑，离线）

在 `public/plugins/webai/lib/` 下运行：

```bash
cd <data>/public/plugins/webai/lib && python3 - <<'PY'
import importlib.util as iu, sys
spec = iu.spec_from_file_location("webai", "__init__.py", submodule_search_locations=["."])
pkg = iu.module_from_spec(spec); sys.modules["webai"] = pkg; spec.loader.exec_module(pkg)
m = pkg.get("<你的 provider>")
assert isinstance(m.capabilities, set) and {"chat"} <= m.capabilities
assert isinstance(m.CAPABILITY_MAP, dict)
print("models:", m.models()); print("probe:", pkg.probe("<你的 provider>", "chat"))
print("classify:", m.classify(text="<方言文案>") if hasattr(m,"classify") else "n/a")
# 用合成的 SSE payload 喂解析器，验证：未知 phase 不丢 + 产物被提取
PY
```

**并用合成数据验证 §2**：构造一个含**未知 phase**（如 `"brand_new_thing"`）的 payload，
断言 `phases` 里有它；构造一个 `image_gen` 帧，断言 `assets` 里有图。

最后必须跑一次**语法与导入检查**，并确认 `pkg.available()` 四家都不报错。

---

## 9. `verify() -> tuple[str, str]`（凭据自检）

给统一入口 `login.py`（`--check` / 静默刷新后的确认）用：判断**凭据是否被服务端认可**。

返回三态之一：

| 返回 | 含义 | 调用方动作 |
|---|---|---|
| `("ok", "")` | 服务端认可 | 无 |
| `("dead", why)` | 服务端**明确**拒绝（401/403 / 明确的鉴权文案） | 需要刷新凭据 |
| `("unknown", why)` | 网络不通 / 端点不确定 / 认不出来 | **不刷新** |

**铁律：拿不准一律 `unknown`。** 把网络抖动报成 `dead`，会导致每次抖动都去弹一次浏览器登录 ——
比漏刷糟糕得多。只有"确实是鉴权被拒"才回 `dead`。

实现经验：

* **有只读接口的后端**（qwen / glm）→ 打一个**需要鉴权**的只读 GET，按 HTTP 状态码判。
  ⚠️ 别用游客也能访问的配置类接口（如 `config/operation_data` / `configs/`）——
  它们 200 不代表登录有效（实测：未登录也能拿到数据）。
* **其余**（deepseek / stepfun）→ 用真实的鉴权调用做探针（`new_session()`）。
  代价是服务端会多一个空会话，但比猜端点可靠。异常文本不在 provider 内判死，
  交给 `login.py` 用**公共词表**（`core.AUTH_HINTS`）补一刀
  （provider 的局部 `classify()` 只认自家方言，认不出就回 `""`）。

### ⚠️ 必须做负向测试（否则等于没测）

**"凭据有效时返回 ok" 什么也证明不了** —— 一个游客也能访问的接口同样回 200。
必须反过来测：把凭据换成垃圾，断言它报 `dead`。

```bash
cd <data>/public/plugins/webai/lib && python3 tests/check_verify_live.py --yes
```

这条不是可选项。2026-09 就是这么抓到的：qwen 的 verify() 原本打 `/api/v2/models`，
**游客可访问** → 凭据全坏也回 200 → verify() 永远报 ok → `--check` 形同虚设、
AUTH 自愈永不触发。而当时"凭据正常 → 返回 ok"的测试**是绿的**。
改用 `new_session()` 探针后四家都能识破坏凭据。

`verify()` **不得**触发 `login.py`（会递归）。它只报告状态。

## 10. 凭据自愈（默认 provider 不需要写任何代码）

`webai.get()` 返回的是 `_HealingProvider` 包装，它有**两条互不串门的通道**：

| 归类 | 动作 | 为什么 |
|---|---|---|
| `AUTH` | 调 `login.silent_refresh()` → 成功则**立刻**重试（不退避，只刷一次） | 换凭据是唯一出路 |
| `TRANSIENT` | **退避重试** 2 次（0.8s / 2.5s） | 网络断流 / 5xx / 读超时，重发大概率就好 |
| 其余 | 原样抛出，不重试 | `QUOTA` 该停手、`UNSUPPORTED` 该换家、`CONTENT_POLICY` 重试无用 |

> **TRANSIENT 这条是踩出来的**（2026-09 用户反馈"copilot 里 qwen 老是断联"）：
> 以前这里只认 `AUTH`，网络抖一下就直接抛给用户 —— 而 qwen 恰好是 copilot 的默认后端，
> 所以看起来像"qwen 的问题"。判据是：**各类错误的动作是相反的**，
> 判错会让调用方做相反的事，所以 `TRANSIENT` 必须与 `AUTH` 分开。
>
> 关掉重试：`FP_WEBAI_NO_RETRY=1`。

`silent_refresh()` 内部是**两级**：

1. **本地续期**（SPEC §11，~0.3s）：provider 自带 `refresh_credentials()` 就直接换 token；
2. **开浏览器**（headless、不等人、子进程 + 硬超时 + 并发锁）：兜底。

因此新增 provider **默认无需**处理登录 —— 只要：

1. `available()` 诚实报告本地有没有凭据；
2. `classify()` 认得自家的鉴权方言（其余交给公共词表）；
3. 提供 `verify()`。

关掉自愈：`FP_WEBAI_NO_AUTOLOGIN=1`。

### `no_retry` 标记（避免"内层重发 × 外层重发"）

provider 若**已经自己重发过**，或某错误**重发治不了**，请给异常打标记：

```python
err = WebAIError("transient", "…")
err.no_retry = True
raise err
```

否则一个用户请求会被打成一串（qwen 实测：内层 2 次 × 外层 3 次 = 6 发）。

## 11. `refresh_credentials() -> bool`（可选：免浏览器续期）

**当本家的凭据本身是"短命 access + 长命 refresh"结构时，务必实现它。**

不实现的代价（实测 stepfun）：access 段只活 ~29 分钟，过期后每次都 401；
自愈只能开浏览器，慢且依赖 profile —— 用户体感就是"这个后端总是鉴权失败"。

```python
def refresh_credentials() -> bool:
    """用 refresh 段换新 access 段并落盘。成功 True；可预期的失败静默 False。"""
```

要求：

* **只做"续期"**，不做"登录"：没有 refresh 段/凭据真死了 → `False`（上层会开浏览器）；
* **必须把新凭据落盘**（下次进程直接可用），权限 0600，且**原子替换**
  （provider 每次请求前都读它 → 直写会留下"读到半个 JSON"的窗口 → 假的鉴权失败）；
* **必须校验换回来的是不是真凭据**。反面教材（实测 stepfun）：RefreshToken 端点在
  凭据无效时**照样回 200**，但给的是游客 token（`activated: false`，拿去调用得到
  403 `need sign in`）—— 不校验就会把"过期"刷成"权限不足"，**比不刷新更糟**；
* **"尽力而为"≠"静默失败"**。可预期的网络/解析异常 → `False`；
  **不认识的异常 → 也必须 `False`，但要打到 stderr**。实测教训：一句
  `s.cookies.get("Oasis-Webid")` 抛 `CookieConflictError`（同名 cookie 有多个域），
  被宽 `except Exception: return False` 吃掉 → 续期"永远失败"→ 每次都退回开浏览器，
  **症状与不修一模一样**，白忙一场。看不见的失败会伪装成"已修好"。
  （另一条：`except (requests.RequestException, ...)` 的元组是**异常时**才求值的 ——
  先存成模块级常量，否则 except 子句自身会抛 `AttributeError` 盖掉原始异常。）
* 建议顺带在**真实调用前**做临期预判（如 access 剩余 <90s 就先换），省掉一次注定 401 的往返；
* ⚠️ 有些服务端的 refresh token **一次一换**（用过的立即作废）：成功必须**立刻**落盘新的一对，
  否则下一次续期只会拿到游客态。

## 12. 流完整性：`truncated` 与"什么才算截断"

**症状**：多轮对话"聊着聊着断了"——回答短了一截、或对方下一轮忽然记不起刚才说的话。

**要求**：`ask()` 收到**传输层中断**时，已收到的内容**照常返回**（铁律：不丢东西），
但必须带 `truncated=True`（+ 可选 `truncated_reason`）。上层据此提示用户，
而不是静默把半截回答当完整回答。

### ⚠️ 判据是"有没有报错"，不是"有没有 `[DONE]`"

这条是**联网实测**出来的（2026-09）：

* **qwen 的 SSE 根本不发 `data: [DONE]`**。它靠 `Transfer-Encoding: chunked`
  正常收尾；末帧带 `usage.total_tokens`，但 `usage` 从第 2 帧起就一直在，
  **当不了结束标记**。所以"没见到 `[DONE]`"绝不能当截断 ——
  第一版就是这么写的，结果**每一次正常回答都被误报成 truncated**（本地离线测试全绿，
  是 `check_qwen_resilience_live.py` 抓出来的）。
* 可靠信号只有一个：**传输层报错**。分块编码下服务端没写完就断开，
  urllib3 会抛 `IncompleteRead` → `ChunkedEncodingError`。
* **诚实边界**：服务端**主动**提前结束（风控掐断 / 用户点停止）在协议层没有任何标记，
  客户端分不出来。别假装能检测 —— 把它写成"疑似截断"只会变成狼来了。

### 重发规则（与"不重复提问"的权衡）

* **一个字都没收到** → 可以重发（此时服务端大概率没落地）。重发请**复用同一份请求体**
  （含同一个 `fid`/`timestamp`），给服务端去重的机会；
* **已经收到内容** → **不要**重发。重发会让对方收到两条相同的提问，
  比"回答短了一截"更糟。返回内容 + `truncated=True` 即可。
* 内层重发用尽后，抛出的异常请打 `no_retry`（见 §10），别让外层再退避一遍。

参考实现：`qwen.py` 的 `_collect_stream()` / `ask()`；联网验收：`tests/check_qwen_resilience_live.py`。
