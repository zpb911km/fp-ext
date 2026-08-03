"""
Outline 插件 — 文件/目录结构大纲速览

定位：低摩擦的"地形感知"工具。给定一个路径（文件或目录），
返回结构骨架而非内容，几百 token 内建立"这张地图长什么样"的认知。

与 codegraph 的关系：outline 做"地图"，codegraph 做"定点深挖"。
看到大纲中的符号后想深入 → 再调 codegraph 或 read_file。

引擎分层（扩展性设计）：
  - DirEngine     : 目录 → tree 目录树 + tokei 语言/行数统计（tokei 缺失时降级）
  - CtagsEngine   : 代码文件 → universal-ctags JSON 符号表 → 层级缩进树（135+ 语言）
  - MdEngine      : .md → 标题层级树（内置，零依赖）
  - JsonEngine    : .json → 键结构树（内置，含嵌套层级）
  - YamlEngine    : .yaml/.yml → 键结构树（内置，缩进解析）

新增文件类型支持 = 新增一个引擎函数 + 路由表加一行。零依赖，纯标准库。
"""

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

# ── 工具定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "outline",
        "description": """获取文件或目录的结构大纲（骨架，非内容），token 开销小。

- 目录 → 目录树 + 语言/行数统计
- 代码文件 → 类/函数/方法/变量的层级缩进树（含签名与行号，135+ 语言）
- .md → 标题层级树；.json/.yaml → 键结构树
- 其他/二进制 → 行数、大小、首几行预览

适合：读新文件/新项目前先 outline 建立地图，再决定 read_file 哪些段落。
示例：outline(path=/path/to/agent.py)  /  outline(path=/path/to/project, depth=2)""",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要查看的文件或目录的绝对路径",
                },
                "depth": {
                    "type": "integer",
                    "description": "目录树深度，仅目录生效（默认 2）",
                },
            },
            "required": ["path"],
        },
    },
}

# ══════════════════════════════════════════════════════════════════════
#  通用辅助
# ══════════════════════════════════════════════════════════════════════

MAX_TREE_LINES = 120   # 目录树最大行数（防爆 token）
MAX_PREVIEW = 10       # 兜底文本预览行数
MAX_SIG_LEN = 60       # 签名最大长度


def _is_binary(path: Path) -> bool:
    with open(path, "rb") as f:
        return b"\x00" in f.read(1024)


def _count_lines(path: Path) -> int:
    with open(path, "r", errors="replace") as f:
        return sum(1 for _ in f)


def _read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _run_cmd(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )


# ══════════════════════════════════════════════════════════════════════
#  引擎 1：目录 → tree + tokei
# ══════════════════════════════════════════════════════════════════════

def _outline_dir(path: Path, depth: int) -> str:
    parts = []

    # 1. 目录树（tree 缺失时用 os.walk 降级）
    tree_lines = _run_cmd(
        ["tree", "-L", str(depth), "--dirsfirst", "--noreport",
         "--charset=ascii", str(path)], timeout=20
    )
    if tree_lines.returncode == 0 and tree_lines.stdout.strip():
        parts.append(f"📁 {path}  (目录树, depth={depth})")
        parts.append(tree_lines.stdout.rstrip()[: MAX_TREE_LINES * 200])
    else:
        parts.append(f"📁 {path}  (tree 不可用, 顶层结构)")
        parts.append(_fallback_listing(path, depth))

    # 2. tokei 语言统计（缺失时降级为扩展名计数）
    tok = _run_cmd(
        ["tokei", str(path), "--output", "json", "--sort", "code"], timeout=30
    )
    if tok.returncode == 0 and tok.stdout.strip():
        parts.append(_format_tokei(tok.stdout))
    else:
        parts.append(_fallback_lang_stats(path))

    return "\n".join(parts)


def _fallback_listing(path: Path, depth: int) -> str:
    """tree 缺失时的降级：os.walk 顶层列表。"""
    lines = []
    max_depth = depth
    for root, dirs, files in os.walk(path):
        rel = Path(root).relative_to(path)
        lvl = 0 if rel == Path(".") else len(rel.parts)
        if lvl > max_depth:
            continue
        indent = "  " * lvl
        lines.append(f"{indent}{rel.name}/")
        if lvl < max_depth:
            for f in sorted(files)[:15]:
                lines.append(f"{indent}  {f}")
    return "\n".join(lines[: MAX_TREE_LINES])


