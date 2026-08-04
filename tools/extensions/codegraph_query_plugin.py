"""
CodeGraph 查询插件 — 基于 AST 的按需代码结构分析

纯 Python 标准库，零外部依赖。

泛用性：通过 `project_path` 参数指定任意项目目录，不局限于本 Agent。
"""

__fp__ = {
    "name": "codegraph",
    "version": "1.0.0",
    "description": "基于 AST 的按需代码结构分析（跨文件依赖/调用关系）",
    "author": "zpb",
    "license": "GPL-3.0",
    "type": "tools",
}

import ast
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# ── 工具定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "codegraph",
        "description": """深度分析 Python 代码的**关系网络**（跨文件依赖/调用关系）
python ONLY!

**结构化查询**：用 action（动作枚举）+ 具名参数，取代自然语言。

动作速查（按需填对应参数）：
- file_structure(file, kind?)  文件结构（kind: all/class/function/method）
- class_methods(class)         类的所有方法/属性
- symbol_details(symbol)       符号的完整定义/源码
- callers(symbol)              谁调用了该符号
- imports(file)                文件导入了哪些模块
- importers(symbol)            谁导入了某模块（反向依赖）
- impact(file)                 修改该文件影响哪些模块
- call_chain(symbol)           符号的调用链
- search_in_file(keyword, file) 在指定文件内搜索关键词
- file_overview(file)          文件概览（类/函数/导入统计）
- all_modules()                列出项目所有模块（无需额外参数）
- fulltext(keyword)            项目全文搜索

注意：单文件/目录的类、函数、标题结构速览请用 outline（更轻量更快）。
""",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "file_structure",
                        "class_methods",
                        "symbol_details",
                        "callers",
                        "imports",
                        "importers",
                        "impact",
                        "call_chain",
                        "search_in_file",
                        "file_overview",
                        "all_modules",
                        "fulltext",
                    ],
                    "description": "查询动作（必填）。结构化指令，取代自然语言，见上方动作速查。",
                },
                "project_path": {
                    "type": "string",
                    "description": "要分析的项目根目录（必填），如 /path/to/project",
                },
                "file": {
                    "type": "string",
                    "description": "文件路径（相对项目根或绝对路径）。用于 file_structure / imports / impact / search_in_file / file_overview",
                },
                "symbol": {
                    "type": "string",
                    "description": "符号名（函数/方法/类/模块名）。用于 symbol_details / callers / importers / call_chain",
                },
                "class": {
                    "type": "string",
                    "description": "类名。用于 class_methods",
                },
                "keyword": {
                    "type": "string",
                    "description": "关键词。用于 search_in_file / fulltext",
                },
                "kind": {
                    "type": "string",
                    "enum": ["all", "class", "function", "method"],
                    "description": "file_structure 的过滤粒度，默认 all（全部）",
                },
            },
            "required": ["action", "project_path"],
        },
    },
}


# ══════════════════════════════════════════════════════════════════════
#  AST 分析引擎（所有函数接受 root 参数，实现泛用性）
# ══════════════════════════════════════════════════════════════════════

EXCLUDE_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    "node_modules",
    "env",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    "build",
    "dist",
    "*.egg-info",
    ".tox",
    ".nox",
    ".idea",
    ".vscode",
}

STDLIB_MODULES = {
    "os",
    "sys",
    "re",
    "json",
    "math",
    "time",
    "datetime",
    "pathlib",
    "collections",
    "itertools",
    "functools",
    "typing",
    "ast",
    "inspect",
    "asyncio",
    "socket",
    "http",
    "urllib",
    "io",
    "base64",
    "hashlib",
    "subprocess",
    "tempfile",
    "contextlib",
    "abc",
    "enum",
    "struct",
    "textwrap",
    "string",
    "random",
    "statistics",
    "copy",
    "pprint",
    "traceback",
    "logging",
    "warnings",
    "dataclasses",
    "argparse",
    "configparser",
    "importlib",
    "pkgutil",
    "platform",
    "signal",
    "threading",
    "multiprocessing",
    "pickle",
    "shelve",
    "sqlite3",
    "xml",
    "html",
    "csv",
    "glob",
    "shutil",
    "binascii",
    "zlib",
    "gzip",
    "bz2",
    "lzma",
    "zipfile",
    "tarfile",
    "unittest",
    "doctest",
    "pdb",
    "profile",
    "cProfile",
    "ctypes",
    "curses",
    "turtle",
    "tkinter",
    "numbers",
    "decimal",
    "fractions",
    "uuid",
    "weakref",
    "types",
    "gc",
    "site",
    "atexit",
    "keyword",
    "tokenize",
    "difflib",
    "filecmp",
    "fileinput",
    "linecache",
    "gettext",
    "locale",
    "calendar",
    "mailbox",
}


