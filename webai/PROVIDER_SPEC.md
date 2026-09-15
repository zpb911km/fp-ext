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

在 `public/webai/` 下运行：

```bash
cd <data>/public/webai && python3 - <<'PY'
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
cd <data>/public/webai && python3 tests/check_verify_live.py --yes
```

这条不是可选项。2026-09 就是这么抓到的：qwen 的 verify() 原本打 `/api/v2/models`，
**游客可访问** → 凭据全坏也回 200 → verify() 永远报 ok → `--check` 形同虚设、
AUTH 自愈永不触发。而当时"凭据正常 → 返回 ok"的测试**是绿的**。
改用 `new_session()` 探针后四家都能识破坏凭据。

`verify()` **不得**触发 `login.py`（会递归）。它只报告状态。

## 10. 凭据自愈（provider 不需要写任何代码）

`webai.get()` 返回的是 `_HealingProvider` 包装：`ask / search / upload / poll` 抛出的异常
若被 `classify()` 判为 `AUTH`，会先调 `login.silent_refresh()`（headless、不等人、
子进程 + 硬超时 + 并发锁），成功则**重试一次**，失败则原样抛出。

因此新增 provider **无需**处理登录 —— 只要：

1. `available()` 诚实报告本地有没有凭据；
2. `classify()` 认得自家的鉴权方言（其余交给公共词表）；
3. 提供 `verify()`。

关掉自愈：`FP_WEBAI_NO_AUTOLOGIN=1`。