def _format_tokei(raw: str) -> str:
    """tokei JSON → 紧凑表格。结构(v14): {"Total": {"children": {lang: [{name, stats}]}}, "Python": {...}}"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    total_info = data.get("Total", {})
    total_children = total_info.get("children", {}) if isinstance(total_info, dict) else {}
    rows = []
    for lang, info in data.items():
        if lang == "Total" or not isinstance(info, dict):
            continue
        files = len(total_children.get(lang, [])) if isinstance(total_children.get(lang), list) else 0
        code = info.get("code", 0)
        comments = info.get("comments", 0)
        rows.append(f"{lang}: {files} 文件 / {code} 行代码 / {comments} 注释")
    if not rows:
        return ""
    total_files = sum(len(v) for v in total_children.values() if isinstance(v, list))
    head = f"语言统计（共 {total_files} 文件, {total_info.get('code', 0)} 行代码）:"
    return head + "\n  " + "\n  ".join(rows[:15])


def _fallback_lang_stats(path: Path) -> str:
    """tokei 缺失时的降级：按扩展名简单计数。"""
    counter: dict[str, int] = {}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules",
                                                ".venv", "venv", "build", "dist"}]
        for f in files:
            ext = Path(f).suffix.lower() or "(无扩展名)"
            counter[ext] = counter.get(ext, 0) + 1
    if not counter:
        return "（目录为空）"
    top = sorted(counter.items(), key=lambda x: -x[1])[:10]
    stats = " / ".join(f"{k}: {v}" for k, v in top)
    return f"语言统计（tokei 未安装, 扩展名计数降级）: {stats}"


# ══════════════════════════════════════════════════════════════════════
#  引擎 2：代码文件 → ctags
# ══════════════════════════════════════════════════════════════════════

_KIND_SYMBOL = {
    "class": "class", "struct": "struct", "interface": "interface",
    "enum": "enum", "union": "union", "namespace": "ns", "module": "mod",
    "package": "pkg", "function": "def", "member": "def", "method": "def",
    "prototype": "def", "macro": "#define", "typedef": "type", "type": "type",
    "property": "prop", "prop": "prop", "event": "event",
    "implementation": "impl",        # Rust impl 块
    "field": "",                     # 结构体字段（Go/Rust）
    "enumerator": "",                # 枚举成员
    "variable": "", "constant": "", "label": "",
}

# 大纲要"结构"而非"所有符号"——这些 kind 是噪音，过滤掉。
# （ctags 部分语言默认禁用它们，但 --kinds-all=* 会全开，需在此兜底）
_NOISE_KINDS = {
    "parameter",     # 函数参数（self/name 等）
    "local",         # 局部变量
    "import",        # import 语句（已单独提取）
    "unknown",       # 未解析的外部引用
    "namespace",     # Python: 其他文件的模块引用
    "receiver",      # Go 方法接收者（冗余，签名已含）
}

# Python/JS import 提取正则（对代码文件补充依赖信息）
# 注意：必须限定单行（行首用 [ \t]*，内容用 [^\n]），
# 否则 \s 会跨行贪婪吞掉后续内容（如 "from x import y\n\nCONSTANT"）。
_IMPORT_PATTERNS = {
    ".py": re.compile(r"^[ \t]*(?:from\s+([\w.]+)\s+import\s+([^\n]+?)\s*$|import\s+([\w.]+(?:[ \t]*,[ \t]*[\w.]+)*)\s*$)", re.M),
    ".js": re.compile(r"^[ \t]*import\s+[^\n]*?from\s+['\"]([^'\"]+)['\"]", re.M),
    ".jsx": re.compile(r"^[ \t]*import\s+[^\n]*?from\s+['\"]([^'\"]+)['\"]", re.M),
    ".ts": re.compile(r"^[ \t]*import\s+[^\n]*?from\s+['\"]([^'\"]+)['\"]", re.M),
    ".tsx": re.compile(r"^[ \t]*import\s+[^\n]*?from\s+['\"]([^'\"]+)['\"]", re.M),
    ".vue": re.compile(r"^[ \t]*import\s+[^\n]*?from\s+['\"]([^'\"]+)['\"]", re.M),
    ".svelte": re.compile(r"^[ \t]*import\s+[^\n]*?from\s+['\"]([^'\"]+)['\"]", re.M),
}


def _outline_code_file(path: Path) -> str:
    line_count = _count_lines(path)
    lines = [f"# {path.name} ({line_count} 行)"]

    # 1. import 依赖（可选，按扩展名）
    imp_pat = _IMPORT_PATTERNS.get(path.suffix.lower())
    if imp_pat:
        imports = _extract_imports(_read_text(path), imp_pat)
        if imports:
            lines.append("imports: " + ", ".join(imports))

    # 2. ctags 符号表
    ct = _run_cmd([
        "ctags", "--output-format=json", "--fields=+n+k+S",
        "--kinds-all=*", str(path),
    ], timeout=20)
    if ct.returncode != 0 or not ct.stdout.strip():
        # ctags 不可用或无符号 → 文本兜底
        return _outline_text(path, header=lines[0])

    symbols = _parse_ctags(ct.stdout)
    if not symbols:
        return _outline_text(path, header=lines[0])

    lines.extend(_render_symbol_tree(symbols))
    return "\n".join(lines)


def _extract_imports(text: str, pattern: re.Pattern) -> list[str]:
    out = []
    n_groups = pattern.groups   # Python 正则 3 组, JS 正则 1 组
    for m in pattern.finditer(text):
        if n_groups >= 2 and m.group(1) and m.group(2):   # from X import Y
            out.append(f"from {m.group(1)} import {m.group(2).strip()}")
        elif n_groups >= 3 and m.group(3):                # import X
            out.append(f"import {m.group(3).strip()}")
        elif m.group(1):                                  # JS: from 'x'
            out.append(m.group(1))
    # 去重保序 + 截断
    seen, dedup = set(), []
    for i in out:
        if i not in seen:
            seen.add(i)
            dedup.append(i)
    return dedup[:20]


def _parse_ctags(raw: str) -> list[dict]:
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _render_symbol_tree(entries: list[dict]) -> list[str]:
    """按 scope 重建层级缩进树。ctags 输出非行号序，先排序。"""
    entries = sorted(entries, key=lambda e: e.get("line", 0))
    out: list[str] = []
    for e in entries:
        kind = e.get("kind", "")
        if kind in _NOISE_KINDS:          # 局部变量/参数等噪音，过滤
            continue
        name = e.get("name", "?")
        sig = e.get("signature", "")
        line = e.get("line", 0)
        scope = e.get("scope") or ""

        depth = scope.count(".") + 1 if scope else 0
        if kind == "member":
            # member 有签名 → 方法(def)；无签名 → 字段(留空)
            sym = "def" if sig else ""
        else:
            sym = _KIND_SYMBOL.get(kind, kind)
        prefix = f"{sym} " if sym else ""
        sig_str = ""
        if sig:
            sig_str = sig if len(sig) <= MAX_SIG_LEN else sig[: MAX_SIG_LEN - 1] + "…"
        out.append(f"{'  ' * depth}{prefix}{name}{sig_str} :{line}")
    return out or ["（无可提取符号）"]


# ══════════════════════════════════════════════════════════════════════
#  引擎 3/4/5：md / json / yaml 键结构树
# ══════════════════════════════════════════════════════════════════════

def _outline_markdown(path: Path) -> str:
    text = _read_text(path)
    n = text.count("\n") + 1
    lines = [f"# {path.name} ({n} 行)"]
    found = False
    for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*#*\s*$", text, re.M):
        level = len(m.group(1))
        title = m.group(2).strip()
        lines.append(f"{'  ' * (level - 1)}H{level} {title}")
        found = True
    if not found:
        lines.append("（无标题结构）")
    return "\n".join(lines)


def _outline_json(path: Path) -> str:
    text = _read_text(path)
    n = text.count("\n") + 1
    lines = [f"# {path.name} ({n} 行)"]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return f"# {path.name}: JSON 解析失败 — {e}"

    def walk(obj: Any, depth: int, max_depth: int = 8) -> None:
        if depth > max_depth:
            lines.append("  " * depth + "…")
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                lines.append("  " * depth + f"{k}: {_json_type(v)}")
                if isinstance(v, (dict, list)) and depth < max_depth:
                    walk(v, depth + 1, max_depth)
        elif isinstance(obj, list):
            if not obj:
                lines.append("  " * depth + "[]")
            elif isinstance(obj[0], (dict, list)):
                lines.append("  " * depth + f"[数组 x{len(obj)}, 首项结构]")
                walk(obj[0], depth + 1, max_depth)
            else:
                sample = ", ".join(repr(x) for x in obj[:5])
                more = f", …" if len(obj) > 5 else ""
                lines.append("  " * depth + f"[数组 x{len(obj)}: {sample}{more}]")

    walk(data, 0)
    return "\n".join(lines)


def _json_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, dict):
        return f"object[{len(v)}]"
    if isinstance(v, list):
        return f"array[{len(v)}]"
    return type(v).__name__


def _outline_yaml(path: Path) -> str:
    text = _read_text(path)
    n = text.count("\n") + 1
    lines = [f"# {path.name} ({n} 行)"]
    stack: list[tuple[int, str]] = []   # (缩进, 键)
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith(("#", "---", "...")):
            continue
        indent = len(raw) - len(raw.lstrip())
        content = raw.strip()
        if content.startswith("- "):
            content = content[2:].strip()
        m = re.match(r"([^:#]+?)\s*:\s*(.*)$", content)
        if not m:
            continue                      # 纯值行（多行文本等），大纲跳过
        key = m.group(1).strip().strip("\"'")
        val = m.group(2).strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        level = len(stack)
        stack.append((indent, key))
        suffix = f" = {val}" if val and not val.startswith(("-", "|", ">", "&", "*")) else ""
        lines.append("  " * level + f"{key}{suffix}")
    if len(lines) == 1:
        lines.append("（无可提取键结构）")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
#  引擎 6：兜底文本预览
# ══════════════════════════════════════════════════════════════════════

def _outline_text(path: Path, header: str | None = None) -> str:
    n = _count_lines(path)
    lines = [header or f"# {path.name} ({n} 行)"]
    lines.append(f"（非代码/不可提取结构, 前 {MAX_PREVIEW} 行预览）")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, ln in enumerate(f):
            if i >= MAX_PREVIEW:
                break
            lines.append("  " + ln.rstrip()[:100])
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
#  路由 + 入口
# ══════════════════════════════════════════════════════════════════════

_STRUCT_EXT = {
    ".md": _outline_markdown,
    ".markdown": _outline_markdown,
    ".json": _outline_json,
    ".yaml": _outline_yaml,
    ".yml": _outline_yaml,
}


def _outline_file(path: Path) -> str:
    if path.suffix.lower() in _STRUCT_EXT:
        return _STRUCT_EXT[path.suffix.lower()](path)
    # 其余交给 ctags（内部会兜底到文本预览）
    return _outline_code_file(path)


async def execute(params: dict[str, Any]) -> str:
    """
    Outline 入口。
    params: path（必填）, depth（可选, 目录树深度, 默认 2）
    """
    path = (params.get("path") or "").strip()
    depth = params.get("depth") or 2
    try:
        depth = max(1, min(int(depth), 6))
    except (TypeError, ValueError):
        depth = 2

    if not path:
        return "⚠️ 缺少必填参数 `path`，请指定要查看的文件或目录绝对路径，如 outline(path=/home/user/project)"

    p = Path(path).resolve()
    if not p.exists():
        return f"⚠️ 路径不存在: {path}"

    try:
        if p.is_dir():
            return await asyncio.to_thread(_outline_dir, p, depth)
        if _is_binary(p):
            return f"# {p.name}: 二进制文件, {p.stat().st_size} 字节（跳过结构分析）"
        return await asyncio.to_thread(_outline_file, p)
    except Exception as e:  # noqa: BLE001 — 工具层兜底，任何异常转成友好信息
        return f"⚠️ outline 执行失败: {type(e).__name__}: {e}"