def _get_py_files(root: Path) -> list[Path]:
    """获取项目所有 .py 文件（排除无关目录）"""
    py_files = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f.endswith(".py"):
                py_files.append(Path(dirpath) / f)
    return sorted(py_files)


def _resolve_file(name: str, root: Path) -> Path | None:
    """解析文件名/路径 → 绝对 Path（在 root 范围内）"""
    name = name.strip().lstrip("./")

    # 1. 精确路径（绝对或相对 root）
    candidate = root / name
    if candidate.exists() and candidate.is_file():
        return candidate

    # 2. 模糊匹配（文件名或相对路径）
    name.lower()
    for py_file in _get_py_files(root):
        try:
            rel = py_file.relative_to(root)
        except ValueError:
            continue
        if str(rel) == name or py_file.name == name:
            return py_file

    # 3. 子路径匹配（如 "core/agent.py"）
    candidate = root / name
    if candidate.exists():
        return candidate

    return None


def _parse_ast(filepath: Path) -> ast.AST | None:
    """安全解析文件为 AST"""
    try:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        return ast.parse(source)
    except SyntaxError:
        return None
    except Exception:
        return None


def _get_docstring_summary(node) -> str:
    """提取节点文档字符串的第一行"""
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, (ast.Constant, ast.Str)):
        doc = node.body[0].value.value
        first_line = doc.strip().split("\n")[0]
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        return first_line
    return ""


def _format_args(node) -> str:
    """获取函数参数列表的字符串表示"""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "()"
    args = node.args
    parts = []
    for arg in args.posonlyargs:
        parts.append(arg.arg)
    if args.posonlyargs:
        parts.append("/")
    for arg in args.args:
        parts.append(arg.arg)
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    for arg in args.kwonlyargs:
        parts.append(f"{arg.arg}=…")
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return f"({', '.join(parts)})"


def _get_decorators(node) -> list[str]:
    """获取装饰器名称列表"""
    decors = []
    for d in node.decorator_list:
        if isinstance(d, ast.Name):
            decors.append(f"@{d.id}")
        elif isinstance(d, ast.Attribute):
            decors.append(f"@{d.attr}")
        elif isinstance(d, ast.Call):
            if isinstance(d.func, ast.Name):
                decors.append(f"@{d.func.id}(…)")
            elif isinstance(d.func, ast.Attribute):
                decors.append(f"@{d.func.attr}(…)")
    return decors


def _find_enclosing_function(tree, target_node) -> str | None:
    """找到包含目标节点的最近函数/方法名"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for child in ast.walk(node):
                if child is target_node:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return node.name
                    return None
    return None


def _is_internal_import(module_name: str, root: Path) -> bool:
    """判断一个模块名是否属于项目内部"""
    if module_name.startswith("."):
        return True  # 相对导入 = 内部
    # 检查路径：把模块名转成路径，看是否存在于项目中
    path = module_name.replace(".", "/")
    for candidate in [
        root / f"{path}.py",
        root / path / "__init__.py",
        root / f"src/{path}.py",
        root / f"src/{path}/__init__.py",
    ]:
        if candidate.exists():
            return True
    return False


# ══════════════════════════════════════════════════════════════════════
#  查询处理器（全部接受 root 参数）
# ══════════════════════════════════════════════════════════════════════


def _query_file_structure(filepath: Path, root: Path, what: str = "结构") -> str:
    """查询文件中的类/函数/结构"""
    tree = _parse_ast(filepath)
    if tree is None:
        return f"⚠️ 无法解析文件: {filepath}（语法错误或不可读）"

    try:
        rel_path = filepath.relative_to(root)
    except ValueError:
        rel_path = filepath

    lines = [f"## 📄 `{rel_path}`"]

    classes = []
    functions = []
    async_funcs = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            decorators = _get_decorators(node)
            bases = [b.id if isinstance(b, ast.Name) else str(b) for b in node.bases]
            doc = _get_docstring_summary(node)
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "bases": bases,
                "decorators": decorators,
                "doc": doc,
                "method_count": len(methods),
            })
        elif isinstance(node, ast.FunctionDef):
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "args": _format_args(node),
                "decorators": _get_decorators(node),
                "doc": _get_docstring_summary(node),
            })
        elif isinstance(node, ast.AsyncFunctionDef):
            async_funcs.append({
                "name": node.name,
                "line": node.lineno,
                "args": _format_args(node),
                "decorators": _get_decorators(node),
                "doc": _get_docstring_summary(node),
            })

    if what in ("类", "定义", "结构") and classes:
        lines.append(f"\n### 类 ({len(classes)} 个)")
        for c in classes:
            base_str = f"({', '.join(c['bases'])})" if c["bases"] else ""
            decor_str = f" {' '.join(c['decorators'])}" if c["decorators"] else ""
            doc_str = f"  — {c['doc']}" if c["doc"] else ""
            lines.append(f"- `{c['name']}{base_str}` L{c['line']}{decor_str}{doc_str}")
            lines.append(f"  └ 方法: {c['method_count']} 个")

    if what in ("函数", "定义", "结构"):
        all_funcs = functions + async_funcs
        if all_funcs:
            label = "异步函数" if async_funcs and not functions else "函数"
            lines.append(f"\n### {label} ({len(all_funcs)} 个)")
            for f in all_funcs:
                decor_str = f" {' '.join(f['decorators'])}" if f["decorators"] else ""
                doc_str = f"  — {f['doc']}" if f["doc"] else ""
                prefix = "async " if f in async_funcs else ""
                lines.append(f"- `{prefix}def {f['name']}{f['args']}` L{f['line']}{decor_str}{doc_str}")

    if what == "方法" and classes:
        lines.append("\n### 所有方法")
        for c in classes:
            lines.append(f"\n**{c['name']}:**")
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == c["name"]:
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            d = _get_docstring_summary(item)
                            dec = _get_decorators(item)
                            dec_str = f" {' '.join(dec)}" if dec else ""
                            doc_str = f"  — {d}" if d else ""
                            prefix = "async " if isinstance(item, ast.AsyncFunctionDef) else ""
                            lines.append(
                                f"  - `{prefix}def {item.name}{_format_args(item)}` L{item.lineno}{dec_str}{doc_str}"
                            )
                    break

    lines.append(f"\n---\n📊 总计: {len(classes)} 个类, {len(functions)} 个函数, {len(async_funcs)} 个异步函数")
    return "\n".join(lines)


def _query_class_methods(class_name: str, root: Path) -> str:
    """查询指定类的方法"""
    for filepath in _get_py_files(root):
        tree = _parse_ast(filepath)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                try:
                    rel_path = filepath.relative_to(root)
                except ValueError:
                    rel_path = filepath
                lines = [f"## 🏛️ `{class_name}` in `{rel_path}`"]

                bases = [b.id if isinstance(b, ast.Name) else ast.dump(b) for b in node.bases]
                if bases:
                    lines.append(f"\n**继承:** {', '.join(bases)}")

                doc = _get_docstring_summary(node)
                if doc:
                    lines.append(f"\n**文档:** {doc}")

                methods = []
                properties = []
                class_methods = []
                static_methods = []
                magic_methods = []

                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        decors = _get_decorators(item)
                        entry = {
                            "name": item.name,
                            "line": item.lineno,
                            "args": _format_args(item),
                            "decorators": decors,
                            "doc": _get_docstring_summary(item),
                            "is_async": isinstance(item, ast.AsyncFunctionDef),
                        }
                        if "@property" in decors:
                            properties.append(entry)
                        elif "@classmethod" in decors:
                            class_methods.append(entry)
                        elif "@staticmethod" in decors:
                            static_methods.append(entry)
                        elif item.name.startswith("__") and item.name.endswith("__"):
                            magic_methods.append(entry)
                        else:
                            methods.append(entry)

                sections = [("普通方法", methods)]
                if properties:
                    sections.insert(0, ("📌 属性", properties))
                if class_methods:
                    sections.insert(0, ("🔧 类方法", class_methods))
                if static_methods:
                    sections.insert(0, ("🔩 静态方法", static_methods))
                if magic_methods:
                    sections.append(("✨ 魔术方法", magic_methods))

                for title, items in sections:
                    if items:
                        lines.append(f"\n### {title} ({len(items)} 个)")
                        for m in items:
                            dec_str = f" {' '.join(m['decorators'])}" if m["decorators"] else ""
                            doc_str = f"  — {m['doc']}" if m["doc"] else ""
                            prefix = "async " if m.get("is_async") else ""
                            lines.append(f"- `{prefix}def {m['name']}{m['args']}` L{m['line']}{dec_str}{doc_str}")

                lines.append(
                    f"\n---\n📊 总计: {len(methods)} 方法, {len(properties)} 属性, {len(magic_methods)} 魔术方法"
                )
                return "\n".join(lines)

    return f"⚠️ 找不到类 `{class_name}`"


def _query_symbol_details(symbol_name: str, root: Path) -> str:
    """查询符号（函数/类）的完整定义"""
    for filepath in _get_py_files(root):
        tree = _parse_ast(filepath)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol_name:
                try:
                    rel_path = filepath.relative_to(root)
                except ValueError:
                    rel_path = filepath
                lines = [f"## 🔍 `{symbol_name}` in `{rel_path}`"]

                if isinstance(node, ast.ClassDef):
                    bases = [b.id if isinstance(b, ast.Name) else str(b) for b in node.bases]
                    if bases:
                        lines.append(f"\n**继承:** {', '.join(bases)}")
                    methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    lines.append(f"\n**方法 ({len(methods)} 个):**")
                    for m in methods:
                        lines.append(f"  - `{m.name}{_format_args(m)}` L{m.lineno}")
                else:
                    lines.append(
                        f"\n**签名:** `{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}"
                        f"def {node.name}{_format_args(node)}`"
                    )
                    lines.append(f"**行号:** L{node.lineno}")

                decors = _get_decorators(node)
                if decors:
                    lines.append(f"\n**装饰器:** {' '.join(decors)}")

                doc = _get_docstring_summary(node)
                if doc:
                    lines.append(f"\n**文档:** {doc}")

                if not isinstance(node, ast.ClassDef):
                    calls = []
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            if isinstance(sub.func, ast.Name):
                                calls.append(sub.func.id)
                            elif isinstance(sub.func, ast.Attribute):
                                calls.append(f"{sub.func.attr}")
                    if calls:
                        call_counts = Counter(calls)
                        top_calls = call_counts.most_common(10)
                        lines.append("\n**内部调用:**")
                        for name, count in top_calls:
                            lines.append(f"  - {name} ({count} 次)")

                return "\n".join(lines)

    return f"⚠️ 找不到符号 `{symbol_name}`"


def _query_callers(func_name: str, root: Path) -> str:
    """查询谁调用了某个函数"""
    func_name = func_name.rstrip("()")
    results = []
    defined_in = None

    for filepath in _get_py_files(root):
        tree = _parse_ast(filepath)
        if tree is None:
            continue

        method_owners = {}
        call_lines = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_owners[item.name] = node.name

            if isinstance(node, ast.Call):
                caller_name = None
                if isinstance(node.func, ast.Name) and node.func.id == func_name:
                    caller_name = func_name
                elif isinstance(node.func, ast.Attribute) and node.func.attr == func_name:
                    caller_name = f"{node.func.attr}"

                if caller_name:
                    context_name = _find_enclosing_function(tree, node)
                    call_lines.append({
                        "line": node.lineno,
                        "context": context_name or "(模块顶层)",
                    })

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                try:
                    defined_in = f"{filepath.relative_to(root)} L{node.lineno}"
                except ValueError:
                    defined_in = f"{filepath} L{node.lineno}"

        if call_lines:
            try:
                rel_path = filepath.relative_to(root)
            except ValueError:
                rel_path = filepath
            for c in call_lines:
                owner = method_owners.get(c["context"], "")
                ctx = f"{owner}.{c['context']}" if owner else c["context"]
                results.append(f"- `{rel_path}` L{c['line']} 在 `{ctx}` 中")

    if not results:
        return f"ℹ️ 未找到 `{func_name}` 的调用者"

    lines = [f"## 📞 `{func_name}` 的调用者 ({len(results)} 处)"]
    if defined_in:
        lines.append(f"\n**定义位置:** {defined_in}")
    lines.append("")
    lines.extend(results)
    return "\n".join(lines)


def _query_imports(filepath: Path, root: Path) -> str:
    """查询文件的导入关系"""
    tree = _parse_ast(filepath)
    if tree is None:
        return f"⚠️ 无法解析文件: {filepath}"

    try:
        rel_path = filepath.relative_to(root)
    except ValueError:
        rel_path = filepath

    lines = [f"## 📥 `{rel_path}` 的导入关系"]

    internal_imports = []
    external_imports = []
    stdlib_imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                top_level = name.split(".")[0]
                if top_level in STDLIB_MODULES:
                    stdlib_imports.append(name)
                elif _is_internal_import(name, root):
                    internal_imports.append(name)
                else:
                    external_imports.append(name)

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.name for alias in node.names]

            if module.startswith(".") or module == "":
                for n in names:
                    internal_imports.append(f"{module}{n}")
                continue

            top_level = module.split(".")[0]
            if top_level in STDLIB_MODULES:
                stdlib_imports.append(f"from {module} import {', '.join(names)}")
            elif _is_internal_import(module, root):
                internal_imports.append(f"from {module} import {', '.join(names)}")
            else:
                external_imports.append(f"from {module} import {', '.join(names)}")

    sections = [
        ("🏗️ 项目内部模块", internal_imports),
        ("📦 第三方库", external_imports),
        ("📚 标准库", stdlib_imports),
    ]

    for title, items in sections:
        if items:
            lines.append(f"\n### {title} ({len(items)} 个)")
            for item in sorted(items):
                lines.append(f"- `{item}`")

    lines.append(
        f"\n---\n📊 总计: {len(internal_imports)} 内部 + {len(external_imports)} 第三方 + {len(stdlib_imports)} 标准库"
    )
    return "\n".join(lines)


def _query_importers(module_name: str, root: Path) -> str:
    """查询哪些文件导入了指定模块"""
    module_name = module_name.strip()
    results = []

    for filepath in _get_py_files(root):
        tree = _parse_ast(filepath)
        if tree is None:
            continue
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == module_name or alias.name.startswith(f"{module_name}."):
                        found = True
                        break
            elif isinstance(node, ast.ImportFrom) and node.module and (
                node.module == module_name or node.module.startswith(f"{module_name}.")
            ):
                    found = True
                    break
            if found:
                break
        if found:
            try:
                results.append(str(filepath.relative_to(root)))
            except ValueError:
                results.append(str(filepath))

    if not results:
        return f"ℹ️ 未找到导入 `{module_name}` 的文件"

    lines = [f"## 🔗 导入了 `{module_name}` 的文件 ({len(results)} 个)"]
    for r in results:
        lines.append(f"- `{r}`")
    return "\n".join(lines)


def _query_impact(filepath: Path, root: Path) -> str:
    """影响分析：如果修改了该文件，哪些其他文件会受影响"""
    try:
        rel_path = filepath.relative_to(root)
    except ValueError:
        rel_path = filepath

    lines = [f"## ⚡ 修改 `{rel_path}` 的影响分析"]

    # 1. 该文件自身的导入
    tree = _parse_ast(filepath)
    own_imports = []
    if tree:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.module.startswith("."):
                own_imports.append(node.module)

    # 2. 哪些文件依赖该文件
    file_module = str(rel_path).replace("/", ".").replace("\\", ".").rstrip(".py")
    if file_module.endswith(".__init__"):
        file_module = file_module[:-9]

    dependents = []
    for py_file in _get_py_files(root):
        if py_file == filepath:
            continue
        t = _parse_ast(py_file)
        if t is None:
            continue
        for node in ast.walk(t):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == file_module or alias.name.startswith(f"{file_module}."):
                        dependents.append(py_file)
                        break
            elif isinstance(node, ast.ImportFrom) and node.module and (
                node.module == file_module or node.module.startswith(f"{file_module}.")
            ):
                    dependents.append(py_file)
                    break

    if dependents:
        lines.append(f"\n### 🔻 直接依赖者 ({len(dependents)} 个)")
        for d in sorted(set(dependents)):
            try:
                d_rel = d.relative_to(root)
            except ValueError:
                d_rel = d
            lines.append(f"- `{d_rel}`")
    else:
        lines.append("\n### 🔻 无直接依赖者")

    # 3. 自身依赖（仅项目内部）
    internal_deps = [m for m in own_imports if _is_internal_import(m, root)]
    if internal_deps:
        lines.append(f"\n### 🔼 自身依赖 ({len(internal_deps)} 个)")
        for d in sorted(internal_deps):
            lines.append(f"- `{d}`")

    return "\n".join(lines)


def _query_call_chain(func_name: str, root: Path) -> str:
    """查询函数的调用链（该函数调用了什么）"""
    func_name = func_name.rstrip("()")

    for filepath in _get_py_files(root):
        tree = _parse_ast(filepath)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                try:
                    rel_path = filepath.relative_to(root)
                except ValueError:
                    rel_path = filepath
                lines = [f"## 🔗 `{func_name}` 的调用链 (定义在 `{rel_path}` L{node.lineno})"]

                doc = _get_docstring_summary(node)
                if doc:
                    lines.append(f"\n**文档:** {doc}")

                self_calls = []
                attr_calls = []
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        if isinstance(sub.func, ast.Name):
                            self_calls.append(sub.func.id)
                        elif isinstance(sub.func, ast.Attribute):
                            attr_calls.append(sub.func.attr)

                if self_calls:
                    lines.append(f"\n### 直接调用的函数 ({len(set(self_calls))} 个)")
                    for name, count in Counter(self_calls).most_common():
                        lines.append(f"- `{name}()` — {count} 次调用")

                if attr_calls:
                    lines.append(f"\n### 调用的方法/属性 ({len(set(attr_calls))} 个)")
                    for name, count in Counter(attr_calls).most_common():
                        lines.append(f"- `.{name}()` — {count} 次调用")

                return "\n".join(lines)

    return f"⚠️ 找不到函数 `{func_name}`"


def _query_file_overview(filepath: Path, root: Path) -> str:
    """文件整体概览"""
    tree = _parse_ast(filepath)
    if tree is None:
        return f"⚠️ 无法解析文件: {filepath}"

    try:
        rel_path = filepath.relative_to(root)
    except ValueError:
        rel_path = filepath

    stat = filepath.stat()
    with open(filepath, "rb") as f:
        line_count = sum(1 for _ in f)
    lines = [f"## 📋 `{rel_path}` 概览"]
    lines.append(f"\n**基本信息:** 大小 {stat.st_size:,} bytes | {line_count} 行")

    doc = _get_docstring_summary(tree)
    if doc:
        lines.append(f"\n**模块说明:** {doc}")

    classes = []
    functions = []
    async_funcs = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            classes.append({"name": node.name, "methods": len(methods), "line": node.lineno})
        elif isinstance(node, ast.FunctionDef):
            functions.append({"name": node.name, "line": node.lineno})
        elif isinstance(node, ast.AsyncFunctionDef):
            async_funcs.append({"name": node.name, "line": node.lineno})

    if classes:
        lines.append(f"\n### 类 ({len(classes)} 个)")
        for c in classes:
            lines.append(f"- `{c['name']}` L{c['line']} — {c['methods']} 个方法")

    all_funcs = functions + async_funcs
    if all_funcs:
        lines.append(f"\n### 顶层函数 ({len(all_funcs)} 个)")
        for f in all_funcs:
            prefix = "async " if f in async_funcs else ""
            lines.append(f"- `{prefix}def {f['name']}()` L{f['line']}")

    return "\n".join(lines)


def _query_all_modules(root: Path) -> str:
    """列出项目的所有 Python 模块"""
    py_files = _get_py_files(root)
    lines = ["## 🗂️ 项目模块列表", ""]

    dirs = defaultdict(list)
    for f in py_files:
        try:
            rel = f.relative_to(root)
        except ValueError:
            rel = f
        parent = str(rel.parent)
        if parent == ".":
            parent = "(根目录)"
        dirs[parent].append(rel.name)

    for directory in sorted(dirs.keys()):
        files = dirs[directory]
        lines.append(f"**{directory}/** ({len(files)} 个)")
        for fname in sorted(files):
            lines.append(f"- `{fname}`")
        lines.append("")

    lines.append(f"---\n📊 总计: **{len(py_files)}** 个 Python 文件")
    return "\n".join(lines)


def _query_search_in_file(filepath: Path, keyword: str, root: Path) -> str:
    """在文件中搜索关键词"""
    tree = _parse_ast(filepath)
    if tree is None:
        return f"⚠️ 无法解析文件: {filepath}"

    try:
        rel_path = filepath.relative_to(root)
    except ValueError:
        rel_path = filepath

    lines = [f'## 🔍 在 `{rel_path}` 中搜索 "{keyword}"']

    results = []
    with open(filepath, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if keyword in line:
                results.append((i, line.rstrip()))

    if results:
        lines.append(f"\n找到 {len(results)} 处匹配:\n")
        for line_no, content in results:
            highlighted = content.replace(keyword, f"**{keyword}**")
            lines.append(f"  L{line_no:4d} │ {highlighted[:120]}")
    else:
        lines.append("\n未找到匹配")

    return "\n".join(lines)


def _query_fulltext(keyword: str, root: Path) -> str:
    """全文搜索（在所有文件中搜索）"""
    words = re.findall(r"[a-zA-Z_]\w*", keyword)
    if not words:
        return "⚠️ 未识别出可搜索的代码符号。请尝试更具体的查询，如 'who calls process()' 或 'agent.py 有哪些类？'"

    results = []
    for filepath in _get_py_files(root):
        with open(filepath, encoding="utf-8", errors="replace") as f:
            content = f.read()
        for w in words:
            if w in content:
                try:
                    results.append(str(filepath.relative_to(root)))
                except ValueError:
                    results.append(str(filepath))
                break

    if not results:
        return f"ℹ️ 未找到包含 '{', '.join(words)}' 的文件"

    lines = [f'## 🔍 搜索 "{", ".join(words)}" 结果 ({len(results)} 个文件)']
    for r in sorted(results):
        lines.append(f"- `{r}`")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════════════


def _act_file_structure(params: dict[str, Any], root: Path) -> str:
    """文件结构：类/函数/方法 的层级骨架"""
    file = params.get("file", "").strip()
    if not file:
        return "⚠️ action=file_structure 需要参数 `file`（如 file=agent.py）"
    fp = _resolve_file(file, root)
    if not fp:
        return f"⚠️ 找不到文件: {file}"
    kind = (params.get("kind", "all") or "all").strip()
    what_map = {"class": "类", "function": "函数", "method": "方法"}
    what = what_map.get(kind, "结构")
    return _query_file_structure(fp, root, what)


def _act_class_methods(params: dict[str, Any], root: Path) -> str:
    """类的方法/属性"""
    class_name = params.get("class", "").strip()
    if not class_name:
        return "⚠️ action=class_methods 需要参数 `class`（如 class=Agent）"
    return _query_class_methods(class_name, root)


def _act_symbol_details(params: dict[str, Any], root: Path) -> str:
    """符号完整定义"""
    symbol = params.get("symbol", "").strip()
    if not symbol:
        return "⚠️ action=symbol_details 需要参数 `symbol`（如 symbol=process）"
    return _query_symbol_details(symbol, root)


def _act_callers(params: dict[str, Any], root: Path) -> str:
    """谁调用了符号"""
    symbol = params.get("symbol", "").strip()
    if not symbol:
        return "⚠️ action=callers 需要参数 `symbol`（如 symbol=process）"
    return _query_callers(symbol.rstrip("()"), root)


def _act_imports(params: dict[str, Any], root: Path) -> str:
    """文件导入了哪些模块"""
    file = params.get("file", "").strip()
    if not file:
        return "⚠️ action=imports 需要参数 `file`（如 file=agent.py）"
    fp = _resolve_file(file, root)
    if not fp:
        return f"⚠️ 找不到文件: {file}"
    return _query_imports(fp, root)


def _act_importers(params: dict[str, Any], root: Path) -> str:
    """谁导入了某模块（反向依赖）"""
    module = params.get("symbol", "").strip()
    if not module:
        return "⚠️ action=importers 需要参数 `symbol`（模块名，如 symbol=utils）"
    return _query_importers(module, root)


def _act_impact(params: dict[str, Any], root: Path) -> str:
    """修改文件的影响面"""
    file = params.get("file", "").strip()
    if not file:
        return "⚠️ action=impact 需要参数 `file`（如 file=agent.py）"
    fp = _resolve_file(file, root)
    if not fp:
        return f"⚠️ 找不到文件: {file}"
    return _query_impact(fp, root)


def _act_call_chain(params: dict[str, Any], root: Path) -> str:
    """符号调用链"""
    symbol = params.get("symbol", "").strip()
    if not symbol:
        return "⚠️ action=call_chain 需要参数 `symbol`（如 symbol=process）"
    return _query_call_chain(symbol.rstrip("()"), root)


def _act_search_in_file(params: dict[str, Any], root: Path) -> str:
    """文件内搜索关键词"""
    keyword = params.get("keyword", "").strip()
    file = params.get("file", "").strip()
    if not keyword or not file:
        return "⚠️ action=search_in_file 需要参数 `keyword` 和 `file`（如 keyword=ToolRegistry file=agent.py）"
    fp = _resolve_file(file, root)
    if not fp:
        return f"⚠️ 找不到文件: {file}"
    return _query_search_in_file(fp, keyword, root)


def _act_file_overview(params: dict[str, Any], root: Path) -> str:
    """文件概览"""
    file = params.get("file", "").strip()
    if not file:
        return "⚠️ action=file_overview 需要参数 `file`（如 file=agent.py）"
    fp = _resolve_file(file, root)
    if not fp:
        return f"⚠️ 找不到文件: {file}"
    return _query_file_overview(fp, root)


def _act_fulltext(params: dict[str, Any], root: Path) -> str:
    """项目全文搜索"""
    keyword = params.get("keyword", "").strip()
    if not keyword:
        return "⚠️ action=fulltext 需要参数 `keyword`（如 keyword=ToolRegistry）"
    return _query_fulltext(keyword, root)


ACTION_HANDLERS: dict[str, callable] = {
    "file_structure": _act_file_structure,
    "class_methods": _act_class_methods,
    "symbol_details": _act_symbol_details,
    "callers": _act_callers,
    "imports": _act_imports,
    "importers": _act_importers,
    "impact": _act_impact,
    "call_chain": _act_call_chain,
    "search_in_file": _act_search_in_file,
    "file_overview": _act_file_overview,
    "all_modules": lambda params, root: _query_all_modules(root),
    "fulltext": _act_fulltext,
}

ACTION_USAGE = """可用动作（action）：
- file_structure(file, kind?)  文件结构（kind: all/class/function/method）
- class_methods(class)         类的所有方法/属性
- symbol_details(symbol)       符号的完整定义/源码
- callers(symbol)              谁调用了该符号
- imports(file)                文件导入了哪些模块
- importers(symbol)            谁导入了某模块（反向依赖）
- impact(file)                 修改该文件影响哪些模块
- call_chain(symbol)           符号的调用链
- search_in_file(keyword, file) 在指定文件内搜索关键词
- file_overview(file)          文件概览
- all_modules()                列出项目所有模块
- fulltext(keyword)            项目全文搜索"""


async def execute(params: dict[str, Any]) -> str:
    """
    CodeGraph 结构化查询入口
    action + project_path 必填，其余参数按 action 取用。
    """
    action = params.get("action", "").strip()
    project_path = params.get("project_path", "")

    if not project_path:
        return "⚠️ 缺少必填参数 `project_path`，请指定要分析的项目根目录，如 project_path=/path/to/project"

    # 解析项目根目录
    root = Path(project_path).resolve()
    if not root.exists() or not root.is_dir():
        return f"⚠️ 项目目录不存在: {project_path}"

    if not action:
        return f"⚠️ 缺少必填参数 `action`。\n\n{ACTION_USAGE}"

    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        return f"⚠️ 未知 action: `{action}`。\n\n{ACTION_USAGE}"

    return handler(params, root)
